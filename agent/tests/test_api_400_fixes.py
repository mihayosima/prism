"""400 修复测试（9-29 实测两起 deepseek 400 的回归防护）：

- A：裁剪以「act + 观察回灌」配对原子单元为粒度——治理器 ``_trim_messages`` 与
  react 降级 ``_trim`` 的丢弃切点均不得落在配对之间（role=tool 失去前置 tool_calls）；
- B：thinking 模式 ``reasoning_content`` 采集（流式/非流式）→ ReasonOutput 随行 →
  react turn 落链 → wire 层对 assistant 消息透传（DeepSeek 校验要求回传）。

全部离线确定性（MockTransport / 桩对象），零触网。
"""

from __future__ import annotations

import asyncio

import httpx

from agent.api import Services
from agent.core.request_cost import estimate_request_cost, wire_messages
from agent.plugins.context.governor import ContextGovernor
from agent.plugins.react.components import DefaultReasoner
from agent.plugins.react.loop import ReactLoop, _trim
from agent.sys.llm import DeepSeekLlm, LlmService


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 公共断言：wire 配对合法性（role=tool 必须紧跟带 tool_calls 的 assistant）
# ---------------------------------------------------------------------------

def _wire_pairs_valid(messages: list[dict]) -> None:
    wired = wire_messages(messages)
    prev_has_calls = False
    for i, m in enumerate(wired):
        if m.get("role") == "tool":
            assert prev_has_calls, f"wire[{i}] role=tool 缺少前置 tool_calls（拆对）"
        prev_has_calls = (m.get("role") == "assistant"
                          and bool(m.get("tool_calls")))


def _act(key: str, size: int = 400, pinned: bool = False) -> dict:
    """act 条目：id=act_{key}，工具调用 id=c_{key}。"""
    return {"role": "assistant", "content": "a" * size, "pinned": pinned,
            "id": f"act_{key}",
            "tool_calls": [{"id": f"c_{key}", "name": "t.x", "arguments": {"p": 1}}]}


def _observe(key: str, size: int = 400, pinned: bool = False) -> dict:
    """观察回灌条目：id=obs_{key}，tool_call_id 与 ``_act(key)`` 配对。"""
    return {"role": "user", "pinned": pinned, "id": f"obs_{key}",
            "content": {"tool_results": [{"tool_call_id": f"c_{key}",
                                          "name": "t.x", "content": "o" * size}]}}


# ---------------------------------------------------------------------------
# A：治理器裁剪保配对
# ---------------------------------------------------------------------------

def test_trim_messages_never_splits_act_observe_pair():
    """A：预算切点落在 act/observe 之间时，必须以单元为粒度整体保留/丢弃。

    旧实现按单条贪心：最新观察先占、其前置 act 被丢 → role=tool 失去前置 → 400。
    预算 150 只够一条观察（≈100）不够一个单元（≈205）——旧实现恰在此拆对。
    """
    g = ContextGovernor(max_context_tokens=100_000, reserve_output_tokens=0)
    msgs = [_observe("0"), _act("1"), _observe("1"), _act("2"), _observe("2")]
    kept, _down, _clipped = g._trim_messages(msgs, 150)
    assert kept, "末位单元保底不得全弃"
    ids = {m.get("id") for m in kept}
    assert ("act_2" in ids) == ("obs_2" in ids), "act 与其观察必须同进同退"
    _wire_pairs_valid(kept)


def test_trim_messages_pinned_observe_pulls_its_act():
    """A：pinned 观察先占额度时，其前置 act 必须连带保留（配对原子性）。"""
    g = ContextGovernor(max_context_tokens=100_000, reserve_output_tokens=0)
    msgs = [_act("1"), _observe("1", pinned=True)]
    kept, downgraded, _ = g._trim_messages(msgs, 10_000)
    assert downgraded == []
    ids = {m.get("id") for m in kept}
    assert "obs_1" in ids and "act_1" in ids, "pinned 观察连带其 act 保留"
    _wire_pairs_valid(kept)


def test_trim_messages_last_unit_fallback_keeps_pair_structure():
    """A：末位单元保底——整单元保留、只裁文本载荷，配对结构不破。"""
    g = ContextGovernor(max_context_tokens=100_000, reserve_output_tokens=0)
    msgs = [_act("1", size=4000), _observe("1", size=4000)]
    kept, _down, clipped = g._trim_messages(msgs, 120)
    assert clipped is True
    assert len(kept) == 2, "保底必须整单元保留"
    assert len(kept[0]["tool_calls"]) == 1, "act 的 tool_calls 结构不破"
    tr = kept[1]["content"]["tool_results"][0]
    assert tr["tool_call_id"] == "c_1", "观察元数据不破"
    assert len(tr["content"]) < 4000, "观察文本载荷被裁"
    _wire_pairs_valid(kept)


def test_react_trim_fallback_shifts_cut_off_pair_boundary():
    """A：react 降级条数裁剪——切点撞上观察回灌时回退到配对边界之外。"""
    msgs = [{"role": "user", "content": "f"}] + \
           [_act("1"), _observe("1"), _act("2"), _observe("2")]
    # keep=1：朴素切点 = len-1 → 恰落在 act_2 与 obs_2 之间
    out = _trim(msgs, 1)
    _wire_pairs_valid(out)
    ids = [m.get("id") for m in out if m.get("id")]
    assert "act_2" in ids and "obs_2" in ids, "act_2/obs_2 必须成对保留"


# ---------------------------------------------------------------------------
# B：reasoning_content 采集 → 随行 → 回传
# ---------------------------------------------------------------------------

def _svc(transport) -> LlmService:
    return LlmService(DeepSeekLlm("m", "k", transport=transport), "m")


class _Bus:
    def __init__(self):
        self.frames: list[tuple] = []

    async def emit(self, name, payload=None):
        self.frames.append((name, payload))

    async def waterfall(self, name, payload=None):
        return payload


class _Db:
    def __init__(self):
        self.rows: list[tuple] = []

    async def execute(self, ctx, sql, params):
        self.rows.append((sql, params))


class _LlmCtx:
    def __init__(self, stream: bool = True):
        self.config = {"agent": {"llm": {"stream": stream}}}
        self.events = _Bus()
        self.db = _Db()

    def get(self, svc):
        return self.db if svc == Services.DB else None


def test_non_stream_captures_reasoning_content():
    """B①：非流式响应的 reasoning_content 必须随结果返回（此前被直接丢弃）。"""
    body = {"choices": [{"message": {"role": "assistant", "content": "答案",
                                     "reasoning_content": "思考过程"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    def handler(request):
        return httpx.Response(200, json=body)

    svc = _svc(httpx.MockTransport(handler))
    ctx = _LlmCtx(stream=False)

    async def s():
        r = await svc.complete(ctx, [{"role": "user", "content": "q"}], caller="test")
        assert r.data.get("reasoning_content") == "思考过程"
        await svc._provider.close()

    _run(s())


def test_stream_captures_reasoning_content_even_for_tool_calls():
    """B①：流式思考帧须累积并随结果返回（含工具调用轮；schema 门控不再吞思考链）。"""
    sse = (
        'data: {"choices":[{"delta":{"reasoning_content":"想一"}}]}\n\n'
        'data: {"choices":[{"delta":{"reasoning_content":"想二"}}]}\n\n'
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"fs","arguments":"{}"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        return httpx.Response(200, text=sse)

    svc = _svc(httpx.MockTransport(handler))
    ctx = _LlmCtx(stream=True)

    async def s():
        r = await svc.complete(ctx, [{"role": "user", "content": "q"}], caller="test")
        assert r.data.get("reasoning_content") == "想一想二"
        assert r.data["tool_calls"][0]["id"] == "c1"
        assert any(n.endswith("llm/thinking") for n, _p in ctx.events.frames), \
            "思考帧仍须照常流给前端"
        await svc._provider.close()

    _run(s())


def test_reasoner_output_carries_reasoning_and_wire_passes_it_back():
    """B②③：ReasonOutput 随行思考链 → turn 落链形状 → wire 对 assistant 透传。"""
    class _Lr:
        data = {"text": "答", "tool_calls": [], "reasoning_content": "链"}
        tokens_in = 1
        tokens_out = 1

    class _Llm:
        async def complete(self, ctx, messages, tools=None, caller="", **kw):
            return _Lr()

    class _Ctx:
        llm = _Llm()

    out = _run(DefaultReasoner().reason(_Ctx(), [], []))
    assert out.reasoning_content == "链", "Reasoner 必须把思考链带出"

    turn = {"role": "assistant", "content": out.text, "tool_calls": [],
            "type": "think", "id": "step1_think"}
    if getattr(out, "reasoning_content", ""):
        turn["reasoning_content"] = out.reasoning_content
    msgs = [{"role": "user", "content": "q"}, turn,
            {"role": "assistant", "content": "旧轮", "reasoning_content": "旧链"}]
    wired = wire_messages(msgs)
    assert wired[1]["reasoning_content"] == "链", "wire 必须透传 assistant 思考链"
    assert wired[2]["reasoning_content"] == "旧链", "历史轮思考链同样回传"
    assert "reasoning_content" not in wired[0], "user 消息不得夹带该字段"
    assert estimate_request_cost(msgs) > 0, "估算口径不因新字段崩坏"


def test_reasoner_act_turn_replays_with_reasoning_and_pairing():
    """B：act 轮（带 tool_calls）回放时思考链与 tool_calls 同条携带（DeepSeek wire 形状）。"""
    turn = {"role": "assistant", "content": "", "id": "step3_act", "type": "act",
            "reasoning_content": "先想",
            "tool_calls": [{"id": "c9", "name": "t.w",
                            "arguments": {"path": "a.md", "content": "正文"}}]}
    observe = {"role": "user", "content": {"tool_results": [
        {"tool_call_id": "c9", "name": "t.w", "content": "ok"}]}}
    wired = wire_messages([{"role": "user", "content": "q"}, turn, observe])
    assert wired[1]["tool_calls"][0]["function"]["name"] == "t.w"
    assert wired[1]["reasoning_content"] == "先想"
    assert wired[2]["role"] == "tool" and wired[2]["tool_call_id"] == "c9"
    _wire_pairs_valid([{"role": "user", "content": "q"}, turn, observe])
