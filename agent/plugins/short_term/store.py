"""agent.plugins.short_term.store —— 短期记忆内容层存储（会话历史 + 滚动摘要）。

职责（内容层，归短期记忆插件所有）：
- 对话历史 ``agent_messages`` 的读取（seq ASC，含完整原文——**截短/摘要都不改写它**）；
- 滚动摘要 ``agent_session_summaries`` 的读写（``covers_up_to_seq`` 标记覆盖到哪条）。

零特权：只经注入的 ``ctx.db``（SqliteDb），不碰内核内部、不依赖其它记忆层。
``token_count`` 写入时预估（chat 写入）；读时若为 NULL（旧消息）即时退回 ``estimate_tokens``，
保证预算计算始终有值。
"""

from __future__ import annotations

from typing import Any

from agent.api import estimate_tokens, now_iso


class SessionSummaryStore:
    """短期记忆内容存储：历史 + 摘要（均按 session_id 隔离）。

    **本类是 ``agent_messages`` / ``agent_session_summaries`` 两表的唯一归属者**：
    会话转写的写入、读取、清除一律经此，消费方（chat 等）不得自行拼 SQL 直连，
    以免"内容层 owner"形同虚设、多套实现漂移。
    """

    def __init__(self, db: Any):
        self._db = db

    # ---- 历史写入（唯一入口） ----

    async def append(self, ctx: Any, session_id: str, role: str, content: str) -> None:
        """追加一条会话转写（token_count 写入时预估，读时无需再算）。"""
        await self._db.execute(
            ctx, "INSERT INTO agent_messages"
                 " (session_id, role, content, at, token_count) VALUES (?,?,?,?,?)",
            (session_id, role, content, now_iso(), estimate_tokens(content)))

    # ---- 历史读取（原文始终完整） ----

    async def get_history(self, ctx: Any, session_id: str) -> list[dict]:
        """全量历史（seq ASC）：含 seq/role/content/token_count。

        token_count 为 NULL 时即时退回 ``estimate_tokens(content)``，保证预算有值。
        原文绝不在此被改写（截短/摘要走窗口内视图 / 独立摘要表）。
        """
        rows = await self._db.query(
            ctx, "SELECT seq, role, content, token_count FROM agent_messages "
                 "WHERE session_id=? ORDER BY seq ASC", (session_id,))
        out: list[dict] = []
        for r in rows:
            tk = r.get("token_count")
            if tk is None:
                tk = estimate_tokens(r["content"])
            out.append({"seq": r["seq"], "role": r["role"],
                        "content": r["content"], "token_count": int(tk)})
        return out

    async def get_original_messages(self, ctx: Any, session_id: str,
                                   start_seq: int, end_seq: int) -> list[dict]:
        """按需调取原文（审计 / 回溯）：完整、未截短。"""
        rows = await self._db.query(
            ctx, "SELECT seq, role, content FROM agent_messages "
                 "WHERE session_id=? AND seq BETWEEN ? AND ? ORDER BY seq ASC",
            (session_id, start_seq, end_seq))
        return [dict(r) for r in rows]

    # ---- 摘要读写 ----

    async def latest_summary(self, ctx: Any, session_id: str) -> str | None:
        """当前生效的滚动摘要文本（可能为 None）。"""
        rows = await self._db.query(
            ctx, "SELECT summary FROM agent_session_summaries WHERE session_id=?",
            (session_id,))
        return rows[0]["summary"] if rows else None

    async def get_summary_meta(self, ctx: Any, session_id: str) -> dict | None:
        """摘要元数据：version / covers_up_to_seq / tokens。"""
        rows = await self._db.query(
            ctx, "SELECT version, covers_up_to_seq, tokens FROM agent_session_summaries "
                 "WHERE session_id=?", (session_id,))
        return dict(rows[0]) if rows else None

    async def upsert_summary(self, ctx: Any, session_id: str, *,
                             summary: str, version: int,
                             covers_up_to_seq: int, tokens: int) -> None:
        """写摘要（version 显式递增，由调用方算好）。"""
        await self._db.execute(
            ctx,
            "INSERT INTO agent_session_summaries"
            " (session_id, summary, version, covers_up_to_seq, tokens, updated_at)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET"
            " summary=excluded.summary, version=excluded.version,"
            " covers_up_to_seq=excluded.covers_up_to_seq,"
            " tokens=excluded.tokens, updated_at=excluded.updated_at",
            (session_id, summary, version, covers_up_to_seq, tokens, now_iso()))

    async def mark_compressed(self, ctx: Any, session_id: str, up_to_seq: int) -> None:
        """标记历史被覆盖：``compressed_into`` 统一指向最新 covers_up_to_seq。"""
        await self._db.execute(
            ctx, "UPDATE agent_messages SET compressed_into=? "
                 "WHERE session_id=? AND seq <= ? AND compressed_into IS NULL",
            (up_to_seq, session_id, up_to_seq))

    # ---- 历史读取：对外视图（chat 等消费方经此，不直连 SQL） ----

    async def history(self, ctx: Any, session_id: str, limit: int | None = None) -> list[dict]:
        """会话转写（seq ASC，含 seq/role/content/at）。limit 为 None 表示不限。"""
        sql = ("SELECT seq, role, content, at FROM agent_messages "
               "WHERE session_id=? ORDER BY seq ASC")
        args: list = [session_id]
        if limit is not None:
            sql += " LIMIT ?"
            args.append(limit)
        rows = await self._db.query(ctx, sql, tuple(args))
        return [dict(r) for r in rows]

    async def recent(self, ctx: Any, session_id: str, limit: int) -> list[dict]:
        """最近 N 条（返回仍按 seq ASC，便于直接当上下文序列）。"""
        rows = await self._db.query(
            ctx, "SELECT seq, role, content, at FROM agent_messages WHERE session_id=?"
                 " ORDER BY seq DESC LIMIT ?", (session_id, limit))
        return [dict(r) for r in reversed(rows)]

    # ---- 会话清除（级联摘要，避免摘要残留注入已删历史） ----

    async def delete_session(self, ctx: Any, session_id: str) -> int:
        """清除会话：历史 + 滚动摘要**同一事务**，返回删除的历史条数。

        若不级联清摘要，``reset`` 后下一轮仍会把"已被删掉的历史"的前情注入模型。
        """
        rows = await self._db.query(
            ctx, "SELECT COUNT(*) AS n FROM agent_messages WHERE session_id=?", (session_id,))
        n = int(rows[0]["n"]) if rows else 0
        async with self._db.transaction(ctx):
            await self._db.execute(
                ctx, "DELETE FROM agent_messages WHERE session_id=?", (session_id,))
            await self._db.execute(
                ctx, "DELETE FROM agent_session_summaries WHERE session_id=?", (session_id,))
        return n

    # ---- 审计 ----

    async def count_history(self, ctx: Any, session_id: str) -> int:
        rows = await self._db.query(
            ctx, "SELECT COUNT(*) AS n FROM agent_messages WHERE session_id=?", (session_id,))
        return int(rows[0]["n"]) if rows else 0


__all__ = ["SessionSummaryStore"]
