"""#3 · LLM 调用重试 + 超时降级 单元测试（全部 httpx.MockTransport 模拟，零触网）。

覆盖：
- 非流式：前 2 次 503、第 3 次 200 → 成功且仅 3 次 POST（指数退避重试生效）；
- 非流式：400 → 立即抛 AgentError、不重试（不可重试状态）；
- 非流式：连续 503 耗尽重试 → 抛 LlmUnavailableError 且消息含 last_status=503；
- 经 LlmService 封装：耗尽重试抛 LlmUnavailableError，**且 emit 记录 LLM_CALL_FAILED**
  （验证复用既有失败路径，而非另起炉灶）；
- 流式：503 → 重试 → 200 SSE 正常解析；
- 流式三档边界：首帧后断开 → 原样抛出、不重试（避免半截结果被当成完整响应）。
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from agent.api import Events, LlmUnavailableError
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
    def __init__(self, llm_cfg: dict | None = None):
        self.config = {"agent": {"llm": llm_cfg or {}}}
        self.events = _Bus()

    def get(self, key):
        # 测试桩：无 db → durable 落盘 no-op（与真实 durable 在 ctx.get 返回 None 时行为一致）
        return None


def _svc(transport, model="m") -> LlmService:
    return LlmService(DeepSeekLlm(model, "k", transport=transport), model)


_OK_BODY = {
    "choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1},
}

# 非流式需显式 stream=False（complete 默认 stream_on=True）
_NONSTREAM_CFG = {"stream": False, "max_retries": 3, "retry_backoff_s": 0.001}


# ---- 非流式：503→503→200 ----

def test_nonstream_retry_then_success():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json=_OK_BODY)

    ctx = _Ctx(_NONSTREAM_CFG)
    svc = _svc(httpx.MockTransport(handler))

    async def s():
        r = await svc.complete(ctx, [{"role": "user", "content": "hi"}], caller="t")
        await svc._provider.close()
        return r

    r = _run(s())
    assert r.data["text"] == "hello", "重试后成功应返回正确内容"
    assert calls["n"] == 3, f"应恰好重试 3 次 POST，实得 {calls['n']}"


# ---- 非流式：400 不重试 ----

def test_nonstream_400_no_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(400, text="bad request")

    ctx = _Ctx(_NONSTREAM_CFG)
    svc = _svc(httpx.MockTransport(handler))

    async def s():
        with pytest.raises(Exception) as ei:
            await svc.complete(ctx, [{"role": "user", "content": "hi"}], caller="t")
        await svc._provider.close()
        return ei

    ei = _run(s())
    # 400 不可重试 → 立即抛（AgentError 或子类），且不重试
    assert calls["n"] == 1, f"不可重试状态不应重试，实得 {calls['n']} 次"
    assert "deepseek api 异常 400" in str(ei.value)


# ---- 非流式：连续 503 耗尽 → LlmUnavailableError（含 last_status） ----

def test_nonstream_all_503_raises_unavailable():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="overloaded")

    ctx = _Ctx({"stream": False, "max_retries": 2, "retry_backoff_s": 0.001})  # 2 重试 → 共 3 次
    svc = _svc(httpx.MockTransport(handler))

    async def s():
        with pytest.raises(LlmUnavailableError) as ei:
            await svc.complete(ctx, [{"role": "user", "content": "hi"}], caller="t")
        await svc._provider.close()
        return ei

    ei = _run(s())
    assert "last_status=503" in str(ei.value), "耗尽重试的错误应携带 last_status"
    assert calls["n"] == 3, f"应恰好尝试 3 次，实得 {calls['n']}"


# ---- 经 LlmService：耗尽重试复用既有失败路径（emit 记录 LLM_CALL_FAILED） ----

def test_service_records_failed_event_on_exhausted_retry():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="overloaded")

    ctx = _Ctx({"stream": False, "max_retries": 1, "retry_backoff_s": 0.001})  # 共 2 次
    svc = _svc(httpx.MockTransport(handler))

    async def s():
        with pytest.raises(LlmUnavailableError):
            await svc.complete(ctx, [{"role": "user", "content": "hi"}], caller="t")
        await svc._provider.close()

    _run(s())
    events = [e for e, _ in ctx.events.frames]
    assert Events.LLM_CALL_FAILED in events, \
        "重试耗尽应沿既有失败路径 emit LLM_CALL_FAILED（而非另起炉灶）"


# ---- 流式：503 → 重试 → 200 SSE ----

def test_stream_retry_then_success():
    calls = {"n": 0}
    sse = (
        'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, text=sse)

    ctx = _Ctx({"stream": True, "max_retries": 2, "retry_backoff_s": 0.001})
    svc = _svc(httpx.MockTransport(handler))

    async def s():
        r = await svc.complete(ctx, [{"role": "user", "content": "hi"}], caller="t")
        await svc._provider.close()
        return r

    r = _run(s())
    assert r.data["text"] == "ok", "流式重试后成功应返回正确内容"
    assert calls["n"] == 2, f"流式应恰好重试 2 次，实得 {calls['n']}"


# ---- 流式三档边界：首帧后断开 → 不重试，原样抛出 ----

class _OneFrameThenError(httpx.AsyncByteStream):
    """第一帧正常，随后迭代时抛传输错误（模拟首帧后断流）。"""

    def __init__(self, data: bytes, exc):
        self._data = data
        self._exc = exc
        self._done = False

    async def aread(self):
        return self._data

    async def __aiter__(self):
        if not self._done:
            self._done = True
            yield self._data
        raise self._exc


class _MidStreamDropTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.calls = 0

    async def handle_async_request(self, request):
        self.calls += 1
        if self.calls == 1:
            first = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            return httpx.Response(
                200, stream=_OneFrameThenError(first, httpx.TransportError("mid-stream drop"))
            )
        sse = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, text=sse)


def test_stream_post_first_frame_drop_not_retried():
    transport = _MidStreamDropTransport()
    ctx = _Ctx({"stream": True, "max_retries": 3, "retry_backoff_s": 0.001})
    svc = _svc(transport)

    async def s():
        # 首帧后断开 → 原样抛出（httpx.TransportError），不应被重试
        with pytest.raises(httpx.TransportError):
            await svc.complete(ctx, [{"role": "user", "content": "hi"}], caller="t")
        await svc._provider.close()

    _run(s())
    assert transport.calls == 1, (
        f"首帧后断开不应重试，实得 {transport.calls} 次调用"
    )
