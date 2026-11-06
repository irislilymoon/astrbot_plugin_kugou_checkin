# -*- coding: utf-8 -*-
"""结果格式化。

只做 QQ 推送：本插件跑在 AstrBot 里，通知就该走 AstrBot 已连接的 QQ 平台，
不接企业微信/Server酱/邮件这类外部渠道。推送目标由 /酷狗绑定 写入 bindings.json。
"""

from __future__ import annotations


def format_title(rec: dict) -> str:
    ok = rec.get("ok")
    return f"酷狗签到{'成功' if ok else '异常'} {rec.get('date') or ''}".strip()


def format_message(rec: dict) -> str:
    lines = [f"🎵 {format_title(rec)}"]
    if rec.get("account"):
        lines.append(f"👤 账号：{rec['account']}")
    lines.append(f"🎧 听歌：{rec.get('listen') or '未知'}")
    lines.append(f"🎁 VIP：{rec.get('vip_count', 0)}/{rec.get('vip_total', 8)} 次")
    lines.append(f"⏰ 到期：{rec.get('vip_expire') or '未知'}")
    if rec.get("error"):
        lines.append(f"⚠️ 错误：{rec['error']}")
    if rec.get("trigger"):
        lines.append(f"⚙️ 触发：{rec['trigger']}")
    return "\n".join(lines)
