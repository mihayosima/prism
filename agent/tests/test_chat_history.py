"""短期记忆（会话历史）测试 —— 转写落库 · 下一轮注入模型上下文 · 会话隔离 · 重置。

数据层归属（解耦审计 §① 修复）：``agent_messages`` 归短期记忆内容层
（``plugins/short_term``），chat 侧零 SQL，一律经 ``Services.SHORT_TERM``。
故本文件的会话历史用例**显式装配**目录插件；另补一组"插件未装配"的降级用例，
证明 opt-in 语义下对话不裸崩。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.api import Services
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path):
    return {"db_path": str(tmp_path / "agent.db")}


def _pm(tmp_path):
    PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    pm.set_enabled("plugins.short_term", True)      # 目录插件：opt-in
    pm.set_enabled("plugins.context", True)         # 调度层（收窗）
    return pm


def _build(tmp_path):
    return build_agent(config=_cfg(tmp_path), plugin_mgr=_pm(tmp_path))


# ---------------------------------------------------------------------------
# 装配短期记忆：会话转写
# ---------------------------------------------------------------------------


def test_multiturn_history_stored(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        r1 = await c.root.chat.send(c.root, "你好", session_id="s1")
        r2 = await c.root.chat.send(c.root, "我上一句说了什么", session_id="s1")
        assert r1.ok and r2.ok
        hist = await c.root.chat.history(c.root, "s1")
        assert hist[0]["role"] == "user" and hist[0]["content"] == "你好"
        assert hist[1]["role"] == "assistant" and hist[1]["content"]
        assert hist[2]["role"] == "user" and hist[2]["content"] == "我上一句说了什么"
        await c.teardown()

    _run(s())


def test_next_turn_sees_prior_turns_in_llm_messages(tmp_path):
    """证明短期记忆真的注入模型：第二轮发给 LLM 的消息里含第一轮用户消息。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        svc = c.root.get(Services.LLM)
        captured: list[list[dict]] = []
        orig = svc.complete

        async def rec(ctx, messages, *, schema=None, caller="test"):
            captured.append(list(messages))
            return await orig(ctx, messages, schema=schema, caller=caller)

        svc.complete = rec                       # 换实现不动契约（捕获→原样转发）
        await c.root.chat.send(c.root, "你好", session_id="s2")
        await c.root.chat.send(c.root, "我上一句说了什么", session_id="s2")
        user_contents = [m["content"] for ms in captured for m in ms if m["role"] == "user"]
        assert "你好" in user_contents, "第二轮的模型输入未含第一轮用户消息（短期记忆未注入）"
        assert "我上一句说了什么" in user_contents
        await c.teardown()

    _run(s())


def test_sessions_isolated_and_reset(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        await c.root.chat.send(c.root, "会话A的消息", session_id="a")
        await c.root.chat.send(c.root, "会话B的消息", session_id="b")
        ha = [m["content"] for m in await c.root.chat.history(c.root, "a")]
        hb = [m["content"] for m in await c.root.chat.history(c.root, "b")]
        assert "会话A的消息" in ha and "会话B的消息" not in ha
        assert "会话B的消息" in hb and "会话A的消息" not in hb
        n = await c.root.chat.reset(c.root, "a")
        assert n >= 2
        assert await c.root.chat.history(c.root, "a") == []
        assert await c.root.chat.history(c.root, "b") != []   # 互不影响
        await c.teardown()

    _run(s())


def test_reset_clears_summary_too(tmp_path):
    """解耦审计发现的缺陷：``reset`` 必须级联清摘要表。

    否则历史已删、摘要仍残留，下一轮会把"已被删掉的历史"的前情注入模型。
    """
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        short = c.root.get(Services.SHORT_TERM)
        for i in range(3):
            await short.append(c.root, "s", "user", f"消息{i}")
        await short._store.upsert_summary(c.root, "s", summary="旧前情", version=1,
                                          covers_up_to_seq=999, tokens=10)
        assert await short.latest_summary(c.root, "s") == "旧前情"
        await c.root.chat.reset(c.root, "s")
        assert await short.latest_summary(c.root, "s") is None, "reset 未级联清摘要"
        await c.teardown()

    _run(s())


def test_chat_has_no_direct_sql_on_message_table():
    """守卫：chat（sys 层）不得直连短期记忆拥有的表（数据层归属）。"""
    import re
    from pathlib import Path as _P
    src = (_P(__file__).resolve().parent.parent / "sys" / "chat.py").read_text(encoding="utf-8")
    body = "\n".join(ln for ln in src.splitlines() if not ln.strip().startswith("#"))
    assert not re.search(r"(INSERT\s+INTO|DELETE\s+FROM|UPDATE)\s+agent_messages", body, re.I)
    assert not re.search(r"FROM\s+agent_messages", body, re.I)


# ---------------------------------------------------------------------------
# 降级：短期记忆未装配（opt-in）→ 对话不裸崩，仅无会话转写
# ---------------------------------------------------------------------------


def test_without_short_term_plugin_chat_still_works(tmp_path):
    """未装配短期记忆插件：send 正常返回，历史为空，reset 返回 0（不裸崩）。"""
    c = build_agent(config=_cfg(tmp_path))       # 不传 plugin_mgr → 目录插件不装

    async def s():
        await c.assemble()
        assert not c.root.has(Services.SHORT_TERM)
        r = await c.root.chat.send(c.root, "你好", session_id="s3")
        assert r.ok and r.text
        assert await c.root.chat.history(c.root, "s3") == []
        assert await c.root.chat.reset(c.root, "s3") == 0
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# G1：prior 经治理器 messages 块装配（统一互斥 + 降级不劣化）
# ---------------------------------------------------------------------------


def test_prior_summary_and_history_mutually_exclusive(tmp_path):
    """互斥（左开右闭）：已摘要历史不进 prior 原文；无重复；输出剥离 ``seq``。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        short = c.root.get(Services.SHORT_TERM)
        for i in range(6):
            await short.append(c.root, "m", "user", f"消息{i}")
        # 摘要覆盖 seq<=4（"消息0..3"）→ 与 prior 原文互斥
        await short._store.upsert_summary(c.root, "m", summary="前情摘要", version=1,
                                          covers_up_to_seq=4, tokens=5)
        prior = await c.root.chat._build_prior(c.root, "m", "新一轮")
        contents = [m["content"] for m in prior]
        assert any("前情摘要" in x for x in contents)                        # 摘要块在
        assert not any(x in ("消息0", "消息1", "消息2", "消息3") for x in contents)
        assert "消息4" in contents                        # 未摘要部分仍在
        assert "消息5" not in contents                    # raw[:-1] 视末条为"本轮 user"，已排除
        assert len(contents) == len(set(contents))                           # 无重复注入
        assert all("seq" not in m for m in prior)                            # 剥离内部 seq
        await c.teardown()

    _run(s())


def test_prior_without_context_plugin_fallback(tmp_path):
    """治理器停用 → 降级拼接：prior 仍正确（摘要头 + 未摘要历史），不劣化。"""
    PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR), state_file=str(tmp_path / "s2.json"))
    pm.set_enabled("plugins.short_term", True)      # 不启用 context
    c = build_agent(config=_cfg(tmp_path), plugin_mgr=pm)

    async def s():
        await c.assemble()
        assert not c.root.has(Services.CONTEXT)
        short = c.root.get(Services.SHORT_TERM)
        for i in range(5):
            await short.append(c.root, "k", "user", f"m{i}")
        await short._store.upsert_summary(c.root, "k", summary="前情", version=1,
                                          covers_up_to_seq=2, tokens=3)
        prior = await c.root.chat._build_prior(c.root, "k", "x")
        contents = [m["content"] for m in prior]
        assert any("前情" in x for x in contents)
        assert "m2" in contents and "m3" in contents      # seq 3,4 未摘要（raw[:-1] 排除末条）
        assert "m0" not in contents and "m1" not in contents
        assert all("seq" not in m for m in prior)
        await c.teardown()

    _run(s())
