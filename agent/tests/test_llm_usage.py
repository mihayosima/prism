"""S6 llm 连接复用 + usage 真实入账测试（httpx.MockTransport，零触网）。"""

from __future__ import annotations

import asyncio

import httpx

from agent.sys.llm import DeepSeekLlm, LlmService


def _run(coro):
    return asyncio.run(coro)


class _Bus:
    def __init__(self):
        self.frames: list[tuple] = []

    async def emit(self, name, payload=None):
        self.frames.append((name, payload))

    async def waterfall(self, name, payload=None):
        return payload


class _Ctx:
    def __init__(self, stream: bool = False):
        self.config = {"agent": {"llm": {"stream": stream}}}
        self.events = _Bus()


def _svc(transport, stream=False) -> LlmService:
    return LlmService(DeepSeekLlm("m", "k", transport=transport), "m")


def test_nonstream_usage_accounting():
    """非流式：优先用响应 usage 真实 token 入账，不再靠估算。"""
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 111, "completion_tokens": 22},
        })

    svc = _svc(httpx.MockTransport(handler))
    ctx = _Ctx(stream=False)

    async def s():
        r = await svc.complete(ctx, [{"role": "user", "content": "hi"}])
        assert r.data["text"] == "hello"
        assert "usage" not in r.data, "usage 键应被 LlmService 消费，不下发给调用方"
        assert r.tokens_in == 111 and r.tokens_out == 22
        await svc._provider.close()

    _run(s())


def test_client_reused_across_calls():
    """两次调用复用同一 AsyncClient（连接池），不再每次新建。"""
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    svc = _svc(httpx.MockTransport(handler))
    ctx = _Ctx(stream=False)

    async def s():
        await svc.complete(ctx, [{"role": "user", "content": "a"}])
        c1 = svc._provider._client
        assert c1 is not None
        await svc.complete(ctx, [{"role": "user", "content": "b"}])
        assert svc._provider._client is c1, "应复用同一 AsyncClient"
        assert calls["n"] == 2
        await svc._provider.close()
        assert svc._provider._client is None

    _run(s())


_SSE = (
    'data: {"choices":[{"delta":{"content":"he"}}]}\n\n'
    'data: {"choices":[{"delta":{"content":"y"}}]}\n\n'
    'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n'
    'data: [DONE]\n\n'
)


def test_stream_usage_accounting():
    """流式：include_usage 末帧的真实 usage 入账；token 帧照常发出。"""
    def handler(request):
        assert b'"include_usage":true' in request.read().replace(b" ", b"") \
            or b"include_usage" in request.read(), "流式应请求 include_usage"
        return httpx.Response(200, text=_SSE)

    svc = _svc(httpx.MockTransport(handler), stream=True)
    ctx = _Ctx(stream=True)

    async def s():
        r = await svc.complete(ctx, [{"role": "user", "content": "hi"}])
        assert r.data["text"] == "hey"
        assert r.tokens_in == 7 and r.tokens_out == 3
        tokens = [p for name, p in ctx.events.frames if name == "agent/llm/token"]
        assert len(tokens) == 2, "打字机帧不应受 usage 采集影响"
        await svc._provider.close()

    _run(s())
