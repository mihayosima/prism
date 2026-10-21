"""durable 事件落盘并提供统一时间戳 now_iso。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from agent.core.obs import inc_counter, trace_fields
from agent.core.services_catalog import Services


def now_iso() -> str:
    """本地时间 ISO 串。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


async def durable(ctx: Any, event: str, payload: dict) -> None:
    """写一条 durable 事件到 agent_events；无 db 时不落盘。"""
    db = ctx.get(Services.DB)
    if db is None:
        return
    fields = trace_fields(ctx)
    try:
        await db.execute(
            ctx,
            "INSERT INTO agent_events(event, payload, at, request_id, run_id, session)"
            " VALUES (?,?,?,?,?,?)",
            (event, json.dumps(payload, ensure_ascii=False), now_iso(),
             fields["rid"], fields["run"], fields["session"]),
        )
        inc_counter("agent_events_total")
    except Exception as exc:  # noqa: BLE001 —— 写失败不阻断主流程
        inc_counter("agent_event_write_errors_total")
        import logging
        logging.getLogger("agent.audit").warning("durable event %r 落盘失败: %s", event, exc)


__all__ = ["durable", "now_iso"]
