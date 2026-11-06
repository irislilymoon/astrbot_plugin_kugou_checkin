# -*- coding: utf-8 -*-
"""每日定时与补跑。

不用系统 cron：本机是 Windows，AstrBot 进程内 asyncio 循环最省事，也便于面板开关。
时区固定 UTC+8（中国无夏令时），不依赖 Windows 是否装了 tzdata。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from .store import now_cn, today_str

DEFAULT_TIME = "01:10"


def parse_hhmm(value: str) -> tuple[int, int]:
    try:
        hh, mm = str(value or "").strip().split(":", 1)
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except (ValueError, AttributeError):
        pass
    return 1, 10


class Scheduler:
    def __init__(self, plugin, config, store, log) -> None:
        self.plugin = plugin
        self.config = config
        self.store = store
        self.log = log
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._skip_date = ""

    @property
    def enabled(self) -> bool:
        return bool(self.config.get("enable_schedule", False))

    def _target(self, day: datetime) -> datetime:
        h, m = parse_hhmm(self.config.get("run_time") or DEFAULT_TIME)
        return day.replace(hour=h, minute=m, second=0, microsecond=0)

    def next_run(self) -> datetime | None:
        if not self.enabled:
            return None
        now = now_cn()
        target = self._target(now)
        if now >= target:
            target = self._target(now + timedelta(days=1))
        return target

    def next_run_text(self) -> str:
        nxt = self.next_run()
        if not nxt:
            return "未开启"
        now = now_cn()
        delta = nxt - now
        if delta < timedelta(hours=1):
            return f"{nxt:%H:%M}（{max(1, int(delta.total_seconds() // 60))} 分钟后）"
        return nxt.strftime("%m-%d %H:%M")

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        # 首轮先睡 15 秒，等插件加载完、平台连接稳定
        await asyncio.sleep(15)
        first = True
        while not self._stop.is_set():
            try:
                await self._tick(first=first)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.log.warning(f"[kgcheckin] 定时检查异常：{e}")
            first = False
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=20)
            except asyncio.TimeoutError:
                pass

    async def _tick(self, first: bool) -> None:
        if not self.enabled:
            return
        now = now_cn()
        target = self._target(now)
        if now < target:
            return
        today = today_str()
        if self.store.state().get("last_run_date") == today:
            return

        if first and not bool(self.config.get("catch_up", True)):
            self._skip_date = today
            self.log.info("[kgcheckin] 已过今日执行时间且未开启补跑，跳过")
            return
        if self._skip_date == today:
            return

        self.log.info(f"[kgcheckin] 定时触发签到（{self.config.get('run_time') or DEFAULT_TIME}）")
        await self.plugin.run_checkin(trigger="定时")
