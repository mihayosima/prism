"""agent.plugins.short_term.compactor —— 短期记忆内容层压缩编排（HistoryCompactor）。

职责（内容层，归短期记忆插件所有）：
- **对自己内容的压缩**：本地截短（第一层，窗口内可逆）+ LLM 摘要（第二层，软上限兜底）；
- **覆盖范围对齐**：摘要覆盖「所有不在 kept 里、且未被旧摘要覆盖」的历史（covers_up_to_seq），
  避免窗口滑动丢数据；
- **失败处理**：递增退避（§8）+ 按原因分类 + 恢复探测（探测失败只续期）+ 降级路径
  （LLM 失败→仅本地截短；本地截短失败→不截短喂原文；插件/治理器停用→HISTORY_LIMIT 硬裁）。

严格不在本模块做"调度决策"（收窗 = ``ctx.get(Services.CONTEXT).pick_history`` 是治理器的事）；
本模块只决定"被丢弃的历史怎么凝练成前情"。
"""

from __future__ import annotations

import logging
from typing import Any


from agent.api import estimate_tokens, Services

from .backoff import BackoffState
from .truncate import compress_summary_to_budget, truncate_messages

logger = logging.getLogger("agent.short_term.compactor")

_PROMPT = (
    "你是会话压缩器。把以下对话压缩成要点式摘要，按下述五类分节（用 ### 标题）：\n"
    "### 反馈/纠正\n### 未决问题\n### 目标/意图\n### 事实/决策\n### 关键实体\n"
    "不要保留：寒暄、试错过程、重复内容。\n"
    "预算：总软上限 ≤{soft} token；五类各有独立预算，单类超限先内部要点化；"
    "总超 {soft} 时从最低优先级（关键实体→事实→目标）开始压缩；"
    "反馈与未决受保护不可牺牲。\n"
    "若已有旧摘要，请在旧摘要基础上**增量更新**（保留仍有效的条目），不要丢弃旧摘要中的有效信息。\n"
    "输入：\n[旧摘要]\n{old}\n[本轮被裁历史]\n{hist}\n"
)


class HistoryCompactor:
    """短期记忆压缩器：``maybe_compress(ctx, session_id)`` 异步触发。"""

    def __init__(self, store: Any, cfg: dict | None = None, backoff: BackoffState | None = None):
        self._store = store
        self._cfg = cfg or {}
        self._backoff = backoff or BackoffState()
        self._warmup = 0           # 恢复后连续成功计数（§8.5 渐暖）

    # ---- 配置 ----

    def _threshold(self) -> int:
        cw = int(self._cfg.get("context_window", 300000))
        fb = int(self._cfg.get("fixed_blocks", 80000))
        cb = int(self._cfg.get("compact_buffer", 13000))
        return max(0, (cw - fb) - cb)

    def _budgets(self) -> dict[str, int]:
        return {
            "feedback": int(self._cfg.get("summary_budget_feedback", 150)),
            "unresolved": int(self._cfg.get("summary_budget_unresolved", 120)),
            "goal": int(self._cfg.get("summary_budget_goal", 120)),
            "facts": int(self._cfg.get("summary_budget_facts", 130)),
            "entity": int(self._cfg.get("summary_budget_entity", 80)),
        }

    def _min_messages(self) -> int:
        return int(self._cfg.get("min_messages_to_summarize", 4))

    # ---- 对外：压缩（带顶层失败粒度保护） ----

    async def maybe_compress(self, ctx: Any, session_id: str) -> None:
        """异步触发压缩。仅**执行异常**算失败（记入退避）；无内容可摘要/未触发不计。"""
        try:
            await self._try_compress(ctx, session_id)
        except Exception as exc:                 # execution 异常（DB/意外）→ 按原因分类退避
            reason = self._classify(exc)
            tier = self._backoff.record_failure(reason)
            logger.warning("short_term compress error (%s): %s; backoff tier=%sm", reason, exc, tier)

    def _classify(self, exc: Exception) -> str:
        name = type(exc).__name__
        if "Database" in name or "Sqlite" in name:
            return "db_error"
        return "unknown"

    # ---- 内部：压缩主流程 ----

    async def _try_compress(self, ctx: Any, session_id: str) -> None:
        raw = await self._store.get_history(ctx, session_id)
        if len(raw) < self._min_messages():
            return                                     # 短历史：不算失败
        meta = await self._store.get_summary_meta(ctx, session_id)
        covers = int(meta["covers_up_to_seq"]) if meta else 0

        # 调度层收窗（短期记忆**咨询**治理器，不拥有该决策）
        governor = ctx.get(Services.CONTEXT)
        if governor is not None and hasattr(governor, "pick_history"):
            budget = governor.history_budget()
            keep_first = bool(self._cfg.get("keep_first", True))
            kept, _ = governor.pick_history(raw, budget, keep_first=keep_first)
        else:
            kept = raw                                   # 无治理器：全部 kept，不压缩
        kept_seqs = {m["seq"] for m in kept}

        # 被裁历史 = 未被 kept 且未被旧摘要覆盖
        to_sum = [m for m in raw if m["seq"] > covers and m["seq"] not in kept_seqs]
        if len(to_sum) < self._min_messages():
            return                                     # 没多少可压：不算失败

        # 第一层：本地截短（内容层凝练，窗口内可逆；对 to_sum 估算 + LLM 输入凝练）
        to_sum_view = truncate_messages(
            to_sum,
            recent_rounds=0,                            # to_sum 全为老内容，全部可截
            tool_threshold=int(self._cfg.get("tool_result_max_tokens", 6000)),
            file_threshold=int(self._cfg.get("file_content_max_tokens", 12000)),
        )
        view_tokens = sum(estimate_tokens(m["content"]) for m in to_sum_view)
        if view_tokens < self._threshold():
            return                                     # 本地截短后很小：无需 LLM，不算失败

        # 第二层：LLM 摘要（受退避保护；暂停期先轻量探测）
        old_summary = await self._store.latest_summary(ctx, session_id)
        llm_text = await self._maybe_llm_summary(ctx, old_summary, to_sum_view)
        if llm_text is None:
            return                                     # LLM 不可用/失败 → 降级（仅本地截短已生效），不算失败

        # 优先级预算兜底压缩
        new_summary = compress_summary_to_budget(llm_text, self._budgets(),
                                                int(self._cfg.get("summary_max_tokens", 600)))
        new_tokens = estimate_tokens(new_summary)
        up_to = max(m["seq"] for m in to_sum)
        version = (int(meta["version"]) if meta else 0) + 1

        # 原子写：摘要 + 标记 compressed_into（同一事务，避免"标记了但没摘要"）
        async with ctx.db.transaction(ctx):
            await self._store.upsert_summary(
                ctx, session_id, summary=new_summary, version=version,
                covers_up_to_seq=up_to, tokens=new_tokens)
            await self._store.mark_compressed(ctx, session_id, up_to)

        self._warmup += 1
        logger.info("short_term: compressed session=%s seq<= %d → v%d (%d tok)",
                    session_id, up_to, version, new_tokens)

    async def _maybe_llm_summary(self, ctx: Any, old_summary: str | None,
                                 to_sum_view: list[dict]) -> str | None:
        """第二层 LLM 摘要。受 llm_error 退避保护；暂停期先轻量探测，探测成功才继续。"""
        if ctx.get(Services.LLM) is None:
            return None                                 # 无 LLM → 降级
        if not self._backoff.should_attempt("llm_error"):
            # 暂停期：轻量探测（合成最小负载），成功→清零继续；失败→续期并降级
            if not await self._probe_llm(ctx):
                self._backoff.record_probe_failure("llm_error")
                return None
            self._backoff.record_probe_success("llm_error")
            self._warmup = 0
        hist = "\n".join(f"[{m['role']}] {m['content']}" for m in to_sum_view)
        prompt = _PROMPT.format(soft=int(self._cfg.get("summary_max_tokens", 600)),
                                old=old_summary or "（无）", hist=hist[:20000])
        messages = [{"role": "user", "content": prompt}]
        try:
            res = await ctx.llm.complete(ctx, messages, caller="agent.short_term")
            text = (res.data.get("text") if isinstance(res.data, dict) else str(res.data)) or ""
            text = text.strip()
            if not text:
                return None
            return text
        except Exception as exc:                         # LLM 执行异常 → 退避 + 降级
            self._backoff.record_failure("llm_error")
            logger.warning("short_term: LLM summary failed (%s); degrade to local-truncate-only", exc)
            return None

    async def _probe_llm(self, ctx: Any) -> bool:
        """恢复探测：轻量合成调用，成功即视为该原因已恢复。"""
        try:
            await ctx.llm.complete(ctx, [{"role": "user", "content": "ping"}],
                                  caller="agent.short_term.probe")
            return True
        except Exception:                                # noqa: BLE001
            return False

    # ---- 对外：窗口内本地截短（prior 构造时调用，内容层） ----

    def truncate_view(self, messages: list[dict]) -> list[dict]:
        """prior 构造时对 kept 里的老内容做窗口内本地截短（可逆，不改原文）。"""
        return truncate_messages(
            messages,
            recent_rounds=int(self._cfg.get("truncate_recent_rounds", 3)),
            tool_threshold=int(self._cfg.get("tool_result_max_tokens", 6000)),
            file_threshold=int(self._cfg.get("file_content_max_tokens", 12000)),
        )

    # ---- 内容层读接口（委托 store，供 chat 构造 prior / 回溯） ----

    async def get_history(self, ctx: Any, session_id: str) -> list[dict]:
        return await self._store.get_history(ctx, session_id)

    async def latest_summary(self, ctx: Any, session_id: str) -> str | None:
        return await self._store.latest_summary(ctx, session_id)

    async def get_original_messages(self, ctx: Any, session_id: str,
                                   start_seq: int, end_seq: int) -> list[dict]:
        return await self._store.get_original_messages(ctx, session_id, start_seq, end_seq)

    # ---- 诊断 ----

    @property
    def backoff(self) -> BackoffState:
        """退避状态机（可观测：诊断 / 测试可查暂停与计数）。"""
        return self._backoff

    def stats(self) -> dict:
        return {"backoff": self._backoff.stats(), "warmup_success": self._warmup}


__all__ = ["HistoryCompactor"]
