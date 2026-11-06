# -*- coding: utf-8 -*-
"""酷狗概念版 VIP 每日签到。

架构：Python 只做调度、状态机、消息交互与持久化；真正的领取逻辑由上游 Node 代码
作为侧车进程执行（api/ 是完整的酷狗客户端协议实现，用 Python 重写等于复刻协议）。

参考实现：https://github.com/develop202/kgcheckin （Node 侧车代码来源）
其 api/ 层来自 https://github.com/MakcRe/KuGouMusicApi
本插件只做 AstrBot 适配，不重写协议。
"""

from __future__ import annotations

import asyncio
import base64
import sys
import time
from collections import deque
from datetime import date
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.all import MessageChain, MessageType
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api.web import request
import astrbot.api.message_components as Comp

PLUGIN_NAME = "astrbot_plugin_kugou_checkin"
AUTHOR = "irislilymoon"

_BASE = Path(__file__).resolve().parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

# 重载插件时清掉子模块缓存，避免改完 kugou/ 下的代码不生效
for _name in [m for m in list(sys.modules) if m == "kugou" or m.startswith("kugou.")]:
    sys.modules.pop(_name, None)

from kugou.login import LoginManager  # noqa: E402
from kugou.notify import format_message  # noqa: E402
from kugou.scheduler import Scheduler  # noqa: E402
from kugou.sidecar import Sidecar, parse_checkin  # noqa: E402
from kugou.store import Store, now_cn, today_str, token_fingerprint  # noqa: E402

LOGIN_HELP = (
    "登录方式：\n"
    "1. /酷狗登录 扫码 —— 生成二维码，用酷狗音乐 APP 扫\n"
    "2. /酷狗登录 138xxxxxxxx —— 发送短信验证码\n"
    "3. /酷狗登录 验证码 123456 —— 用验证码换 token"
)


@register(
    PLUGIN_NAME,
    AUTHOR,
    "酷狗概念版 VIP 每日签到：Node 侧车执行领取，支持扫码/手机号登录、定时补跑、QQ 推送降级与面板管理",
    "0.1.0",
)
class KugouCheckinPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.store = Store(self.data_dir)
        self.lock = asyncio.Lock()
        self.sidecar = Sidecar(self.data_dir, _BASE, self.store.userinfo_path, config, logger)
        self.login = LoginManager(self.sidecar, self.store, self.lock, logger)
        self.scheduler = Scheduler(self, config, self.store, logger)

        self._running = False
        self._run_started = 0.0
        self._progress: deque[str] = deque(maxlen=200)
        self._last_result: dict = {}
        self._bg: set[asyncio.Task] = set()

        self._register_web_apis()
        self.scheduler.start()
        self._spawn(self._startup())

    # ---------------------------------------------------------------- 生命周期

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)

        def _done(t: asyncio.Task) -> None:
            self._bg.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc:
                logger.warning(f"[kgcheckin] 后台任务异常：{exc}")

        task.add_done_callback(_done)
        return task

    async def _startup(self) -> None:
        await asyncio.sleep(3)
        try:
            dropped = self.store.prune_history(int(self.config.get("keep_history_days") or 90))
            if dropped:
                logger.info(f"[kgcheckin] 清理 {dropped} 条过期历史")
        except Exception as e:
            logger.warning(f"[kgcheckin] 历史清理失败：{e}")
        state = self.store.state()
        if state.get("last_run_date"):
            logger.info(
                f"[kgcheckin] 上次运行 {state.get('last_run_date')}，"
                f"结果 {'成功' if state.get('last_ok') else '异常'}；"
                f"侧车{'已就绪' if self.sidecar.is_ready() else '未就绪'}"
            )

    async def terminate(self) -> None:
        try:
            await self.scheduler.stop()
        except Exception:
            pass
        try:
            await self.login.close()
        except Exception:
            pass
        try:
            await self.sidecar.kill_running()
        except Exception:
            pass
        for task in list(self._bg):
            task.cancel()

    # ---------------------------------------------------------------- 核心流程

    def _on_line(self, line: str) -> None:
        self._progress.append(line)

    def _judge_ok(self, res, summary) -> bool:
        if summary.token_invalid or summary.port_conflict:
            return False
        if summary.vip_count > 0 or summary.quota_used:
            return True
        if summary.listen in ("成功", "今日已领取"):
            return True
        return res.code == 0

    def _error_text(self, res, summary) -> str:
        if summary.token_invalid:
            return "token 已失效，需要重新登录"
        if summary.port_conflict:
            return "3000 端口被占用，api 子进程未能启动"
        if summary.quota_used and summary.vip_count == 0:
            return "今日额度已用光（可能服务器版已先跑过）"
        if summary.errors:
            return "；".join(dict.fromkeys(summary.errors))[:200]
        if res.timed_out:
            return "执行超时被终止"
        if res.code != 0:
            return (res.tail(3) or res.error or f"退出码 {res.code}")[:200]
        return ""

    async def run_checkin(self, trigger: str = "手动", origin_umo: str = "") -> dict:
        if self._running:
            return {"ok": False, "message": "已有任务在运行，请稍候"}
        if not self.store.has_login():
            return {"ok": False, "message": "还没有登录态，先执行 /酷狗登录 扫码"}

        self._running = True
        self._run_started = time.time()
        self._progress.clear()
        record: dict = {}
        try:
            boot = await self.sidecar.ensure()
            if not boot.get("ok"):
                return {"ok": False, "message": "侧车未就绪：" + (boot.get("error") or "")}

            async with self.lock:
                busy, who = await self.sidecar.port_busy()
                if busy:
                    return {
                        "ok": False,
                        "message": f"127.0.0.1:3000 被占用（{who}），先结束该进程再签到",
                    }
                res = await self.sidecar.run_checkin(on_line=self._on_line)

            summary = parse_checkin(res.lines)
            ok = self._judge_ok(res, summary)
            accounts = self.store.accounts()
            record = {
                "ts": int(time.time()),
                "date": summary.date or today_str(),
                "ok": ok,
                "trigger": trigger,
                "account": summary.nickname or (accounts[0]["userid"] if accounts else ""),
                "listen": summary.listen,
                "vip_count": summary.vip_count,
                "vip_total": summary.vip_total,
                "vip_expire": summary.vip_expire,
                "exit_code": res.code,
                "duration": round(time.time() - self._run_started, 1),
                "error": self._error_text(res, summary),
                "quota_used": summary.quota_used,
                "token_invalid": summary.token_invalid,
                "log_tail": res.tail(20),
                "notify": {},
            }

            try:
                await self._notify(record, skip_umo=origin_umo)
            except Exception as e:
                logger.warning(f"[kgcheckin] 通知失败：{e}")
                record["notify"] = {"qq": 0, "qq_errors": [str(e)]}

            self.store.append_history(record)
            self.store.update_state(
                last_run_date=today_str(),
                last_run_ts=record["ts"],
                last_ok=ok,
                last_result={
                    "date": record["date"],
                    "ok": ok,
                    "account": record["account"],
                    "listen": record["listen"],
                    "vip_count": record["vip_count"],
                    "vip_expire": record["vip_expire"],
                    "error": record["error"],
                },
            )
            self.store.touch_tokens(self.store.read_userinfo())
            self._last_result = record
            return {"ok": ok, "record": record, "message": format_message(record)}
        finally:
            self._running = False

    async def _notify(self, record: dict, skip_umo: str = "") -> None:
        """只走 QQ：把结果推到 /酷狗绑定 记录的会话。

        触发本次运行的会话（origin_umo）不重复推——那边已经直接回了结果。
        单个会话失败只记原因，不影响其他会话，也不抛出。
        """
        text = format_message(record)
        qq_ok = 0
        qq_errors: list[str] = []

        for item in self.store.bindings():
            umo = item.get("umo", "")
            if not umo or umo == skip_umo:
                continue
            label = item.get("label") or umo
            try:
                sent = await self.context.send_message(umo, MessageChain([Comp.Plain(text)]))
                if sent:
                    qq_ok += 1
                else:
                    qq_errors.append(f"{label}: 没有匹配的平台")
            except Exception as e:
                qq_errors.append(f"{label}: {e}")

        if qq_errors:
            for line in qq_errors:
                logger.warning(f"[kgcheckin] QQ 推送失败 {line}")
        record["notify"] = {"qq": qq_ok, "qq_errors": qq_errors}

    # ---------------------------------------------------------------- 指令

    @filter.command("酷狗签到")
    async def cmd_checkin(self, event: AstrMessageEvent):
        if not self.store.has_login():
            yield event.plain_result("还没有登录态。先用 /酷狗登录 扫码，或 /酷狗登录 <手机号> 走验证码。")
            return
        if self._running:
            yield event.plain_result("已有任务在运行，等这次跑完。")
            return

        # 私聊里第一次签到就顺手绑上，省得用户再发一次 /酷狗绑定
        auto_bound = False
        if not self.store.bindings() and event.get_message_type() == MessageType.FRIEND_MESSAGE:
            auto_bound = self.store.add_binding(
                event.unified_msg_origin, event.get_sender_name() or "私聊"
            )

        yield event.plain_result("开始签到。单账号约 4 分钟（8 次领取每次间隔 30 秒），完成后回结果。")
        result = await self.run_checkin(
            trigger=f"指令:{event.get_sender_name() or event.get_sender_id() or '未知'}",
            origin_umo=event.unified_msg_origin,
        )
        if result.get("record"):
            yield event.plain_result(format_message(result["record"]))
            if auto_bound:
                yield event.plain_result("已自动把当前私聊设为推送目标，之后定时签到的结果也会发到这里。")
            elif not self.store.bindings():
                yield event.plain_result(
                    "提示：还没有绑定推送会话，定时签到的结果收不到。在当前 QQ 会话发 /酷狗绑定 即可。"
                )
        else:
            yield event.plain_result(result.get("message") or "签到失败")

    @filter.command("酷狗状态")
    async def cmd_status(self, event: AstrMessageEvent):
        yield event.plain_result(self.status_text())

    @filter.command("酷狗登录")
    async def cmd_login(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        arg1 = (arg1 or "").strip()
        arg2 = (arg2 or "").strip()

        if not arg1:
            yield event.plain_result(LOGIN_HELP)
            return

        if arg1 in ("扫码", "qr", "QR"):
            yield event.plain_result("正在生成二维码，约 5 秒…")
            result = await self.login.start_qr()
            if not result.get("ok"):
                yield event.plain_result(result.get("message") or "生成二维码失败")
                return
            b64 = str(result["image"]).split(",", 1)[-1]
            yield event.chain_result(
                [
                    Comp.Plain("用酷狗音乐 APP 扫码，约 2 分钟内有效。"),
                    Comp.Image.fromBase64(b64),
                ]
            )
            # 扫码结果后台轮询，成功了推一条
            self._spawn(self._watch_qr(result["qr_id"], event.unified_msg_origin))
            return

        if arg1.startswith("验证码"):
            code = arg1[3:].strip() or arg2
            result = await self.login.login_with_code(code)
            yield event.plain_result(result.get("message") or "登录失败")
            return

        if arg1.isdigit():
            result = await self.login.send_code(arg1)
            yield event.plain_result(result.get("message") or "发送失败")
            return

        yield event.plain_result(LOGIN_HELP)

    async def _watch_qr(self, qr_id: str, umo: str) -> None:
        for _ in range(80):
            await asyncio.sleep(3)
            status = self.login.qr_status(qr_id)
            if status["status"] == "confirmed":
                who = status.get("account") or ""
                await self.context.send_message(
                    umo, MessageChain([Comp.Plain(f"✅ 扫码登录成功，账号 {who}")])
                )
                return
            if status["status"] in ("expired", "failed"):
                msg = "二维码已过期，重新 /酷狗登录 扫码" if status["status"] == "expired" else (
                    status.get("message") or "扫码登录失败"
                )
                await self.context.send_message(umo, MessageChain([Comp.Plain(f"❌ {msg}")]))
                return

    @filter.command("酷狗绑定")
    async def cmd_bind(self, event: AstrMessageEvent):
        umo = event.unified_msg_origin
        added = self.store.add_binding(umo, event.get_sender_name() or umo)
        if added:
            yield event.plain_result(
                f"已把当前 QQ 会话设为推送目标：{umo}\n之后每次签到（含定时）结果都会发到这里。"
            )
        else:
            yield event.plain_result("当前会话已经在推送列表里。")

    @filter.command("酷狗历史")
    async def cmd_history(self, event: AstrMessageEvent, days: int = 7):
        days = max(1, min(90, int(days or 7)))
        rows = self.store.read_history(days=days, limit=30)
        if not rows:
            yield event.plain_result(f"最近 {days} 天没有运行记录。")
            return
        ok_count = sum(1 for r in rows if r.get("ok"))
        lines = [f"📅 最近 {days} 天：{len(rows)} 次，成功 {ok_count} 次"]
        for r in rows[:20]:
            mark = "✅" if r.get("ok") else "❌"
            ts = time.strftime("%m-%d %H:%M", time.localtime(r.get("ts", 0)))
            lines.append(
                f"{mark} {ts}  {r.get('vip_count', 0)}/{r.get('vip_total', 8)}  "
                f"到期 {r.get('vip_expire') or '未知'}"
            )
        yield event.plain_result("\n".join(lines))

    # ---------------------------------------------------------------- 文本

    def status_text(self) -> str:
        data = self.status_payload()
        lines = ["🎵 酷狗签到状态"]
        lines.append(f"酷狗ID：{data['account'] or '未登录'}")
        if data["claimed_today"]:
            when = time.strftime("%H:%M", time.localtime(data.get("last_run_ts") or 0))
            lines.append(f"今日：已领取（{when}）")
        elif data.get("last_run_date") == today_str():
            lines.append(f"今日：运行过但异常（{data.get('last_error') or '未知原因'}）")
        else:
            lines.append("今日：未运行")
        lines.append(f"VIP 到期：{data['vip_expire']}")
        lines.append(f"下次运行：{data['next_run']}")
        left = data.get("token_days_left")
        lines.append(
            f"Token：约 {left} 天（自首次记录起算）" if left is not None else "Token：未知"
        )
        lines.append(f"推送绑定：{data['bindings']} 个 QQ 会话" + ("" if data["bindings"] else "（发 /酷狗绑定 添加）"))
        return "\n".join(lines)

    def status_payload(self) -> dict:
        state = self.store.state()
        accounts = self.store.accounts()
        last = state.get("last_result") or {}
        today = today_str()

        tokens = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
        days_left = None
        for item in self.store.read_userinfo():
            seen = tokens.get(token_fingerprint(item.get("token", "")))
            if not seen:
                continue
            try:
                age = (now_cn().date() - date.fromisoformat(str(seen))).days
            except ValueError:
                continue
            left = max(0, 56 - age)
            days_left = left if days_left is None else min(days_left, left)

        return {
            "account": accounts[0]["userid"] if accounts else "",
            "accounts": accounts,
            "vip_expire": last.get("vip_expire") or "未知",
            "claimed_today": state.get("last_run_date") == today and bool(state.get("last_ok")),
            "last_run_date": state.get("last_run_date") or "",
            "last_run_ts": state.get("last_run_ts") or 0,
            "last_ok": state.get("last_ok"),
            "last_error": last.get("error") or "",
            "next_run": self.scheduler.next_run_text(),
            "token_days_left": days_left,
            "schedule_enabled": self.scheduler.enabled,
            "run_time": self.config.get("run_time") or "01:10",
            "running": self._running,
            "progress": list(self._progress)[-25:],
            "bindings": len(self.store.bindings()),
            "sidecar": {
                "ready": self.sidecar.is_ready(),
                "booting": self.sidecar.boot_running,
                "ok": self.sidecar.boot_ok,
                "error": self.sidecar.last_error,
                "dir": str(self.sidecar.code_dir),
                "log": self.sidecar.boot_log[-15:],
            },
            "server_time": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        }

    # ---------------------------------------------------------------- Web API

    def _register_web_apis(self) -> None:
        apis = [
            ("/status", self.api_status, ["GET"], "签到状态"),
            ("/run", self.api_run, ["POST"], "立即签到"),
            ("/login/phone", self.api_login_phone, ["POST"], "发送验证码"),
            ("/login/code", self.api_login_code, ["POST"], "验证码登录"),
            ("/login/qr", self.api_login_qr, ["POST"], "生成登录二维码"),
            ("/login/qr/<qr_id>", self.api_qr_status, ["GET"], "扫码状态"),
            ("/history", self.api_history, ["GET"], "运行历史"),
            ("/bindings", self.api_bindings, ["GET", "POST"], "推送绑定"),
            ("/settings", self.api_settings, ["GET", "POST"], "通知与定时设置"),
            ("/notify/test", self.api_notify_test, ["POST"], "测试通知"),
            ("/bootstrap", self.api_bootstrap, ["POST"], "初始化/重建侧车"),
            ("/log", self.api_log, ["GET"], "运行日志"),
        ]
        for route, handler, methods, desc in apis:
            try:
                self.context.register_web_api(f"/{PLUGIN_NAME}{route}", handler, methods, desc)
            except Exception as e:
                logger.warning(f"[kgcheckin] 注册 Web API {route} 失败：{e}")

    async def api_status(self):
        return self.status_payload()

    async def api_run(self):
        if self._running:
            return {"ok": False, "message": "正在运行"}
        if not self.store.has_login():
            return {"ok": False, "message": "还没有登录态，先完成登录"}
        self._spawn(self.run_checkin(trigger="面板"))
        return {"ok": True, "message": "已开始签到，约 4 分钟"}

    async def api_login_phone(self):
        body = await request.json(default={}) or {}
        return await self.login.send_code(str(body.get("phone") or ""))

    async def api_login_code(self):
        body = await request.json(default={}) or {}
        return await self.login.login_with_code(
            str(body.get("code") or ""), str(body.get("phone") or "")
        )

    async def api_login_qr(self):
        return await self.login.start_qr()

    async def api_qr_status(self, qr_id: str):
        return self.login.qr_status(qr_id)

    async def api_history(self):
        days = 7
        try:
            days = int(request.query.get("days") or 7)
        except Exception:
            pass
        return {"days": days, "items": self.store.read_history(days=days, limit=100)}

    async def api_bindings(self):
        if request.method == "POST":
            body = await request.json(default={}) or {}
            action = str(body.get("action") or "add").lower()
            umo = str(body.get("umo") or "").strip()
            if action == "remove":
                return {"ok": self.store.remove_binding(umo)}
            return {"ok": self.store.add_binding(umo, str(body.get("label") or umo))}
        return {"items": self.store.bindings()}

    async def api_settings(self):
        if request.method == "POST":
            body = await request.json(default={}) or {}
            try:
                if "enable_schedule" in body:
                    self.config["enable_schedule"] = bool(body["enable_schedule"])
                if "run_time" in body:
                    rt = str(body["run_time"] or "").strip()
                    if rt:
                        self.config["run_time"] = rt
                self.config.save_config()
                return {"ok": True, "message": "已保存"}
            except Exception as e:
                return {"ok": False, "message": f"保存失败：{e}"}

        return {
            "enable_schedule": bool(self.config.get("enable_schedule", False)),
            "run_time": self.config.get("run_time") or "01:10",
            "catch_up": bool(self.config.get("catch_up", True)),
            "bindings": self.store.bindings(),
            "next_run": self.scheduler.next_run_text(),
        }

    async def api_notify_test(self):
        record = self._last_result or {
            "date": today_str(),
            "ok": True,
            "account": (self.store.accounts()[0]["userid"] if self.store.accounts() else ""),
            "listen": "成功",
            "vip_count": 8,
            "vip_total": 8,
            "vip_expire": "（测试消息）",
            "trigger": "测试",
        }
        record = dict(record)
        record["trigger"] = "测试"
        text = format_message(record)

        qq_ok = 0
        errors: list[str] = []
        for item in self.store.bindings():
            try:
                if await self.context.send_message(
                    item["umo"], MessageChain([Comp.Plain(text)])
                ):
                    qq_ok += 1
                else:
                    errors.append(f"{item.get('label') or item['umo']}: 平台未匹配")
            except Exception as e:
                errors.append(f"{item.get('label') or item['umo']}: {e}")

        if not self.store.bindings():
            errors.append("还没有绑定 QQ 会话，先在 QQ 里发 /酷狗绑定")
        return {"ok": bool(qq_ok), "qq": qq_ok, "errors": errors}

    async def api_bootstrap(self):
        body = await request.json(default={}) or {}
        force = bool(body.get("force"))
        if self.sidecar.boot_running:
            return {"ok": False, "message": "正在初始化，请稍候"}
        self._spawn(self.sidecar.ensure(force=force))
        return {"ok": True, "message": "已开始初始化，进度见状态区"}

    async def api_log(self):
        return {
            "running": self._running,
            "lines": list(self._progress)[-120:],
            "last": self._last_result,
        }
