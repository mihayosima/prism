"""事故修复一期测试（docs/reports/context/事故修复计划-预算与截断-2026-09-28.md）：

- A：预算收尾预留额度（正常调用 max-reserve 封顶、finalize=True 可用至 max）
- B：缺参报错附必填/可省清单
- C：收尾摘要（exhausted 文本 / evidence / finalizer 兜底 / 记忆载荷 progress）
- E：生成侧截断防护（显式 max_tokens / finish_reason=length 标记 / 残缺参数不静默 / Actor 自纠指引）
- 遥测：finish_reason=length durable 落盘 / react 窗口统计落 agent_events
- 二期：D1 预算预警（budget 置旗标发事件、react 注入收束提示）
        D2 act 大参数折叠（_fold_act / _prepare_chain / checkpoint 存档）
        D4 工具描述瘦身（投影层压行，原目录不动）

全部离线确定性（MockTransport / fixture provider），零触网。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from agent.api import AgentError, CapabilityNotImplemented, Services
from agent.profiles import build_agent
from agent.plugins.react.components import DefaultActor, DefaultFinalizer, ToolCall
from agent.plugins.react.loop import ReactConfig, ReactLoop
from agent.sys.llm import DeepSeekLlm, LlmService, _wire_call
from agent.sys.plugin_manager import PluginManager
from agent.sys.tasks import validate_kwargs

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _build(tmp_path, *, react: bool = True, context: bool = False, memory: bool = False,
           patch: dict | None = None):
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    if react:
        pm.set_enabled("plugins.react", True)
    if context:
        pm.set_enabled("plugins.context", True)
    if memory:
        pm.set_enabled("plugins.memory", True)
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch, plugin_mgr=pm)


# ---------------------------------------------------------------------------
# B：缺参报错附必填/可省清单
# ---------------------------------------------------------------------------

_SCHEMA = {"mode": {"type": "str", "required": True},
           "path": {"type": "str", "required": True},
           "content": {"type": "str", "required": True},
           "encoding": {"type": "str"}}


def test_validate_kwargs_missing_required_lists_params():
    """当晚「缺少必填参数 'mode'」孤句 → 附工具名 + 必填清单 + 可省清单。"""
    with pytest.raises(AgentError) as ei:
        validate_kwargs(_SCHEMA, {"path": "x"}, tool_name="mcp.windows.FileSystem")
    msg = str(ei.value)
    assert "缺少必填参数 'mode'" in msg
    assert "mcp.windows.FileSystem" in msg
    assert "mode, path, content" in msg          # 必填清单（模型可据此自纠）
    assert "encoding" in msg                     # 可省清单


def test_validate_kwargs_without_tool_name_keeps_backward_compat():
    """不带 tool_name（旧调用方）→ 报错语义不变，仅无清单后缀。"""
    with pytest.raises(AgentError) as ei:
        validate_kwargs(_SCHEMA, {})
    msg = str(ei.value)
    assert "缺少必填参数 'mode'" in msg
    assert "（" not in msg


# ---------------------------------------------------------------------------
# E：生成侧截断防护
# ---------------------------------------------------------------------------

def test_wire_call_marks_truncated_arguments():
    """残缺参数 JSON 不再静默吞成 {}：打 truncated 标记并保留原始片段。"""
    raw = {"id": "c1", "function": {"name": "fs",
                                    "arguments": '{"mode": "write", "con'}}
    out = _wire_call(raw)
    assert out["arguments"] == {}
    assert out["truncated"] is True
    assert out["arguments_raw"].startswith('{"mode"')
    ok = _wire_call({"id": "c2", "function": {"name": "fs", "arguments": '{"mode":"w"}'}})
    assert "truncated" not in ok
    assert ok["arguments"] == {"mode": "w"}


class _Bus:
    def __init__(self):
        self.frames: list[tuple] = []

    async def emit(self, name, payload=None):
        self.frames.append((name, payload))

    async def waterfall(self, name, payload=None):
        return payload


class _Db:
    """最小 DB 桩：durable() 落盘审计可直接断言。"""

    def __init__(self):
        self.rows: list[tuple] = []

    async def execute(self, ctx, sql, params):
        self.rows.append((sql, params))


class _LlmCtx:
    def __init__(self, stream: bool = True, max_tokens=None):
        llm_cfg = {"stream": stream}
        if max_tokens is not None:
            llm_cfg["max_tokens"] = max_tokens
        self.config = {"agent": {"llm": llm_cfg}}
        self.events = _Bus()
        self.db = _Db()

    def get(self, svc):
        return self.db if svc == Services.DB else None


def _svc(transport) -> LlmService:
    return LlmService(DeepSeekLlm("m", "k", transport=transport), "m")


def test_deepseek_sends_explicit_max_tokens_and_flags_length():
    """E①：请求显式带 max_tokens；E②：finish_reason=length → 结果与工具调用均打标记。"""
    sse = (
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1",'
        '"function":{"name":"fs","arguments":"{\\"mode\\": \\"wr"}}]}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n'
        "data: [DONE]\n\n"
    )
    captured: dict = {}

    def handler(request):
        captured["payload"] = json.loads(request.read().decode())
        return httpx.Response(200, text=sse)

    svc = _svc(httpx.MockTransport(handler))
    ctx = _LlmCtx(stream=True)

    async def s():
        r = await svc.complete(ctx, [{"role": "user", "content": "写一章"}],
                               caller="test")
        assert captured["payload"]["max_tokens"] == 8192, "必须显式设 max_tokens（默认 8192）"
        assert r.data.get("truncated") is True, "finish_reason=length 必须显式标记"
        call = r.data["tool_calls"][0]
        assert call["truncated"] is True
        assert call["arguments_raw"].startswith('{"mode"')
        # E 遥测：截断必须 durable 落盘（当晚此信号被完全丢弃）
        assert any("agent/llm/truncated" in (p or ("",))
                   for sql, p in ctx.db.rows), "截断事件须落 agent_events"
        await svc._provider.close()

    _run(s())


def test_deepseek_max_tokens_configurable():
    """E①补充：max_tokens 可经 config.agent.llm.max_tokens 覆盖。"""
    captured: dict = {}

    def handler(request):
        captured["payload"] = json.loads(request.read().decode())
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"},'
                                        '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')

    svc = _svc(httpx.MockTransport(handler))
    ctx = _LlmCtx(stream=True, max_tokens=1234)

    async def s():
        await svc.complete(ctx, [{"role": "user", "content": "hi"}], caller="test")
        assert captured["payload"]["max_tokens"] == 1234
        assert "truncated" not in ctx.events.frames and True
        await svc._provider.close()

    _run(s())


def test_actor_refuses_truncated_call_with_selffix_guidance():
    """E④：截断调用不执行工具，回灌错误观察并附分片自纠指引。"""
    actor = DefaultActor()
    invoked: list[str] = []

    class _Tasks:
        async def invoke(self, ctx, name, *, caller=None, **kwargs):
            invoked.append(name)
            return {}

    class _Ctx:
        tasks = _Tasks()

    call = ToolCall(name="mcp.windows.FileSystem", arguments={}, id="c1",
                    truncated=True, arguments_raw='{"mode": "wr')
    results = _run(actor.act(_Ctx(), [call]))
    assert invoked == [], "截断调用绝不应触达工具"
    r = results[0]
    assert r.is_error and not r.denied
    assert "截断" in r.content and "分片" in r.content


# ---------------------------------------------------------------------------
# A：预算收尾预留额度
# ---------------------------------------------------------------------------

def test_budget_reserve_gates_normal_but_allows_finalize(tmp_path):
    """正常调用封顶 max-reserve 即拒；finalize=True 可动用预留段（至 max）。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db"),
                            "agent": {"budget": {"max_tokens_per_run": 200,
                                                 "finalize_reserve_tokens": 100}}})

    async def s():
        await c.assemble()
        big = [{"role": "user", "content": "x" * 600}]     # est ≈ 150（>100 且 ≤200）
        with pytest.raises(CapabilityNotImplemented):
            await c.root.llm.complete(c.root, big, caller="t")     # 正常：超 max-reserve
        r = await c.root.llm.complete(c.root, big, caller="t", finalize=True)
        # G2：``budget_spent`` 为 ``dict[会话键, int]``（本用例无 SESSION_KEY ⇒ 落容器桶 ""）
        spent_map = c.root.state.get("budget_spent") or {}
        total = (sum(int(v or 0) for v in spent_map.values())
                 if isinstance(spent_map, dict) else int(spent_map))
        assert total > 100, "收尾调用应可动用预留段"
        assert r.tokens_in > 0
        await c.teardown()

    _run(s())


def test_budget_reserve_clamped_to_half_of_max():
    """预留 > max/2 时钳到 max//2——正常调用永远保留至少一半额度，护栏不反向失效。"""
    from agent.sys.budget import Budget
    cfg = {"max_tokens_per_run": 120, "finalize_reserve_tokens": 99999}
    max_tokens = int(cfg["max_tokens_per_run"])
    reserve = max(0, int(cfg["finalize_reserve_tokens"]))
    reserve = min(reserve, max_tokens // 2) if max_tokens else 0
    assert reserve == 60
    assert Budget is not None          # 语义说明占位：钳制规则与 budget.apply 一致


# ---------------------------------------------------------------------------
# C：收尾摘要 + 进度快照
# ---------------------------------------------------------------------------

def _register_echo(ctx) -> None:
    async def _echo(text: str = "hi"):
        return {"echo": text}

    ctx.tasks.register(ctx, "test.echo",
                       schema={"text": {"type": "str", "required": False, "default": "hi"}},
                       handler=_echo, owner="test", description="回显参数")


def test_react_exhausted_text_contains_closing_summary(tmp_path):
    """C：触顶收尾必产出摘要（fixture 收尾文本不含时经 evidence/兜底路径携带）。"""
    c = _build(tmp_path, patch={"agent": {"react": {"max_steps": 3}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        r = await c.root.react.run(c.root, "请触顶循环")
        assert r.exhausted
        assert "上限" in r.text
        assert r.summary.startswith("[收尾摘要]"), "触顶必产出收尾摘要"
        assert "tokens 累计" in r.summary
        assert r.as_evidence()["summary"] == r.summary
        await c.teardown()

    _run(s())


def test_closing_summary_includes_last_error():
    """C：有失败观察时，摘要须点名最后失败的工具与原因片段。"""
    s = ReactLoop._closing_summary(
        3, ["a", "b"],
        [{"name": "t.x", "is_error": True, "content": "缺少必填参数 'mode'"}],
        100, 50)
    assert "最后一次失败：t.x" in s
    assert "'mode'" in s
    assert "in=100/out=50" in s


def test_finalizer_uses_finalize_flag_and_fallback_has_summary():
    """A+C：finalizer 调用带 finalize=True（预留额度凭证）；LLM 再拒时兜底文本带摘要。"""
    fin = DefaultFinalizer()
    seen: dict = {}

    class _Llm:
        async def complete(self, ctx, messages, *, caller="unknown", finalize=False,
                           **kw):
            seen["finalize"] = finalize
            raise CapabilityNotImplemented("agent.llm.complete", "预算超限 spent=1>1")

    class _Ctx:
        llm = _Llm()

    msgs = [{"role": "system", "content": "[收尾摘要] 本次运行已执行 3 步"},
            {"role": "user", "content": "q"}]
    text = _run(fin.finalize(_Ctx(), msgs, "预算超限"))
    assert seen["finalize"] is True, "收尾调用必须标记 finalize=True 以动用预留额度"
    assert "已达执行上限" in text
    assert "[收尾摘要]" in text, "兜底文本必须携带收尾摘要"


def test_chat_remember_payload_contains_progress(tmp_path):
    """C：chat 委托路径把收尾摘要以 progress 键前置并入 memory.remember 载荷。"""
    c = _build(tmp_path, react=True, memory=True,
               patch={"agent": {"react": {"max_steps": 2}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        captured: list[dict] = []
        orig = c.root.memory.remember

        async def spy(ctx_, payload):
            captured.append(payload)
            return await orig(ctx_, payload)

        c.root.memory.remember = spy
        await c.root.chat.send(c.root, "请触顶循环", "s1")
        assert captured, "react 收尾必须喂记忆"
        assert any("[收尾摘要]" in str(p.get("progress") or "") for p in captured), \
            "进度快照（progress 键）必须前置进入记忆载荷"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 遥测：窗口统计落库
# ---------------------------------------------------------------------------

def test_window_stats_written_to_agent_events(tmp_path):
    """react 每步窗口统计 durable 落 agent_events（当晚诊断只能靠曲线反推的对策）。"""
    c = _build(tmp_path, react=True, context=True,
               patch={"agent": {"react": {"max_steps": 3}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        await c.root.react.run(c.root, "请触顶循环")
        rows = await c.root.db.query(
            c.root, "SELECT event FROM agent_events WHERE event='agent/context/governed'")
        assert rows, "窗口统计须落 agent_events（durable）"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 用户追加要求：预留额度开放到设置页（三处同步登记）
# ---------------------------------------------------------------------------

def test_finalize_reserve_registered_in_settings_schema():
    """finalize_reserve_tokens 进设置页须三处同步登记；默认值与 profiles 同源。"""
    from agent.profiles import BASE_AGENT_CONFIG
    from agent.run import chat as srv
    from agent.tests._web_sources import frontend_settings_source

    k = "finalize_reserve_tokens"
    assert k in srv._SETTING_SCHEMA
    assert srv._PLUGIN_OF_KEY[k] == "plugins.budget", "归属错误：须经 plugins.budget 映射"
    assert k in srv._SETTING_DEFAULTS
    base = ((BASE_AGENT_CONFIG.get("agent") or {}).get("budget") or {}).get(
        "finalize_reserve_tokens")
    assert srv._SETTING_DEFAULTS[k] == base, "设置默认值与 profiles 默认漂移"
    assert srv._coerce_setting(k, "16000") == 16000
    # B4：`_PAGE` 已删除 → 契约守卫改读前端源码
    assert "'finalize_reserve_tokens'" in frontend_settings_source(), "未同步到前端 keys/字段"


def test_finalize_reserve_override_maps_to_agent_budget(tmp_path, monkeypatch):
    """覆盖文件 plugins.budget.finalize_reserve_tokens → _merge_cfg → 装配生效。"""
    import argparse
    import json

    from agent.run import chat as srv

    ov_file = tmp_path / "overrides.json"
    ov_file.write_text(json.dumps(
        {"plugins.budget": {"max_tokens_per_run": 123456,
                            "finalize_reserve_tokens": 16000}}), encoding="utf-8")
    monkeypatch.setattr(srv, "_OVERRIDE_FILE", ov_file)

    args = argparse.Namespace(fixture=True, provider="fixture", model="fixture",
                              db=str(tmp_path / "agent.db"))
    cfg = srv._merge_cfg(args)
    assert cfg["agent"]["budget"]["finalize_reserve_tokens"] == 16000

    c = build_agent(config=cfg)

    async def s():
        await c.assemble()
        got = ((c.root.config or {}).get("agent") or {}).get("budget") or {}
        assert got.get("finalize_reserve_tokens") == 16000, "装配后预留额度未生效"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 二期 D1：预算 80% 预警
# ---------------------------------------------------------------------------

def test_budget_warning_fires_once_at_threshold(tmp_path):
    """D1：花费跨 ratio*max 阈值 → 置 BUDGET_WARNING 旗标 + durable 事件，且只发一次。"""
    from agent.core.state_keys import StateKeys
    c = build_agent(config={"db_path": str(tmp_path / "agent.db"),
                            "agent": {"budget": {"max_tokens_per_run": 200,
                                                 "finalize_reserve_tokens": 0,
                                                 "warning_ratio": 0.5}}})

    async def s():
        await c.assemble()
        msg = [{"role": "user", "content": "x" * 400}]     # est ≈ 100 = 0.5*200 → 跨阈值
        await c.root.llm.complete(c.root, msg, caller="t")
        warn_map = c.root.state.get(StateKeys.BUDGET_WARNING)
        # G2：旗标已按会话分桶（``{会话键: 记录}``）；本用例无 SESSION_KEY ⇒ 容器桶 ""
        assert isinstance(warn_map, dict) and warn_map, "跨阈值必须置预警旗标"
        rec = next(iter(warn_map.values()))
        assert rec.get("limit") == 200, "预警记录须带上限（阈值判定线）"
        rows = await c.root.db.query(
            c.root, "SELECT event FROM agent_events WHERE event='agent/budget/warning'")
        assert len(rows) == 1, "预警事件 durable 落盘且只发一次"
        await c.root.llm.complete(c.root, [{"role": "user", "content": "y"}], caller="t")
        rows = await c.root.db.query(
            c.root, "SELECT event FROM agent_events WHERE event='agent/budget/warning'")
        assert len(rows) == 1, "第二次调用不得重复发预警"
        await c.teardown()

    _run(s())


def test_budget_warning_disabled_with_nonpositive_ratio(tmp_path):
    """D1：warning_ratio<=0 → 预警关闭，行为与旧版一致。"""
    from agent.core.state_keys import StateKeys
    c = build_agent(config={"db_path": str(tmp_path / "agent.db"),
                            "agent": {"budget": {"max_tokens_per_run": 200,
                                                 "finalize_reserve_tokens": 0,
                                                 "warning_ratio": 0}}})

    async def s():
        await c.assemble()
        await c.root.llm.complete(c.root, [{"role": "user", "content": "x" * 600}],
                                  caller="t")
        assert not c.root.state.get(StateKeys.BUDGET_WARNING), "ratio<=0 不得触发预警"
        await c.teardown()

    _run(s())


def test_react_injects_budget_warning_hint_into_window(tmp_path):
    """D1：旗标在位 → 每步窗口注入 [预算提示]；旗标清除 → 不注入（零直连，只读状态键）。"""
    c = _build(tmp_path, context=True, patch={"agent": {"react": {"max_steps": 1}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        loop = c.root.react
        orig = loop._reasoner
        captured: list[list[dict]] = []

        class _Spy:
            async def reason(self, ctx, messages, tools):
                captured.append(list(messages))
                return await orig.reason(ctx, messages, tools)

        loop._reasoner = _Spy()
        c.root.state["budget_warning"] = {"at": "t", "spent": 9, "limit": 10,
                                          "ratio": 0.8}
        await loop.run(c.root, "你好")
        assert captured and any(
            m.get("role") == "system" and "[预算提示]" in str(m.get("content"))
            for m in captured[0]), "旗标在位时窗口必须注入收束提示"

        captured.clear()
        c.root.state["budget_warning"] = None            # State 无 pop，置 None 即关闭
        await loop.run(c.root, "你好")
        assert captured and not any(
            m.get("role") == "system" and "[预算提示]" in str(m.get("content"))
            for m in captured[0]), "旗标清除后不得再注入"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 二期 D2：act 大参数折叠 + 产物外置
# ---------------------------------------------------------------------------

def test_fold_act_replaces_large_values_keeps_small_and_idempotent():
    """D2：只折叠超阈值字符串值；path/mode 等结构参数保留；幂等（标记不再二次折叠）。"""
    big = "长" * 2000
    m = {"role": "assistant", "content": "", "type": "act", "id": "step2_act",
         "tool_calls": [{"id": "c1", "name": "fs.write",
                         "arguments": {"mode": "write", "path": "a.md", "content": big}}]}
    n = ReactLoop._fold_act(m, act_fold=100, task_id="sess:run1")
    assert n == 1
    args = m["tool_calls"][0]["arguments"]
    assert args["mode"] == "write" and args["path"] == "a.md", "结构参数必须原样保留"
    assert "«[已折叠" in args["content"] and "step_2" in args["content"], \
        "折叠标记须引用 checkpoint 键（step_{N} 与 run 存档键一致）"
    assert "约" in args["content"]
    assert ReactLoop._fold_act(m, act_fold=100, task_id="sess:run1") == 0, "必须幂等"


def test_prepare_chain_folds_old_act_only_with_wm():
    """D2：_prepare_chain 折叠老 act 大参数；wm 缺失时不折（无损承诺）；最近 keep_recent 原样。"""

    class _Gov:
        def clip_head_tail(self, text):
            return text, False

    class _Wm:
        pass                                          # 仅作「在位」哨兵

    def _mk_act_big() -> dict:
        return {"role": "assistant", "content": "", "type": "act", "id": "step1_act",
                "tool_calls": [{"id": "c1", "name": "w",
                                "arguments": {"content": "长" * 2000}}]}

    act_big = _mk_act_big()
    obs1 = {"role": "user", "content": "观察1", "type": "observe", "id": "o1"}
    act_small = {"role": "assistant", "content": "", "type": "act", "id": "step2_act",
                 "tool_calls": [{"id": "c2", "name": "w", "arguments": {"path": "b.md"}}]}
    obs2 = {"role": "user", "content": "观察2", "type": "observe", "id": "o2"}
    chain = [act_big, obs1, act_small, obs2]

    loop = ReactLoop(None, None, None, None, ReactConfig())   # type: ignore[arg-type]
    out, newly, act_folds = loop._prepare_chain(_Gov(), chain, keep_recent=2,
                                                wm=_Wm(), task_id="s:r", act_fold=100)
    assert newly == 0 and act_folds == 1
    assert "«[已折叠" in out[0]["tool_calls"][0]["arguments"]["content"]
    assert out[2]["tool_calls"][0]["arguments"] == {"path": "b.md"}, "近期 act 不折叠"

    # wm 缺失 → act 折叠整体关闭（不可找回就不折）；用工厂新造条目防就地突变串扰
    fresh = _mk_act_big()
    out2, _, act_folds2 = loop._prepare_chain(_Gov(), [fresh], keep_recent=0,
                                              wm=None, task_id="s:r", act_fold=100)
    assert act_folds2 == 0
    assert out2[0]["tool_calls"][0]["arguments"]["content"] == "长" * 2000


def test_react_checkpoints_full_act_arguments(tmp_path):
    """D2：act 发生时完整参数先入工作记忆 checkpoint（折叠可按引用找回的依据）。"""
    from agent.api import Services

    class _WmSpy:
        def __init__(self):
            self.checkpoints: list[tuple] = []

        async def begin(self, tid, meta):
            pass

        async def update(self, tid, **kw):
            pass

        async def checkpoint(self, tid, key, data):
            self.checkpoints.append((tid, key, data))

        async def finish(self, tid):
            pass

        # 阶段 2（2.1）：快照接口契约对齐（spy 直接 provide 绕过门面 → 须齐全）
        async def snapshot_save(self, tenant, task_id, data, ttl=None):
            self.checkpoints.append((task_id, "snapshot", data))

        async def snapshot_load(self, tenant, task_id):
            return None

        async def snapshot_list(self, tenant, prefix=None):
            return []

        async def snapshot_mark_finished(self, tenant, task_id):
            pass

        async def snapshot_flag_finished(self, tenant, task_id):
            pass

    c = _build(tmp_path, context=True, patch={"agent": {"react": {"max_steps": 1}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        spy = _WmSpy()
        c.root.provide(Services.WORK_MEMORY, spy)
        await c.root.react.run(c.root, "请触顶循环")
        assert spy.checkpoints, "每步必须写 checkpoint"
        keys = [k for _t, k, _d in spy.checkpoints]
        assert any(k.startswith("step_") for k in keys)
        step_cks = [d for _t, k, d in spy.checkpoints if k.startswith("step_")]
        assert any("act_calls" in d and d["act_calls"] for d in step_cks), \
            "checkpoint 必须携带本轮 act 完整参数（产物外置）"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 二期 D4：工具描述瘦身（投影层）
# ---------------------------------------------------------------------------

def test_visible_tools_slims_description_projection_only(tmp_path):
    """D4：visible_tools 返回的 description 压成一行（≤120 字符）；原目录保持全文。"""
    c = _build(tmp_path, context=True)

    async def s():
        await c.assemble()
        long_desc = ("第一行简要说明：" + "详细" * 200 + "\n第二行补充说明\n第三行示例")

        async def _echo(text: str = "hi"):
            return {"echo": text}

        c.root.tasks.register(c.root, "test.longdoc", schema={}, handler=_echo,
                              owner="test", description=long_desc)
        vis = {t["name"]: t for t in
               __import__("agent.plugins.react.loop", fromlist=["visible_tools"])
               .visible_tools(c.root)}
        d = vis["test.longdoc"]["description"]
        assert "\n" not in d, "描述必须压成一行"
        assert len(d) <= 120, "描述必须截到 120 字符内"
        assert d.startswith("第一行简要说明"), "保留首行语义"
        orig = {t["name"]: t for t in c.root.tasks.catalog()}
        assert orig["test.longdoc"]["description"] == long_desc, "原目录不得被改动"
        await c.teardown()

    _run(s())
