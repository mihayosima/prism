"""后置课题实施测试（docs/governance/后置课题实施计划-口径链与懒加载-2026-09-29.md）：

- F1：口径校准因子（EWMA 维护 / cost_factor 消费 / 样本门槛 / 预借判定乘因子）
- F2：agent_budget_log.run_id 归组（react 经 REACT_RUN_ID 状态键）
- F3：口径漂移 >25% durable 事件
- P1/P2：链存档（react_chain:{session}）与 resume 续接；chat 关键词意图
- P3：账单 cache_hit 列迁移
- L1：懒加载（目录卡 / 元工具内联处理 / loaded 上限 / 分步工具视图）
- 设置页：cost_calibration / lazy_tools 三处同步登记

全部离线确定性，零触网。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.api import Services, StateKeys
from agent.plugins.react.components import ToolCall
from agent.plugins.react.loop import ReactConfig, ReactLoop, _card, _slim_description
from agent.profiles import BASE_AGENT_CONFIG, build_agent
from agent.run import chat as srv
from agent.sys.llm import LlmService
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _build(tmp_path, *, react: bool = True, context: bool = False,
           work_memory: bool = False, patch: dict | None = None):
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    if react:
        pm.set_enabled("plugins.react", True)
    if context:
        pm.set_enabled("plugins.context", True)
    if work_memory:
        pm.set_enabled("plugins.work_memory", True)
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch, plugin_mgr=pm)


def _register_echo(ctx) -> None:
    async def _echo(text: str = "hi"):
        return {"echo": text}

    ctx.tasks.register(ctx, "test.echo",
                       schema={"text": {"type": "str", "required": False, "default": "hi"}},
                       handler=_echo, owner="test", description="回显参数")


# ---------------------------------------------------------------------------
# F1：口径校准因子
# ---------------------------------------------------------------------------

def test_cost_factor_applied_to_budget_judgement(tmp_path):
    """F1：样本达标后估算×因子参与预借判定（因子 2.0 → 提前触发拒绝线）。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db"),
                            "agent": {"budget": {"max_tokens_per_run": 200,
                                                 "finalize_reserve_tokens": 0}}})

    async def s():
        await c.assemble()
        big = [{"role": "user", "content": "x" * 600}]     # 裸估算 ≈150 < 200
        # 样本不足（门槛 5）→ 因子不生效，不拒
        c.root.state[StateKeys.LLM_COST_RATIO] = {"fixture": {"ratio": 2.0, "samples": 2}}
        await c.root.llm.complete(c.root, big, caller="t")
        # 样本达标 → est×2 ≈300 > 200 → 拒绝
        c.root.state[StateKeys.LLM_COST_RATIO] = {"fixture": {"ratio": 2.0, "samples": 9}}
        from agent.api import CapabilityNotImplemented
        with pytest.raises(CapabilityNotImplemented):
            await c.root.llm.complete(c.root, big, caller="t")
        await c.teardown()

    _run(s())


def test_ewma_maintained_after_calls(tmp_path):
    """F1：每次调用后 EWMA 维护（样本数递增，ratio 落在夹紧区间）。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")})

    async def s():
        await c.assemble()
        for _ in range(2):
            await c.root.llm.complete(c.root, [{"role": "user", "content": "你好"}],
                                      caller="t")
            for _ in range(5):                   # _post 是 async fire-and-forget
                await asyncio.sleep(0)           # 且同一 caller 的 pending 槽会被下次覆盖
        stats = c.root.state.get(StateKeys.LLM_COST_RATIO) or {}
        m = stats.get("fixture") or {}
        assert int(m.get("samples") or 0) >= 2, "每次调用须喂 EWMA 样本"
        assert 0.5 <= float(m.get("ratio") or 0) <= 3.0
        await c.teardown()

    _run(s())


def test_cost_calibration_off_disables_factor(tmp_path):
    """F1：cost_calibration=False → 因子不参与判定（裸估算现状）。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db"),
                            "agent": {"budget": {"max_tokens_per_run": 200,
                                                 "finalize_reserve_tokens": 0,
                                                 "cost_calibration": False}}})

    async def s():
        await c.assemble()
        c.root.state[StateKeys.LLM_COST_RATIO] = {"fixture": {"ratio": 9.0, "samples": 99}}
        await c.root.llm.complete(c.root, [{"role": "user", "content": "x" * 600}],
                                  caller="t")          # 裸估算 ≈150 < 200 → 不拒
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# F2 / F3 / P3：账单归组 · 漂移事件 · 缓存列
# ---------------------------------------------------------------------------

def test_budget_log_records_run_id(tmp_path):
    """F2：react 的 LLM 调用经 REACT_RUN_ID 状态键归组落账。"""
    c = _build(tmp_path, context=True)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        await c.root.react.run(c.root, "请触顶循环")
        for _ in range(5):                       # _post 是 async fire-and-forget，让出循环
            await asyncio.sleep(0)
        rows = await c.root.db.query(
            c.root, "SELECT run_id FROM agent_budget_log"
                    " WHERE run_id IS NOT NULL AND run_id != ''")
        assert rows, "react 调用的账单须带 run_id 归组键"
        await c.teardown()

    _run(s())


def test_budget_log_has_cache_hit_column(tmp_path):
    """P3：agent_budget_log 幂等迁移出 cache_hit 列（可写入可查询）。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")})

    async def s():
        await c.assemble()
        await c.root.db.execute(
            c.root, "INSERT INTO agent_budget_log(model, tokens_in, tokens_out,"
                    " est_cost, run_id, cache_hit, at) VALUES (?,?,?,?,?,?,?)",
            ("m", 1, 2, None, "r1", 500, "t"))
        rows = await c.root.db.query(
            c.root, "SELECT cache_hit, run_id FROM agent_budget_log WHERE run_id='r1'")
        assert rows and rows[0]["cache_hit"] == 500
        await c.teardown()

    _run(s())


def test_cost_drift_event_on_big_deviation(tmp_path):
    """F3：真实 usage 与估算偏差 >25% → durable agent/llm/cost-drift（容器内经 budget）。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")})

    class _Prov:
        label = "stub"

        async def complete(self, ctx, messages, schema=None, tools=None):
            return {"text": "ok",
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 10}}

    async def s():
        await c.assemble()
        c.root.llm._provider = _Prov()           # 注入大 usage 的 stub provider
        await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}], caller="t")
        for _ in range(5):                       # _post 是 async fire-and-forget，让出循环
            await asyncio.sleep(0)
        rows = await c.root.db.query(
            c.root, "SELECT event FROM agent_events WHERE event='agent/llm/cost-drift'")
        assert rows, "偏差 >25% 须落 drift 事件"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# P1 / P2：链存档与续接
# ---------------------------------------------------------------------------

def test_chain_archived_to_work_memory(tmp_path):
    """P1：run 收尾把折叠链存工作记忆 react_chain:{session}，并发 archived 事件。"""
    c = _build(tmp_path, context=True, work_memory=True,
               patch={"agent": {"react": {"max_steps": 3}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        await c.root.react.run(c.root, "请触顶循环")
        wm = c.root.get(Services.WORK_MEMORY)
        arc = await wm.get("react_chain:default")     # run 未传 session → default
        assert arc, "链存档必须落工作记忆"
        cps = json.loads(arc.get("checkpoints") or "[]")
        data = next((x.get("data") or {} for x in reversed(cps)
                     if x.get("name") == "chain"), {})
        assert data.get("chain"), "存档须携带链条目"
        rows = await c.root.db.query(
            c.root, "SELECT event FROM agent_events WHERE event='agent/react/archived'")
        assert rows, "存档须落 durable 事件"
        await c.teardown()

    _run(s())


def test_resume_loads_archived_chain(tmp_path):
    """P2：resume=True 时上轮链进入本轮窗口；resume=False 不进入。"""
    c = _build(tmp_path, context=True, work_memory=True,
               patch={"agent": {"react": {"max_steps": 2}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        await c.root.react.run(c.root, "请触顶循环")           # 第一轮：存档

        loop = c.root.react
        orig = loop._reasoner
        captured: list[list[dict]] = []

        class _Spy:
            async def reason(self, ctx, messages, tools):
                captured.append(list(messages))
                return await orig.reason(ctx, messages, tools)

        loop._reasoner = _Spy()
        await loop.run(c.root, "继续", session_id="default", resume=True)
        assert any(m.get("id") == "step1_act" for m in captured[0]), \
            "续接时窗口必须包含上轮链条目"

        captured.clear()
        await loop.run(c.root, "新任务", session_id="default", resume=False)
        assert not any(m.get("id") == "step1_act" for m in captured[0]), \
            "非续接不得携带上轮链"
        await c.teardown()

    _run(s())


def test_chat_resume_intent_keywords():
    """P2：chat 续接意图按词表匹配（配置化；空词表恒 False）。"""

    class _Ctx:
        config = {"agent": {"react": {"resume_keywords": ["继续", "continue"]}}}

    from agent.sys.chat import is_resume_intent
    assert is_resume_intent(_Ctx(), "继续写第10章") is True
    assert is_resume_intent(_Ctx(), "please continue") is True
    assert is_resume_intent(_Ctx(), "换个话题，讲讲天气") is False
    assert is_resume_intent(_Ctx(), "") is False

    class _NoKw:
        config = {"agent": {"react": {"resume_keywords": []}}}

    assert is_resume_intent(_NoKw(), "继续") is False


# ---------------------------------------------------------------------------
# L1：懒加载
# ---------------------------------------------------------------------------

_LAZY_CFG = ReactConfig(lazy_tools=True, lazy_tools_max=2)


def _lazy_tools():
    return [{"name": n, "description": f"{n} 工具的说明", "params": {"x": {}},
             "required": ["x"], "risk": "read", "owner": "t"}
            for n in ("a", "b", "c", "d")]


def test_lazy_partition_and_cap():
    """L1：元工具内联处理；loaded 上限诚实拒绝；未加载调用回错误观察。"""
    loop = ReactLoop(None, None, None, None, _LAZY_CFG)      # type: ignore[arg-type]
    from agent.plugins.react.loop import TaskContext
    task = TaskContext(loaded=set())
    calls = [ToolCall(name="tool_search", arguments={"keyword": "a"}, id="m1"),
             ToolCall(name="tool_load", arguments={"names": "a,b,c"}, id="m2"),
             ToolCall(name="d", arguments={}, id="u1")]
    meta, real = loop._partition_meta(task, calls, _lazy_tools())
    assert [r.name for r in meta] == ["tool_search", "tool_load", "d"]
    assert real == [], "未加载的 d 不得进入真实执行"
    assert task.loaded == {"a", "b"}, "上限 2 → 只装载前两个，c 暂缓"
    assert "暂缓" in meta[1].content and "c" in meta[1].content
    assert "未加载" in meta[2].content and "tool_load" in meta[2].content

    # 已加载后 → 进入真实执行
    calls2 = [ToolCall(name="a", arguments={"x": 1}, id="r1")]
    meta2, real2 = loop._partition_meta(task, calls2, _lazy_tools())
    assert meta2 == [] and [x.name for x in real2] == ["a"]


def test_lazy_tools_for_step_view():
    """L1：分步工具视图 = 元工具 + 已加载完整定义 + 其余目录卡（卡无 schema）。"""
    loop = ReactLoop(None, None, None, None, _LAZY_CFG)      # type: ignore[arg-type]
    from agent.plugins.react.loop import TaskContext
    task = TaskContext(loaded={"a"})
    view = loop._tools_for_step(_lazy_tools(), task)
    names = [t["name"] for t in view]
    assert names[:2] == ["tool_search", "tool_load"], "元工具恒在且带完整定义"
    by = {t["name"]: t for t in view}
    assert by["a"].get("params") == {"x": {}}, "已加载 → 完整定义"
    assert by["b"].get("params") == {} and by["b"].get("required") == [], \
        "未加载 → 目录卡（无 schema）"
    assert _slim_description(by["b"].get("description"))


def test_lazy_config_wiring(tmp_path):
    """L1：react.lazy_tools 配置装配生效（默认 off）。"""
    c1 = _build(tmp_path)
    c2 = _build(tmp_path, patch={"agent": {"react": {"lazy_tools": True}}})

    async def s():
        await c1.assemble()
        assert c1.root.react.config.lazy_tools is False
        await c1.teardown()
        await c2.assemble()
        assert c2.root.react.config.lazy_tools is True
        assert c2.root.react.config.lazy_tools_max == 12
        await c2.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 设置页：三处同步登记
# ---------------------------------------------------------------------------

def test_new_toggles_registered_in_settings():
    """cost_calibration / lazy_tools 三处登记；默认与 profiles 同源；bool 钳制。"""
    k1, k2 = "cost_calibration", "lazy_tools"
    for k, pid, default in ((k1, "plugins.budget", True), (k2, "plugins.react", False)):
        assert k in srv._SETTING_SCHEMA
        assert srv._PLUGIN_OF_KEY[k] == pid, f"{k} 归属错误"
        assert k in srv._SETTING_DEFAULTS
        assert srv._SETTING_DEFAULTS[k] == default, f"{k} 默认值与决议漂移"
        assert f"set_{k}" in srv._PAGE, f"{k} 未同步到前端"
    assert srv._coerce_setting(k1, "on") is True
    assert srv._coerce_setting(k1, "off") is False
    assert srv._coerce_setting(k2, True) is True


def test_card_is_projection_only():
    """L1：目录卡是纯投影——原目录 dict 不被改动。"""
    t = {"name": "x", "description": "第一行\n第二行", "params": {"p": {"type": "str"}},
         "required": ["p"], "risk": "read", "owner": "o"}
    card = _card(t)
    assert card["params"] == {} and card["required"] == []
    assert t["params"] == {"p": {"type": "str"}} and t["required"] == ["p"], \
        "原目录不得被投影改动"
