"""agent.core.audit —— durable 事件落盘（审计脊柱；D4 自 sys/audit 转正入内核公共面）。

三域约定：durable 事件写 agent_events（可跨重启）；live/capability 走 EventBus 内存。
payload 一律纯数据 dict（JSON 序列化）。

转正原因（D4）：S7 后 7+ 模块（sys 四件 + react/memory 等）依赖本能力，``sys.audit``
事实成为公共面；而 ``sys/audit`` 顶层 ``from agent.api import Services``——api 再反向
re-export 即循环 import，且 K0 守卫禁止 api 出现指向 sys 包的 import 字面量。故实现下沉 core
（对 Services 的依赖改走 ``agent.core.services_catalog``，先例 events_catalog → errors），
``agent.api`` 以 ``durable`` / ``now_iso`` 转正导出，``sys/audit.py`` 留兼容别名。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from agent.core.services_catalog import Services


def now_iso() -> str:
    """本地时间 ISO 串（秒级；审计/落库时间戳统一口径）。"""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


async def durable(ctx: Any, event: str, payload: dict) -> None:
    """写一条 durable 事件。无 db（纯内存装配）→ 不落盘也不假装（诚实）。"""
    db = ctx.get(Services.DB)
    if db is None:
        return
    try:
        await db.execute(
            ctx, "INSERT INTO agent_events(event, payload, at) VALUES (?,?,?)",
            (event, json.dumps(payload, ensure_ascii=False), now_iso()),
        )
    except Exception as exc:  # noqa: BLE001 —— 审计写失败不阻断主流程（记入日志）
        import logging
        logging.getLogger("agent.audit").warning("durable event %r 落盘失败: %s", event, exc)


__all__ = ["durable", "now_iso"]
