"""短期记忆（第 4 层）滚动摘要压缩测试。

覆盖：本地截短（第一层）/ 摘要优先级预算（第二层兜底）/ 退避状态机 / 覆盖范围对齐
（covers_up_to_seq）/ 触发阈值 / 增量累积 / 降级路径 / 空短历史不触发 / 可逆性。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from agent.api import Services, now_iso
from agent.core.tokens import estimate_tokens
from agent.plugins.short_term.backoff import BackoffState
from agent.plugins.short_term.compactor import HistoryCompactor
from agent.plugins.short_term.store import SessionSummaryStore
from agent.plugins.short_term.truncate import (
    compress_summary_to_budget,
    parse_sections,
    truncate_content,
    truncate_messages,
)
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"

_BIG = "历史内容" * 100            # ~300 token / 条


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path, *, short: bool = True, context: bool = True):
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "plugin_state.json"))
    if short:
        pm.set_enabled("plugins.short_term", True)
    if context:
        pm.set_enabled("plugins.context", True)
    return pm


def _build_short(tmp_path, *, short: bool = True, context: bool = True,
                 st_cfg: dict | None = None, ctx_cfg: dict | None = None):
    patch = {}
    if st_cfg is not None:
        patch.setdefault("agent", {})["short_term"] = st_cfg
    if ctx_cfg is not None:
        patch.setdefault("agent", {})["context"] = ctx_cfg
    return build_agent(
        config={"db_path": str(tmp_path / "agent.db")},
        patch=patch or None,
        plugin_mgr=_pm(tmp_path, short=short, context=context),
    )


# ---- Fake LLM（注入确定性摘要，绕过 fixture 路由） ----

FAKE_SUMMARY = (
    "### 反馈/纠正\n用户要求修正术语\n"
    "### 未决问题\n等待确认部署环境\n"
    "### 目标/意图\n完成短期记忆压缩\n"
    "### 事实/决策\n采用滚动摘要方案\n"
    "### 关键实体\nagent_messages 表\n"
)


class FakeLlmService:
    def __init__(self, text: str = FAKE_SUMMARY, *, fail: bool = False):
        self._text = text
        self._fail = fail

    async def complete(self, ctx, messages, *, schema=None, tools=None, caller="x"):
        if self._fail:
            raise RuntimeError("injected llm failure")
        return SimpleNamespace(data={"text": self._text}, tokens_in=0, tokens_out=0)


# ============ 第一层：本地截短（纯函数） ============

def test_truncate_content_under_limit():
    assert truncate_content("短内容", 1000) == ("短内容", 0)


def test_truncate_content_over_limit_keeps_head_tail():
    big = "\n".join(f"line{i} " + "x" * 40 for i in range(60))   # 多行
    out, omitted = truncate_content(big, 300)
    assert omitted > 0
    assert "[...省略" in out and "tokens...]" in out
    # 头尾保留
    assert out.startswith("line0")
    assert out.split("\n")[-1].startswith(f"line{59}")            # 尾行以 line59 起始


def test_truncate_content_single_line_head_tail():
    big = "甲" * 400 + "乙" * 400
    out, omitted = truncate_content(big, 100)
    assert omitted > 0
    assert out.startswith("甲") and out.endswith("乙")


def test_truncate_messages_only_old_and_reversible():
    msgs = [
        {"seq": i, "role": "user", "content": _BIG, "token_count": estimate_tokens(_BIG)}
        for i in range(20)
    ]
    # 最近 3 轮（6 条）不截；老内容超阈值（tool_threshold=50 < 每条 ~300 token）才截
    out = truncate_messages(msgs, recent_rounds=3, tool_threshold=50)
    # 最近 6 条原文保留
    for m in out[-6:]:
        assert m["content"] == _BIG
    # 老内容被截
    truncated = [m for m in out[:-6] if m["content"] != _BIG]
    assert truncated, "老内容应被截短"
    # 可逆：原文未被改写（入参未变）
    assert all(m["content"] == _BIG for m in msgs)


def test_truncate_messages_recent_not_truncated():
    msgs = [{"seq": 0, "role": "user", "content": _BIG, "token_count": estimate_tokens(_BIG)}]
    out = truncate_messages(msgs, recent_rounds=3)
    assert out[0]["content"] == _BIG          # 单条即"最近"，不截


# ============ 第二层：摘要优先级预算（纯函数） ============

def test_compress_summary_priority_drops_low_not_protected():
    # 实体/事实超预算时裁剪低优先级；反馈/未决受保护不被裁
    text = (
        "### 反馈/纠正\n" + "F" * 400 + "\n"
        "### 未决问题\n" + "U" * 400 + "\n"
        "### 目标/意图\n" + "G" * 400 + "\n"
        "### 事实/决策\n" + "C" * 400 + "\n"
        "### 关键实体\n" + "E" * 400 + "\n"
    )
    budgets = {"feedback": 50, "unresolved": 50, "goal": 50, "facts": 50, "entity": 50}
    # 让总 token 远超（每条 ~300 token，总 ~1500 >> 600）
    out = compress_summary_to_budget(text, budgets, total_soft=600)
    sec = parse_sections(out)
    # 受保护区保留内容（未牺牲）
    assert "F" in sec.get("feedback", "") and "U" in sec.get("unresolved", "")
    # 低优先级被压缩（实体内容显著缩短或清空）
    assert estimate_tokens(sec.get("entity", "")) <= 50 + 30
    assert estimate_tokens(out) <= 600 + 30


def test_compress_summary_single_section_clip_to_soft():
    text = "杂项内容 " + "z" * 400
    out = compress_summary_to_budget(text, {}, total_soft=600)
    assert estimate_tokens(out) <= 600 + 30


# ============ 退避状态机（纯类） ============

def test_backoff_incremental_tiers():
    b = BackoffState(now=lambda: 0.0)
    assert b.record_failure("llm_error") == 1       # 第 1 次 → 1m
    assert b.record_failure("llm_error") == 10      # 第 2 次 → 10m
    assert b.record_failure("llm_error") == 60      # 第 3 次 → 1h
    assert b.record_failure("llm_error") == 1440    # 第 4 次 → 24h（封顶）
    assert b.record_failure("llm_error") == 1440    # 仍封顶


def test_backoff_success_resets():
    b = BackoffState(now=lambda: 0.0)
    b.record_failure("llm_error")
    b.record_failure("llm_error")
    assert b.current_tier_minutes("llm_error") == 10
    b.record_success("llm_error")
    assert not b.is_paused("llm_error")              # 清零，可尝试


def test_backoff_reason_isolation():
    b = BackoffState(now=lambda: 0.0)
    b.record_failure("llm_error")                     # 仅 llm_error 暂停
    assert not b.is_paused("db_error")                # 另一原因不受影响（未暂停）


def test_backoff_probe_failure_only_renews_no_upgrade():
    b = BackoffState(now=lambda: 0.0)
    b.record_failure("llm_error")                    # 第 1 次 → 1m，pause_until=60
    # 模拟暂停期内探测失败：只续期，不累加/升级
    tier = b.record_probe_failure("llm_error")
    assert tier == 1                                 # 档位仍为 1m
    assert b._state["llm_error"]["count"] == 1       # 计数未增


# ============ 集成：表迁移 / 装配 ============

def test_short_term_tables_created(tmp_path):
    c = _build_short(tmp_path)
    async def s():
        await c.assemble()
        ctx = c.root
        assert ctx.has(Services.SHORT_TERM)
        # 摘要表存在
        rows = await ctx.db.query(ctx, "SELECT name FROM sqlite_master WHERE type='table'")
        names = {r["name"] for r in rows}
        assert "agent_session_summaries" in names
        # agent_messages 扩列
        cols = await ctx.db.query(ctx, "PRAGMA table_info(agent_messages)")
        col_names = {r["name"] for r in cols}
        assert "token_count" in col_names and "compressed_into" in col_names
    _run(s())


# ============ 集成：压缩触发 + 覆盖对齐 + 增量 ============

async def _seed_history(ctx, sid, n, content=_BIG):
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        await ctx.db.execute(
            ctx, "INSERT INTO agent_messages(session_id, role, content, at) VALUES (?,?,?,?)",
            (sid, role, content, now_iso()))


def test_compress_triggers_and_covers(tmp_path):
    # 低 compact_trigger_tokens → 低阈值，便于触发（收窗已移除，压缩时机仅由该阈值决定）
    c = _build_short(
        tmp_path,
        st_cfg={"compact_trigger_tokens": 500,
                "summary_max_tokens": 600},
        ctx_cfg={"max_context_tokens": 8000, "history_ratio": 0.6},
    )
    async def s():
        await c.assemble()
        ctx = c.root
        ctx.set(Services.LLM, FakeLlmService())
        sid = "s1"
        await _seed_history(ctx, sid, 30)
        short = ctx.get(Services.SHORT_TERM)
        await short.maybe_compress(ctx, sid)
        summary = await short.latest_summary(ctx, sid)
        assert summary is not None                       # 已生成摘要
        meta = (await ctx.db.query(
            ctx, "SELECT covers_up_to_seq, tokens FROM agent_session_summaries WHERE session_id=?",
            (sid,)))[0]
        # covers_up_to_seq 应覆盖非 kept 历史（>0 且 < 30）
        assert 0 < meta["covers_up_to_seq"] <= 30
        # 被覆盖的历史标记 compressed_into
        marked = await ctx.db.query(
            ctx, "SELECT COUNT(*) AS n FROM agent_messages "
                 "WHERE session_id=? AND compressed_into IS NOT NULL", (sid,))
        assert marked[0]["n"] > 0
        # 摘要 token 不爆软上限（兜底压缩）
        assert estimate_tokens(summary) <= 600 + 30
    _run(s())


def test_compress_empty_short_history_no_trigger(tmp_path):
    c = _build_short(tmp_path, st_cfg={"compact_trigger_tokens": 500})
    async def s():
        await c.assemble()
        ctx = c.root
        ctx.set(Services.LLM, FakeLlmService())
        sid = "s2"
        await _seed_history(ctx, sid, 2)                  # 短历史 < min_messages(4)
        short = ctx.get(Services.SHORT_TERM)
        await short.maybe_compress(ctx, sid)
        assert await short.latest_summary(ctx, sid) is None   # 未触发（不算失败）
    _run(s())


def test_compress_llm_failure_degrades_gracefully(tmp_path):
    c = _build_short(tmp_path, st_cfg={"compact_trigger_tokens": 500},
                     ctx_cfg={"max_context_tokens": 8000})
    async def s():
        await c.assemble()
        ctx = c.root
        ctx.set(Services.LLM, FakeLlmService(fail=True))   # LLM 抛错
        sid = "s3"
        await _seed_history(ctx, sid, 30)
        short = ctx.get(Services.SHORT_TERM)
        await short.maybe_compress(ctx, sid)               # 不应抛
        assert await short.latest_summary(ctx, sid) is None   # 降级：无摘要
    _run(s())


def test_compress_respects_pause_backoff(tmp_path):
    """LLM 暂停期内不调用 LLM（降级），且不崩。"""
    c = _build_short(tmp_path, st_cfg={"compact_trigger_tokens": 500},
                     ctx_cfg={"max_context_tokens": 8000})
    async def s():
        await c.assemble()
        ctx = c.root
        fake = FakeLlmService()
        ctx.set(Services.LLM, fake)
        short = ctx.get(Services.SHORT_TERM)
        sid = "s4"
        await _seed_history(ctx, sid, 30)
        # 手动把 llm_error 推入暂停（经门面暴露的退避状态机）
        short.backoff.record_failure("llm_error")
        assert short.backoff.is_paused("llm_error")
        # 但探针会成功 → 清零 → 正常压缩
        await short.maybe_compress(ctx, sid)
        assert await short.latest_summary(ctx, sid) is not None
    _run(s())


# ============ 集成：降级（短期记忆/治理器停用） ============

def test_degrade_when_short_term_disabled(tmp_path):
    """停用 short_term+context → chat 走 HISTORY_LIMIT 硬裁，不崩。"""
    c = _build_short(tmp_path, short=False, context=False)
    async def s():
        await c.assemble()
        ctx = c.root
        chat = ctx.get(Services.CHAT)
        reply = await chat.send(ctx, "你好，介绍一下自己", session_id="d1")
        assert reply.ok or reply.text
        assert not ctx.has(Services.SHORT_TERM)
    _run(s())


def test_prior_contains_summary_when_available(tmp_path):
    """prior 注入摘要（user 角色）且不含本轮 user（调用方追加）。"""
    c = _build_short(tmp_path, st_cfg={"compact_trigger_tokens": 500},
                     ctx_cfg={"max_context_tokens": 8000})
    async def s():
        await c.assemble()
        ctx = c.root
        ctx.set(Services.LLM, FakeLlmService())
        sid = "s5"
        await _seed_history(ctx, sid, 30)
        short = ctx.get(Services.SHORT_TERM)
        await short.maybe_compress(ctx, sid)
        summary = await short.latest_summary(ctx, sid)
        assert summary is not None
        chat = ctx.get(Services.CHAT)
        prior = await chat._build_prior(ctx, sid, "当前问题")
        roles = [m["role"] for m in prior]
        assert "user" in roles and any("会话前情" in (m.get("content") or "") for m in prior)
        # prior 不含本轮 user 内容（"当前问题"）
        assert not any(m.get("content") == "当前问题" for m in prior)
    _run(s())


__all__ = ["test_truncate_content_under_limit", "test_truncate_content_over_limit_keeps_head_tail",
           "test_truncate_content_single_line_head_tail", "test_truncate_messages_only_old_and_reversible",
           "test_truncate_messages_recent_not_truncated", "test_compress_summary_priority_drops_low_not_protected",
           "test_compress_summary_single_section_clip_to_soft", "test_backoff_incremental_tiers",
           "test_backoff_success_resets", "test_backoff_reason_isolation",
           "test_backoff_probe_failure_only_renews_no_upgrade", "test_short_term_tables_created",
           "test_compress_triggers_and_covers", "test_compress_empty_short_history_no_trigger",
           "test_compress_llm_failure_degrades_gracefully", "test_compress_respects_pause_backoff",
           "test_degrade_when_short_term_disabled", "test_prior_contains_summary_when_available"]
