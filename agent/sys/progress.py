"""agent.sys.progress —— 运行进度桥接（B5 流式 / B6 取消 的公共底座）。

把一次运行期间的**内核事件**桥接成异步队列，供传输层（SSE / CLI / WebSocket）消费。
**与 HTTP 无关**——传输适配在 ``agent/run/`` 层。

设计要点（详见《B5B6-流式与取消-设计方案.md》）：
- 复用内核原语：``ctx.scope()``（作用域退出即自动注销监听）+ ``ctx.on(Events.X)``（事件总线）。
  **不为流式/取消给内核新增任何概念**。
- 只认 ``Events`` 常量（P1 的 SSOT），不 import 任何业务/插件 → **与 ReAct 零耦合**；
  停用 react 时收不到 ``REACT_*`` 帧，属**优雅降级**（没有中间过程，但不报错）。
- ``durable()`` 只落库不进总线，故 ``REACT_FINAL`` / ``REACT_EXHAUSTED`` 等**订阅不到**；
  最终文本由调用方 await 任务结果后作为最后一帧发出，本模块不需要它。

⚠️ 陷阱：子 ctx 的 ``state`` **不继承父级**（core/context.py），故本模块**只用子 ctx 订阅**，
绝不把它交给业务执行——否则 ``budget_spent`` / ``permission_mode`` 会读到空。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from agent.api import Events


# 默认订阅：必须是**总线可见**的事件（emit）。durable-only 的订阅不到。
PROGRESS_EVENTS: tuple[str, ...] = (
    Events.REACT_STEP_START,
    Events.REACT_STEP_END,
    Events.TASKS_POST_EXECUTE,
)


def _pusher(queue: asyncio.Queue, name: str):
    """构造同步推送器（emit 对同步监听器内联执行，无额外任务开销）。"""

    def _push(*args: Any) -> None:
        first = args[0] if args else {}
        payload = first if isinstance(first, dict) else {"value": first}
        try:
            # 帧类型统一为 "progress"；内核事件名放 payload.source（传输层无需知道事件名）
            queue.put_nowait({"event": "progress",
                              "payload": {"source": name, "data": payload}})
        except Exception:      # noqa: BLE001 —— 队列异常绝不阻断业务主链路
            pass

    return _push


@asynccontextmanager
async def tap_progress(ctx, *, queue: asyncio.Queue | None = None,
                       events: tuple[str, ...] = PROGRESS_EVENTS) -> AsyncIterator[asyncio.Queue]:
    """订阅内核事件 → 推入队列；退出作用域**自动注销**监听（scope effect 对称回收）。

    用法：:

        async with tap_progress(ctx) as q:
            task = asyncio.create_task(chat.send(...))   # 必须在订阅之后再起任务，防漏帧
            frame = await q.get()
    """
    q = queue if queue is not None else asyncio.Queue()
    async with ctx.scope("progress") as sctx:
        for name in events:
            sctx.on(name, _pusher(q, name))
        yield q


__all__ = ["PROGRESS_EVENTS", "tap_progress"]
