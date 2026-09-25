"""agent.plugins.short_term.facade —— ``ctx.short_term`` 门面（短期记忆唯一对外接口）。

为什么需要门面：
- 内容层有两块职责——**存储**（store：会话转写 + 摘要）与**压缩**（compactor：本地截短
  + LLM 摘要）。二者都归短期记忆，但对消费方（chat）应只暴露**一个**对象，
  否则消费方得知道"读历史找 store、压缩找 compactor"，等于把内部分工泄漏出去。
- ``agent_messages`` 表的写入/读取/清除**只在本门面之后**，消费方零 SQL，
  数据层归属才算真正收口。

降级：本插件是目录插件（opt-in），未装配时消费方走各自兜底（历史为空 /
``HISTORY_LIMIT`` 硬裁），不裸崩——见 ``sys/chat.py``。
"""

from __future__ import annotations

from typing import Any

from .compactor import HistoryCompactor
from .store import SessionSummaryStore


class ShortTermMemory:
    """短期记忆内容层门面：会话转写 + 滚动摘要 + 对自己内容的压缩。"""

    def __init__(self, store: SessionSummaryStore, compactor: HistoryCompactor):
        self._store = store
        self._compactor = compactor

    # ---- 会话转写（agent_messages 唯一入口） ----

    async def append(self, ctx: Any, session_id: str, role: str, content: str) -> None:
        await self._store.append(ctx, session_id, role, content)

    async def history(self, ctx: Any, session_id: str, limit: int = 100) -> list[dict]:
        return await self._store.history(ctx, session_id, limit)

    async def recent(self, ctx: Any, session_id: str, limit: int) -> list[dict]:
        return await self._store.recent(ctx, session_id, limit)

    async def reset(self, ctx: Any, session_id: str) -> int:
        """清除会话（历史 + 摘要级联），返回删除的历史条数。"""
        return await self._store.delete_session(ctx, session_id)

    async def count(self, ctx: Any, session_id: str) -> int:
        return await self._store.count_history(ctx, session_id)

    # ---- 内容层读接口（构造模型上下文用） ----

    async def get_history(self, ctx: Any, session_id: str) -> list[dict]:
        """全量历史（seq ASC，含 token_count），原文完整。"""
        return await self._store.get_history(ctx, session_id)

    async def latest_summary(self, ctx: Any, session_id: str) -> str | None:
        return await self._store.latest_summary(ctx, session_id)

    async def get_original_messages(self, ctx: Any, session_id: str,
                                    start_seq: int, end_seq: int) -> list[dict]:
        return await self._store.get_original_messages(ctx, session_id, start_seq, end_seq)

    # ---- 压缩（内容层对自己内容的管理） ----

    def truncate_view(self, messages: list[dict]) -> list[dict]:
        """窗口内本地截短（仅老内容、可逆、不改原文）。"""
        return self._compactor.truncate_view(messages)

    async def maybe_compress(self, ctx: Any, session_id: str) -> None:
        await self._compactor.maybe_compress(ctx, session_id)

    # ---- 诊断 ----

    @property
    def backoff(self):
        """退避状态机（可观测：诊断 / 测试可查暂停与计数）。"""
        return self._compactor.backoff

    def stats(self) -> dict:
        return self._compactor.stats()


__all__ = ["ShortTermMemory"]
