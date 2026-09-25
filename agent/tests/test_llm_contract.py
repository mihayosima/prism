"""D1 · LLM 服务契约锁定测试（core/contract.LlmProvider 语义契约）。

- stream=True + schema → 返回已解析 dict（回归 aa39fb4：流式分支必须累积 content）；
- stream=True + tools → 分片 delta 归并完整（name/arguments 跨 chunk 拼接）；
- EventBus emit 契约：同步监听器**内联执行**（budget 退款依赖此语义）、async 监听器 fire-and-forget。

全部 httpx.MockTransport 模拟，零触网。
"""

from __future__ import annotations

import asyncio

import httpx

from agent.core.events import EventBus
from agent.sys.llm import DeepSeekLlm, LlmService

_SCHEMA = {"facts": {"type": "array"}}


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


# ---- 契约①：结果与 stream 配置无关（schema 调用走流式也必须可解析） ----

def test_stream_with_schema_returns_parseable_dict():
    sse = (
        'data: {"choices":[{"delta":{"content":"{\\"facts\\": [\\"喜欢蓝色\\"]"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"}"}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        body = request.read().decode()
        assert b"response_format" in request.read() or "response_format" in body
        return httpx.Response(200, text=sse)

    svc = _svc(httpx.MockTransport(handler), stream=True)
    ctx = _Ctx(stream=True)

    async def s():
        r = await svc.complete(ctx, [{"role": "user", "content": "记住偏好"}],
                               schema=_SCHEMA, caller="plugins.memory")
        assert r.data == {"facts": ["喜欢蓝色"]}, "流式+schema 必须返回已解析 JSON dict"
        assert not [n for n, _ in ctx.events.frames if n == "agent/llm/token"], \
            "schema 调用的中间散文不应外发 token 帧"
        assert r.tokens_in == 5 and r.tokens_out == 2
        await svc._provider.close()

    _run(s())


# ---- 契约②：tools 分片 delta 归并完整 ----

def test_stream_with_tools_merges_split_deltas():
    # 工具名与参数被模型拆成多个 delta 分片（name "ec"+"ho"，arguments 两段）
    sse = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"ec"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"name":"ho","arguments":"{\\"x\\""}}]}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":":1}"}}]}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        return httpx.Response(200, text=sse)

    svc = _svc(httpx.MockTransport(handler), stream=True)
    ctx = _Ctx(stream=True)
    tools = [{"name": "echo", "description": "回声", "params": {"x": {"type": "str"}},
              "required": ["x"]}]

    async def s():
        r = await svc.complete(ctx, [{"role": "user", "content": "调用工具"}],
                               tools=tools, caller="test")
        calls = r.data.get("tool_calls")
        assert calls and len(calls) == 1, "分片必须归并为一个调用"
        assert calls[0]["name"] == "echo", "跨 chunk 的 name 分片必须拼接还原"
        assert calls[0]["arguments"] == {"x": 1}, "跨 chunk 的 arguments 分片必须拼接解析"
        assert calls[0]["id"] == "c1"
        await svc._provider.close()

    _run(s())


# ---- 契约③：EventBus emit 同步监听器内联执行（budget 退款依赖） ----

class _StubCtx:
    def __init__(self):
        self.closed = False
        self.name = "stub"


def test_emit_sync_listener_inline_async_fire_and_forget():
    async def s():
        bus = EventBus()
        seen = {"sync": False, "async": False}

        def on_sync(name, payload=None):
            seen["sync"] = True

        async def on_async(name, payload=None):
            seen["async"] = True

        bus.on(_StubCtx(), "e", on_sync)
        bus.on(_StubCtx(), "e", on_async)
        await bus.emit("e", {})
        assert seen["sync"], "同步监听器必须在 emit 返回前内联执行完（退款立即可见）"
        assert not seen["async"], "async 监听器是 fire-and-forget，emit 返回时不应已执行"
        await bus.aclose()

    _run(s())
