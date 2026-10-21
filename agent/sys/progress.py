"""agent.sys.progress —— 运行进度桥接。

把一次运行期间的内核事件桥接成异步队列，供传输层消费。
与 HTTP 无关——传输适配在 ``agent/run/`` 层。

设计要点：
- 复用内核原语：``ctx.scope()``+ ``ctx.on(Events.X)``。
 不为流式/取消给内核新增任何概念。
- 只认 ``Events`` 常量，不 import 任何业务/插件 → 与 ReAct 零耦合；
 停用 react 时收不到 ``REACT_*`` 帧，属优雅降级。
- ``durable()`` 只落库不进总线，故 ``REACT_FINAL`` / ``REACT_EXHAUSTED`` 等订阅不到；
 最终文本由调用方 await 任务结果后作为最后一帧发出，本模块不需要它。

陷阱：子 ctx 的 ``state`` 不继承父级，故本模块只用子 ctx 订阅，
绝不把它交给业务执行——否则 ``budget_spent`` / ``permission_mode`` 会读到空。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from agent.api import Events


# 默认订阅：必须是总线可见的事件。durable-only 的订阅不到。
PROGRESS_EVENTS: tuple[str, ...] = (
    Events.REACT_STEP_START,
    Events.REACT_STEP_END,
    Events.TASKS_POST_EXECUTE,
    Events.LLM_TOKEN,                              # 打字机帧
    Events.LLM_THINKING,                           # 思考流帧
    Events.APPROVAL_PENDING,                       # 第 2 期：待审批弹窗信号
    Events.APPROVAL_ANSWERED,                      # 第 2 期：审批了结
    Events.ASK_USER_PENDING,                       # 澄清问询卡片
    Events.ASK_USER_ANSWERED,                      # 问询了结
    Events.TODO_PROGRESS,                          # 规划插件：完成度实时帧
)

# 内核事件名 → 传输层帧类型。
# LLM_TOKEN 映射为 "token" 帧，前端据此追加文本而非渲染步骤卡片。
# 第 2 期：approval 两帧独立成帧型，前端弹窗/消隐，不混入通用步骤卡片。
FRAME_TYPES: dict[str, str] = {
    Events.LLM_TOKEN: "token",
    Events.LLM_THINKING: "thinking",
    Events.APPROVAL_PENDING: "approval",
    Events.APPROVAL_ANSWERED: "approval_done",
    Events.ASK_USER_PENDING: "ask_user",
    Events.ASK_USER_ANSWERED: "ask_user_done",
    Events.TODO_PROGRESS: "todo",                  # 规划完成度帧
}


def _pusher(queue: asyncio.Queue, name: str):
    """构造同步推送器。

 S7：队列满→ 丢最旧一帧保新帧并计数，绝不阻断业务主链路。
 """
    ftype = FRAME_TYPES.get(name, "progress")

    def _push(*args: Any) -> None:
        first = args[0] if args else {}
        payload = first if isinstance(first, dict) else {"value": first}
        try:
            # 帧类型来自 FRAME_TYPES；内核事件名放 payload.source
            queue.put_nowait({"event": ftype,
                              "payload": {"source": name, "data": payload}})
        except asyncio.QueueFull:
            try:
                queue.get_nowait()                 # 丢最旧
                queue.put_nowait({"event": ftype,
                                  "payload": {"source": name, "data": payload}})
                queue.dropped = getattr(queue, "dropped", 0) + 1
            except Exception:  # noqa: BLE001 —— 丢帧策略自身的异常也绝不外泄
                pass
        except Exception:  # noqa: BLE001 —— 队列异常绝不阻断业务主链路
            pass

    return _push


@asynccontextmanager
async def tap_progress(ctx, *, queue: asyncio.Queue | None = None,
                       events: tuple[str, ...] = PROGRESS_EVENTS,
                       maxsize: int = 1024) -> AsyncIterator[asyncio.Queue]:
    """订阅内核事件 → 推入队列；退出作用域自动注销监听。

 ``maxsize``：自建队列的有界容量——token 高频流 + 慢消费者
 不再无限占内存；传入自定义 ``queue`` 时以其实际容量为准。
 """
    q = queue if queue is not None else asyncio.Queue(maxsize=maxsize)
    async with ctx.scope("progress") as sctx:
        for name in events:
            sctx.on(name, _pusher(q, name))
        yield q


__all__ = ["PROGRESS_EVENTS", "tap_progress"]
