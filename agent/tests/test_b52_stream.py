"""B5.2 离线测试：Token 级流式（打字机）事件法。

绕开 HTTP 层（与 test_progress 同纪律）：直接驱动 ``ctx.llm.complete``，
订阅 ``Events.LLM_TOKEN`` 收集帧，断言切帧 / 不发 / 关流零帧。

不依赖真实 DeepSeek API —— 用 fixture provider 确定性切帧。
"""

from __future__ import annotations

import asyncio

from agent.api import Events
from agent.profiles import build_agent


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path, llm_overlay: dict | None = None):
    cfg = {"db_path": str(tmp_path / "agent.db"),
           "agent": {"llm": {"provider": "fixture", "model": "fixture"}}}
    if llm_overlay:
        cfg["agent"]["llm"].update(llm_overlay)
    return cfg


def _answer_messages() -> list[dict]:
    # 路由到 chat:answer（产生散文文本，schema=None 路径）
    return [{"role": "system", "content": "<!--chat:answer-->"},
            {"role": "user", "content": "你好"}]


async def _collect_tokens(c, messages, schema=None):
    frames: list[dict] = []
    c.root.on(Events.LLM_TOKEN, lambda *a: frames.append(a[0]))
    reply = await c.root.llm.complete(c.root, messages, schema=schema)
    return reply, frames


def test_stream_true_emits_multiple_token_pieces(tmp_path):
    """stream 开启（显式 true）+ schema None → 多片 token 帧，拼接 == 最终文本。"""
    c = build_agent(config=_cfg(tmp_path, {"stream": True}))
    async def s():
        await c.assemble()
        await c.start()
        try:
            reply, frames = await _collect_tokens(c, _answer_messages())
        finally:
            await c.teardown()
        return reply, frames
    reply, frames = _run(s())
    assert reply.data["text"], "fixture 应返回非空文本"
    assert len(frames) >= 2, f"应切成多片，实际 {len(frames)} 片"
    joined = "".join(f.get("text", "") for f in frames)
    assert joined == reply.data["text"], "token 帧拼接必须等于最终文本（去重前提）"


def test_stream_default_is_true(tmp_path):
    """未显式配置 stream（D5 默认 true）→ 仍应产生 token 帧。"""
    c = build_agent(config=_cfg(tmp_path))          # 不含 stream 键
    async def s():
        await c.assemble()
        await c.start()
        try:
            reply, frames = await _collect_tokens(c, _answer_messages())
        finally:
            await c.teardown()
        return reply, frames
    reply, frames = _run(s())
    assert len(frames) >= 1, "默认 stream=true 应产生 token 帧"


def test_schema_call_emits_no_token(tmp_path):
    """schema 非空（内部规划 _plan 路径）→ 不产生任何 token 帧。"""
    c = build_agent(config=_cfg(tmp_path, {"stream": True}))
    async def s():
        await c.assemble()
        await c.start()
        try:
            # schema 非空，且随便给条消息（route 不影响「schema 非空不发」规则）
            reply, frames = await _collect_tokens(
                c, [{"role": "user", "content": "规划一下"}],
                schema={"intent": {"type": "str"}})
        finally:
            await c.teardown()
        return reply, frames
    reply, frames = _run(s())
    assert frames == [], f"结构化调用不应发 token 帧，实际 {frames}"


def test_stream_off_emits_no_token_and_text_intact(tmp_path):
    """stream 关闭 → 零 token 帧，且 reply.data["text"] 与今天一致（行为不变）。"""
    c = build_agent(config=_cfg(tmp_path, {"stream": False}))
    async def s():
        await c.assemble()
        await c.start()
        try:
            reply, frames = await _collect_tokens(c, _answer_messages())
        finally:
            await c.teardown()
        return reply, frames
    reply, frames = _run(s())
    assert frames == [], f"关流不应发 token 帧，实际 {frames}"
    assert reply.data["text"], "关流时仍应返回完整文本"
