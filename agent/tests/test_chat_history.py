"""短期记忆（会话历史）测试 —— 转写落库 · 下一轮注入模型上下文 · 会话隔离 · 重置。"""

from __future__ import annotations

import asyncio

from agent.profiles import build_agent


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path):
    return {"db_path": str(tmp_path / "agent.db")}


def test_multiturn_history_stored(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

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
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        svc = c.root.get("llm")
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
    c = build_agent(config=_cfg(tmp_path))

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