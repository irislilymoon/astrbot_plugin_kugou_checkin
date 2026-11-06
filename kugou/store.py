# -*- coding: utf-8 -*-
"""持久化层：userinfo / history / state / bindings。

设计约束：
- 全部写操作走「先写 .tmp 再 os.replace」，断电不会留半截文件；
- userinfo.json 存的是账号 token，权限尽力设为 600（Windows 上 chmod 只影响只读位，属正常）；
- history.jsonl 只追加，读取时按时间倒序，超期自动裁剪。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

CN_TZ = timezone(timedelta(hours=8))


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def today_str() -> str:
    return now_cn().strftime("%Y-%m-%d")


def mask_userid(userid) -> str:
    s = str(userid or "").strip()
    if not s:
        return "未知"
    if len(s) <= 4:
        return s[0] + "*" * (len(s) - 1)
    if len(s) <= 7:
        return s[:2] + "*" * (len(s) - 4) + s[-2:]
    return s[:3] + "*" * (len(s) - 5) + s[-2:]


def mask_token(token: str) -> str:
    token = str(token or "")
    if len(token) <= 10:
        return "****"
    return f"{token[:4]}****{token[-4:]}"


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()[:12]


class Store:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.userinfo_path = self.root / "userinfo.json"
        self.history_path = self.root / "history.jsonl"
        self.state_path = self.root / "state.json"
        self.bindings_path = self.root / "bindings.json"
        self.qr_dir = self.root / "qr"
        self.qr_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 基础读写 ----------

    @staticmethod
    def _read_json(path: Path, default):
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    @staticmethod
    def _write_atomic(path: Path, payload: str, mode: int | None = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        if mode is not None:
            try:
                os.chmod(path, mode)
            except OSError:
                pass

    # ---------- 登录态 ----------

    def read_userinfo(self) -> list[dict]:
        data = self._read_json(self.userinfo_path, [])
        if not isinstance(data, list):
            return []
        return [x for x in data if isinstance(x, dict) and x.get("token")]

    def write_userinfo(self, items: list[dict]) -> None:
        clean = [
            {"userid": x.get("userid"), "token": x.get("token")}
            for x in items
            if isinstance(x, dict) and x.get("token")
        ]
        self._write_atomic(
            self.userinfo_path,
            json.dumps(clean, ensure_ascii=False, indent=2),
        )

    def has_login(self) -> bool:
        return bool(self.read_userinfo())

    def accounts(self) -> list[dict]:
        out = []
        for item in self.read_userinfo():
            out.append(
                {
                    "userid": mask_userid(item.get("userid")),
                    "userid_raw": item.get("userid"),
                    "token": mask_token(item.get("token")),
                }
            )
        return out

    # ---------- 运行状态 ----------

    def state(self) -> dict:
        data = self._read_json(self.state_path, {})
        return data if isinstance(data, dict) else {}

    def update_state(self, **kwargs) -> dict:
        state = self.state()
        state.update(kwargs)
        self._write_atomic(
            self.state_path,
            json.dumps(state, ensure_ascii=False, indent=2),
        )
        return state

    def touch_tokens(self, userinfo: list[dict]) -> dict:
        """记录每个 token 首次出现时间，用于估算剩余有效期。

        上游每周日续期一次，token 从签发起约 8 周失效；这里只能给下限估算。
        """
        state = self.state()
        seen: dict = state.get("tokens") if isinstance(state.get("tokens"), dict) else {}
        today = today_str()
        changed = False
        for item in userinfo:
            fp = token_fingerprint(item.get("token", ""))
            if fp and fp not in seen:
                seen[fp] = today
                changed = True
        if changed:
            self.update_state(tokens=seen)
        return seen

    # ---------- 历史 ----------

    def append_history(self, record: dict) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        with self.history_path.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_history(self, days: int = 7, limit: int = 200) -> list[dict]:
        cutoff = time.time() - max(1, int(days)) * 86400
        rows: list[dict] = []
        try:
            with self.history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if float(rec.get("ts", 0)) >= cutoff:
                        rows.append(rec)
        except OSError:
            return []
        rows.sort(key=lambda r: float(r.get("ts", 0)), reverse=True)
        return rows[:limit]

    def prune_history(self, keep_days: int = 90) -> int:
        if not self.history_path.exists():
            return 0
        cutoff = time.time() - max(1, int(keep_days)) * 86400
        kept: list[str] = []
        dropped = 0
        try:
            with self.history_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        dropped += 1
                        continue
                    if float(rec.get("ts", 0)) >= cutoff:
                        kept.append(line)
                    else:
                        dropped += 1
        except OSError:
            return 0
        if dropped:
            self._write_atomic(self.history_path, "\n".join(kept) + ("\n" if kept else ""))
        return dropped

    # ---------- 推送绑定 ----------

    def bindings(self) -> list[dict]:
        data = self._read_json(self.bindings_path, [])
        return [x for x in data if isinstance(x, dict) and x.get("umo")] if isinstance(data, list) else []

    def add_binding(self, umo: str, label: str = "") -> bool:
        umo = str(umo or "").strip()
        if not umo:
            return False
        items = self.bindings()
        if any(x.get("umo") == umo for x in items):
            return False
        items.append({"umo": umo, "label": label or umo, "added_at": int(time.time())})
        self._write_atomic(
            self.bindings_path,
            json.dumps(items, ensure_ascii=False, indent=2),
        )
        return True

    def remove_binding(self, umo: str) -> bool:
        items = self.bindings()
        rest = [x for x in items if x.get("umo") != umo]
        if len(rest) == len(items):
            return False
        self._write_atomic(
            self.bindings_path,
            json.dumps(rest, ensure_ascii=False, indent=2),
        )
        return True
