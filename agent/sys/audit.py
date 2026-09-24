"""agent.sys.audit —— durable 事件落盘（审计脊柱）。

三域约定：durable 事件写 agent_events（可跨重启）；live/capability 走 EventBus 内存。
payload 一律纯数据 dict（JSON 序列化）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from agent.api import Services


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


async def durable(ctx: Any, event: str, payload: dict) -> None:
    """写一条 durable 事件。无 db（纯内存装配）→ 不落盘也不假装（诚实）。"""
    db = ctx.get(Services.DB)
    if db is None:
        return
    try:
        await db.execute(
            ctx, "INSERT INTO agent_events(event, payload, at) VALUES (?,?,?)",
            (event, json.dumps(payload, ensure_ascii=False), _now_iso()),
        )
    except Exception as exc:  # noqa: BLE001 —— 审计写失败不阻断主流程（记入日志）
        import logging
        logging.getLogger("agent.audit").warning("durable event %r 落盘失败: %s", event, exc)


__all__ = ["durable", "_now_iso"]