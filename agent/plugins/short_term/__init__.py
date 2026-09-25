"""插件包：short_term —— 短期记忆（第 4 层，内容层）。

目录插件（agent/plugins/short_term），与长期记忆 ``plugins.memory`` 平级；
经「插件」页可启用 / 停用 / 卸载。语义上是**内容层**——独占对话历史
（``agent_messages`` 存储 + 检索 + 对自己内容的滚动摘要压缩），
与调度层 ``plugins.context``（ContextGovernor）严格解耦：本插件**咨询**治理器的
``pick_history`` 决定"窗口留了哪些"，再压缩其余，不拥有任何调度决策。

依赖方向（单向）：short_term → context（compactor 经 ``ctx.get(Services.CONTEXT)`` 软依赖）；
反向不成立。治理器 / 本插件任一停用，消费方（chat）软降级，不裸崩。

解耦纪律：
- 消费面一律 ``ctx.has(Services.SHORT_TERM)`` 软依赖——停用即退回 ``HISTORY_LIMIT`` 硬裁；
- 表随内核 ``agent.tables`` 统一建（agent_011 摘要表 / agent_012 扩列），幂等；
- 零特权：只经注入的 ``ctx.db`` 与 ``ctx.llm``，不碰内核内部。
"""

from __future__ import annotations

from agent.api import Services, plugin

from .compactor import HistoryCompactor
from .facade import ShortTermMemory
from .store import SessionSummaryStore


@plugin
class ShortTerm:
    """ctx.short_term —— 短期记忆内容层（会话历史 + 滚动摘要压缩）。"""

    name = "plugins.short_term"
    version = "0.1.0"
    layer = "system"
    provides = [Services.SHORT_TERM]
    inject = ["db"]               # 声明依赖 Storage（确保先 apply，db 可用）
    tables = ["agent_011_session_summaries", "agent_012_message_fields"]
    description = "短期记忆（第4层）：对话历史存储 + 滚动摘要压缩（内容层，与调度层解耦）"

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("short_term") or {}
        store = SessionSummaryStore(ctx.db)
        compactor = HistoryCompactor(store, cfg)
        # 门面：会话转写 + 摘要 + 压缩 统一出口（消费方零 SQL）
        ctx.provide(Services.SHORT_TERM, ShortTermMemory(store, compactor))


__all__ = ["ShortTerm", "ShortTermMemory", "HistoryCompactor", "SessionSummaryStore"]
