"""插件包：short_term —— 短期记忆（第 4 层，内容层）。

目录插件（agent/plugins/short_term），与长期记忆 ``plugins.memory`` 平级；
经「插件」页可启用 / 停用 / 卸载。语义上是**内容层**——独占对话历史
（``agent_messages`` 存储 + 检索 + 对自己内容的滚动摘要压缩），
与调度层 ``plugins.context``（ContextGovernor）解耦：本插件**不再咨询**治理器的
``pick_history``（收窗机制已移除）；压缩候选取全部未摘要历史，时机由自身
``compact_trigger_tokens`` 决定，不拥有任何调度决策。

依赖方向：短期记忆内容层自洽；``pick_history`` 保留在治理器内供未来 G1 分块装配复用，
当前无跨插件调用。任一插件停用，消费方（chat）软降级，不裸崩。

解耦纪律：
- 消费面一律 ``ctx.has(Services.SHORT_TERM)`` 软依赖——停用即退回 ``HISTORY_LIMIT`` 硬裁；
- 表随内核 ``agent.tables`` 统一建（agent_011 摘要表 / agent_012 扩列），幂等；
- 零特权：只经注入的 ``ctx.db`` 与 ``ctx.llm``，不碰内核内部。
"""

from __future__ import annotations

from agent.api import Services, inc_counter, plugin
from agent.tables import MESSAGE_FIELDS
import logging

_logger = logging.getLogger("plugins.short_term")

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
    inject = [Services.DB]        # 声明依赖 Storage（确保先 apply，db 可用；SSOT 常量）
    tables = ["agent_011_session_summaries", "agent_012_message_fields"]
    description = "短期记忆（第4层）：对话历史存储 + 滚动摘要压缩（内容层，与调度层解耦）"

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("short_term") or {}
        store = SessionSummaryStore(ctx.db)
        compactor = HistoryCompactor(store, cfg)
        # 门面：会话转写 + 摘要 + 压缩 统一出口（消费方零 SQL）
        ctx.provide(Services.SHORT_TERM, ShortTermMemory(store, compactor))
        self._store = store
        # 6.3（2026-10-04）：agent_messages 扩展列容错补建（与 BUDGET_LOG_FIELDS 同款）。
        # 定义归口 agent/tables.py::MESSAGE_FIELDS；此处幂等 ALTER——列已存在即跳过。
        # duplicate column 属正常幂等路径，不计失败；真失败必须可见（对齐自有纪律）。
        for col, decl in MESSAGE_FIELDS:
            try:
                await ctx.db.execute(
                    ctx, f"ALTER TABLE agent_messages ADD COLUMN {col} {decl}", ())
            except Exception as exc:                     # noqa: BLE001 —— 列已存在/表未建等
                if "duplicate column" not in str(exc).lower():
                    inc_counter("agent_messages_column_backfill_failed_total")
                    _logger.warning("agent_messages 补列 %s 失败 ⇒ 该列将缺失: %s", col, exc)

    async def status(self, ctx) -> dict:
        """运行态：转写与摘要各有多少条（此前本插件无 ``status()``，插件页一片空白）。

        只报**总量**不带会话明细：``status`` 是插件级视角，会话级数字在
        ``/api/context`` 与 SSE 帧里已经另有出口，这里不重复第二份真相。
        """
        store = getattr(self, "_store", None)
        if store is None or not ctx.has(Services.DB):
            return {"error": "未装配（db 或 store 缺失）"}
        try:
            msgs = await ctx.db.query(ctx, "SELECT COUNT(*) AS n FROM agent_messages")
            sums = await ctx.db.query(ctx, "SELECT COUNT(*) AS n FROM agent_session_summaries")
        except Exception as exc:                       # noqa: BLE001 —— 自述失败不拖垮整批
            return {"error": str(exc)}
        return {
            "messages": int(msgs[0]["n"]) if msgs else 0,
            "summaries": int(sums[0]["n"]) if sums else 0,
        }


__all__ = ["ShortTerm", "ShortTermMemory", "HistoryCompactor", "SessionSummaryStore"]
