# -*- coding: utf-8 -*-
"""登录状态机：手机号验证码 / 扫码两条入口。

上游把 gen 与 wait 拆成两次独立进程，各自启动又关闭 127.0.0.1:3000 的 api 服务。
两者必须在二维码有效期内（约 2 分钟）衔接，所以这里用一个后台任务串起来，
并在整个流程期间持有全局锁——否则签到会撞上 3000 端口占用。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .store import mask_userid

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
QR_TTL = 150


@dataclass
class QrSession:
    id: str
    status: str = "pending"  # pending | confirmed | expired | failed
    image: bytes | None = None
    error: str = ""
    detail: str = ""
    created: float = field(default_factory=time.time)
    expires_in: int = QR_TTL
    log: list[str] = field(default_factory=list)
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def expired(self) -> bool:
        return time.time() > self.created + self.expires_in


class LoginManager:
    def __init__(self, sidecar, store, lock: asyncio.Lock, log) -> None:
        self.sidecar = sidecar
        self.store = store
        self.lock = lock
        self.log = log
        self.pending_phone: str = ""
        self.pending_ts: float = 0.0
        self._sessions: dict[str, QrSession] = {}
        self._tasks: set[asyncio.Task] = set()

    # ---------- 手机号 ----------

    async def send_code(self, phone: str) -> dict:
        phone = (phone or "").strip()
        if not PHONE_RE.match(phone):
            return {"ok": False, "message": "手机号格式不对，应为 11 位大陆手机号"}

        async with self.lock:
            if not self.sidecar.is_ready():
                boot = await self.sidecar.ensure()
                if not boot.get("ok"):
                    return {"ok": False, "message": f"侧车未就绪：{boot.get('error', '')}"}

            res = await self.sidecar.run_script(
                "sent.js", timeout=180, env_extra={"PHONE": phone}
            )
        if res.code == 0 and res.has("发送成功"):
            self.pending_phone = phone
            self.pending_ts = time.time()
            return {"ok": True, "message": "验证码已发送，5 分钟内回复「/酷狗登录 验证码 <6位数字>」"}

        detail = res.tail(6) or res.error or "未知错误"
        return {"ok": False, "message": f"发送失败：{detail}"}

    async def login_with_code(self, code: str, phone: str = "") -> dict:
        code = (code or "").strip()
        if not re.fullmatch(r"\d{4,8}", code):
            return {"ok": False, "message": "验证码格式不对，应为 4-8 位数字"}

        phone = (phone or self.pending_phone or "").strip()
        if not phone or time.time() - self.pending_ts > 600:
            return {"ok": False, "message": "没有待验证的手机号，请先发送「/酷狗登录 <手机号>」"}

        async with self.lock:
            if not self.sidecar.is_ready():
                boot = await self.sidecar.ensure()
                if not boot.get("ok"):
                    return {"ok": False, "message": f"侧车未就绪：{boot.get('error', '')}"}

            res = await self.sidecar.run_script(
                "phoneLogin.js",
                timeout=240,
                env_extra={"PHONE": phone, "CODE": code, "APPEND_USER": "是"},
            )

        if res.code == 0 and res.has("登录成功"):
            self.pending_phone = ""
            accounts = self.store.accounts()
            who = accounts[-1]["userid"] if accounts else mask_userid(phone)
            return {"ok": True, "message": f"登录成功，已写入账号 {who}"}

        if res.has("暂不支持多账号绑定手机登录") or res.has("34175"):
            return {
                "ok": False,
                "message": "该手机号绑定了多个酷狗账号，接口不支持手机号登录，请改用「/酷狗登录 扫码」",
            }

        detail = res.tail(6) or res.error or "未知错误"
        return {"ok": False, "message": f"登录失败：{detail}"}

    # ---------- 扫码 ----------

    async def start_qr(self) -> dict:
        session = QrSession(id=uuid.uuid4().hex[:12])
        self._sessions[session.id] = session
        # 清掉过期会话，避免无限增长
        for key in [k for k, v in self._sessions.items() if v.expired and v.status != "pending"]:
            self._sessions.pop(key, None)

        task = asyncio.create_task(self._qr_flow(session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

        try:
            await asyncio.wait_for(session.ready.wait(), timeout=200)
        except asyncio.TimeoutError:
            session.status = "failed"
            session.error = "生成二维码超时"
            return {"ok": False, "message": session.error}

        if session.status == "failed" or not session.image:
            return {"ok": False, "message": session.error or "生成二维码失败"}

        import base64

        return {
            "ok": True,
            "qr_id": session.id,
            "expires_in": session.expires_in,
            "image": "data:image/png;base64," + base64.b64encode(session.image).decode(),
            "message": "用酷狗音乐 APP 扫码，约 2 分钟内有效",
        }

    async def _qr_flow(self, session: QrSession) -> None:
        try:
            async with self.lock:
                if not self.sidecar.is_ready():
                    boot = await self.sidecar.ensure()
                    if not boot.get("ok"):
                        session.status = "failed"
                        session.error = f"侧车未就绪：{boot.get('error', '')}"
                        session.ready.set()
                        return

                res = await self.sidecar.run_script("qrcodeLogin.js", ["gen"], timeout=240)
                if res.code != 0:
                    session.status = "failed"
                    session.error = "生成二维码失败：" + (res.tail(4) or res.error)
                    session.ready.set()
                    return

                png = Path(self.sidecar.code_dir) / "qr" / "qr-1.png"
                if not png.is_file():
                    session.status = "failed"
                    session.error = "二维码图片未生成（qr/qr-1.png 不存在）"
                    session.ready.set()
                    return

                session.image = png.read_bytes()
                session.ready.set()

                def _on_line(line: str) -> None:
                    session.log.append(line)
                    if "登录成功" in line:
                        session.status = "confirmed"
                    elif "二维码已过期" in line or "等待超时" in line:
                        session.status = "expired"
                    elif "请求出错" in line:
                        session.status = "failed"

                res2 = await self.sidecar.run_script(
                    "qrcodeLogin.js",
                    ["wait"],
                    timeout=QR_TTL + 90,
                    env_extra={"APPEND_USER": "是"},
                    on_line=_on_line,
                )

                if session.status == "pending":
                    if res2.has("登录成功"):
                        session.status = "confirmed"
                    elif res2.has("二维码已过期") or res2.has("等待超时") or res2.timed_out:
                        session.status = "expired"
                    else:
                        session.status = "failed"
                        session.error = res2.tail(4) or "等待扫码失败"

                if session.status == "confirmed":
                    accounts = self.store.accounts()
                    session.detail = accounts[-1]["userid"] if accounts else ""
                    self._cleanup_files()
                else:
                    self._cleanup_files()
        except asyncio.CancelledError:
            session.status = "failed"
            raise
        except Exception as e:  # 兜底，避免 ready 永远不 set 卡住面板
            session.status = "failed"
            session.error = str(e)
            session.ready.set()

    def _cleanup_files(self) -> None:
        code_dir = Path(self.sidecar.code_dir)
        for rel in ("qr/qr-1.png", "qr/qr-1.html", "qr/login.html", "qrkeys.json"):
            try:
                (code_dir / rel).unlink(missing_ok=True)
            except OSError:
                pass

    def qr_status(self, qr_id: str) -> dict:
        session = self._sessions.get(qr_id)
        if not session:
            return {"status": "expired", "message": "会话不存在或已过期"}
        status = session.status
        if status == "pending" and session.expired:
            status = "expired"
        return {
            "status": status,
            "message": session.detail if status == "confirmed" else session.error,
            "account": session.detail,
        }

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
