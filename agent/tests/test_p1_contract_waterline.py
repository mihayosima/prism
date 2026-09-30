"""G2.0（P1：契约扩展——字段与水位）测试。

覆盖（对应《上下文管理器架构评估与重构方案》§6 P1 测试点）：
- ``test_layer_defaults_from_rigid``：层缺省推导 ``"" → resident if rigid else runtime``；
- ``test_cap_enforced_via_overflow_channel``：resident / on_demand / input 三处 cap 生效，
  超限走**既有刚性超限双通道**（``rigid_overflow`` + ``system_text`` 显式标注），绝不静默截断；
- ``test_assess_pure_function_no_state``：``assess`` 是**纯函数**（连续调用结果只依赖入参）；
- ``test_scope_opaque_passthrough``：``scope`` 是**不透明归集键**（不改控制流、不改裁决）；
- 契约字段 / 配置三处登记 / 事件两处登记 / run 收尾水位遥测。

全部离线确定性（fixture provider），不触网。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.api import Events, Services
from agent.core.events_catalog import EVENT_MODE, all_events
from agent.plugins.context import ContextGovernor
from agent.plugins.context.protocols import BlockSpec, WaterlineReport, resolve_layer
from agent.profiles import build_agent
from agent.run import chat as srv
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _build(tmp_path, *, patch: dict | None = None):
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    pm.set_enabled("plugins.react", True)
    pm.set_enabled("plugins.context", True)
    pm.set_enabled("plugins.work_memory", True)
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch, plugin_mgr=pm)


def _register_echo(ctx) -> None:
    async def _echo(text: str = "hi"):
        return {"echo": text}

    ctx.tasks.register(ctx, "test.echo",
                       schema={"text": {"type": "str", "required": False, "default": "hi"}},
                       handler=_echo, owner="test", description="回显参数")


class _AlwaysToolReasoner:
    """恒定请求一次工具调用（强制多步），使 run 必然走到收尾路径。"""

    def __init__(self, tool: str = "test.echo"):
        self._tool = tool
        self._n = 0

    async def reason(self, ctx, messages, tools):
        from agent.plugins.react.components import ReasonOutput, ToolCall

        self._n += 1
        return ReasonOutput(text=f"第{self._n}步", reasoning_content=f"思考{self._n}",
                            tool_calls=[ToolCall(self._tool, {}, f"c{self._n}")],
                            tokens_in=1, tokens_out=1)


# ---------------------------------------------------------------------------
# P1-1：契约字段与层推导
# ---------------------------------------------------------------------------

def test_layer_defaults_from_rigid():
    """``layer == ""`` 必须等价于 ``resident if rigid else runtime``（硬约束）。"""
    assert resolve_layer("", True) == "resident"
    assert resolve_layer("", False) == "runtime"
    assert resolve_layer("memory", True) == "memory"      # 显式值优先（不被 rigid 覆盖）
    assert resolve_layer("input", False) == "input"

    assert BlockSpec("a", "text", True, "x", rigid=True).effective_layer() == "resident"
    assert BlockSpec("b", "text", True, "x").effective_layer() == "runtime"
    assert BlockSpec("c", "text", True, "x", layer="on_demand").effective_layer() == "on_demand"


def test_block_spec_new_fields_and_dict_coerce():
    """P1 六新字段：dataclass 缺省合法；``_coerce`` 支持 dict 形式（消费层零 import）。"""
    b = BlockSpec("k", "text", True, "x")
    assert (b.layer, b.cap_tokens, b.compact_at) == ("", 0, 0)
    assert b.compact_floor_ratio == 0.4 and b.on_compact is None and b.regenerable is False

    cb = ContextGovernor._coerce({
        "key": "k", "kind": "text", "present": True, "payload": "x",
        "layer": "on_demand", "cap_tokens": 123, "compact_at": 500,
        "compact_floor_ratio": 0.25, "regenerable": True,
    })
    assert (cb.layer, cb.cap_tokens, cb.compact_at) == ("on_demand", 123, 500)
    assert cb.compact_floor_ratio == 0.25 and cb.regenerable is True


# ---------------------------------------------------------------------------
# P1-5：三处 cap 生效（走既有刚性超限双通道）
# ---------------------------------------------------------------------------

def test_cap_enforced_via_overflow_channel():
    """resident 超 cap → 刚性超限双通道；on_demand 超 cap → 分配受限于 cap（裁而不静默）。"""
    g = ContextGovernor(max_context_tokens=100_000, reserve_output_tokens=0,
                        resident_cap_tokens=100, on_demand_cap_tokens=100)
    big = "x" * 4000                                     # ≈ 1000 token（远超 cap 100）
    asm = g.assemble([
        BlockSpec("sys", "text", True, big, rigid=True, priority=0, layer="resident"),
        BlockSpec("tools", "text", True, big, priority=10, layer="on_demand"),
    ])
    rep = {r.key: r for r in asm.report.blocks}

    assert rep["sys"].layer == "resident" and rep["sys"].cap == 100
    assert rep["sys"].used <= 100, "常驻块必须被 cap 约束"
    assert rep["sys"].reason == "downgraded", "刚性超 cap 必须走刚性超限通道（可见）"
    assert asm.system_text.startswith("⚠️ 上下文预算超限"), \
        "刚性超限第二通道：system_text 必须显式标注（绝不静默截断）"

    assert rep["tools"].layer == "on_demand" and rep["tools"].cap == 100
    assert rep["tools"].used <= 100 and rep["tools"].trimmed is True


def test_cap_zero_means_no_cap():
    """``cap_tokens == 0`` 且层无缺省 cap（memory / runtime）→ 不受 cap 约束。"""
    g = ContextGovernor(max_context_tokens=100_000, reserve_output_tokens=0)
    big = "x" * 4000
    asm = g.assemble([BlockSpec("mem", "text", True, big, priority=0, layer="memory")])
    r = asm.report.blocks[0]
    assert r.cap == 0 and r.used == 1000 and r.trimmed is False


# ---------------------------------------------------------------------------
# P1-3：assess 纯函数 + WaterlineReport
# ---------------------------------------------------------------------------

def test_assess_pure_function_no_state():
    """连续调用结果只依赖入参（无状态证明）；且不污染后续调用。"""
    g = ContextGovernor(max_context_tokens=100000, reserve_output_tokens=0,
                        working_window_tokens=10000, window_high_ratio=0.70)
    r1 = g.assess(5000, phase="run_teardown", remaining_steps=30)
    r2 = g.assess(5000, phase="run_teardown", remaining_steps=30)
    assert r1 == r2 and isinstance(r1, WaterlineReport)
    assert r1.used == 5000 and r1.working_window == 10000
    assert abs(r1.load_ratio - 0.5) < 1e-9
    assert r1.level == "normal" and r1.suggested == "none"
    assert r1.capability_window == 100000, "能力上限与经济基数解耦（双窗口）"

    # 中间插一次极端入参，不得改变后续同样入参的结果
    g.assess(10 ** 6, phase="other")
    assert g.assess(5000, phase="run_teardown", remaining_steps=30) == r1


def test_assess_gates_threshold_timing_payback():
    """三重闸明细：阈值 / 时机（run 收尾）/ 回本（n* = ρ·r/(1−r)）。"""
    g = ContextGovernor(max_context_tokens=100000, reserve_output_tokens=0,
                        working_window_tokens=1000, window_high_ratio=0.70)
    hi = g.assess(800, phase="react")
    assert hi.level == "high" and hi.suggested == "clip"
    assert hi.gates["threshold"]["passed"] is True
    assert hi.gates["timing"]["at_run_teardown"] is False

    n_star = hi.gates["payback"]["required_steps"]
    assert abs(n_star - 29.0 * 0.4 / 0.6) < 1e-6, "n* 默认 r=0.4 ⇒ ≈19.33"
    assert g.assess(800, phase="run_teardown").gates["payback"]["passed"] is False, \
        "remaining_steps 缺省 ⇒ 保守不压（闸不过）"
    assert g.assess(800, phase="run_teardown",
                    remaining_steps=20).gates["payback"]["passed"] is True
    assert g.assess(800, phase="run_teardown",
                    remaining_steps=5).gates["payback"]["passed"] is False


# ---------------------------------------------------------------------------
# P1-6：scope 不透明透传（归集键）
# ---------------------------------------------------------------------------

def test_scope_opaque_passthrough():
    """scope 只作归集键：声明按 (scope, phase) 隔离；同一 blocks 在不同 scope 下裁决一致。"""
    g = ContextGovernor(max_context_tokens=10000, reserve_output_tokens=1000)
    g.declare([{"key": "a", "kind": "text", "required": True}], phase="p", scope="child")
    blocks = [BlockSpec("a", "text", True, "AAA", priority=0)]
    same = g.assemble(blocks, phase="p", scope="child")
    assert same.report.missing_blocks == []
    # 换成「只提交 b」→ 声明的 a 变成漏报红灯
    other = g.assemble([BlockSpec("b", "text", True, "BBB", priority=0)],
                       phase="p", scope="child")
    assert other.report.missing_blocks == ["a"]
    # scope 不同则不共享声明（零误报），且不改变装配字节
    unrelated = g.assemble(blocks, phase="p", scope="other")
    assert unrelated.report.missing_blocks == []
    assert unrelated.system_text == same.system_text

    # assess 也带 scope（留位），但不改变判定
    a1 = g.assess(1, scope="child")
    a2 = g.assess(1, scope="other")
    assert a1.level == a2.level and a1.load_ratio == a2.load_ratio


# ---------------------------------------------------------------------------
# 登记面：配置三处同步 + 事件两处
# ---------------------------------------------------------------------------

def test_window_settings_registered_three_places():
    """P1 新配置键在设置页三处同步登记，默认与 profiles 同源（缺一即失败）。"""
    from agent.tests._web_sources import frontend_settings_source

    src = frontend_settings_source()      # B4：`_PAGE` 已删除 → 读前端源码
    cases = (("working_window_tokens", 96000), ("window_high_ratio", 0.70),
             ("window_low_ratio", 0.40), ("resident_cap_tokens", 8000),
             ("on_demand_cap_tokens", 8000), ("prompt_cap_tokens", 2000))
    for k, default in cases:
        assert k in srv._SETTING_SCHEMA, f"{k} 缺 schema"
        assert srv._PLUGIN_OF_KEY[k] == "plugins.context", f"{k} 归属错误"
        assert k in srv._SETTING_DEFAULTS, f"{k} 缺默认值"
        assert srv._SETTING_DEFAULTS[k] == default, f"{k} 默认值与 profiles 漂移"
        # 数值卡按 fields 数组动态生成 id（'set_'+k）→ 前端同步的证据是键名字面量出现两次
        #（CONTEXT_FIELDS 字段表一次、SETTINGS_NUMERIC_KEYS 保存列表一次）。
        # B4：数据源由内联 `_PAGE` 换成 web/src/views/settings.logic.ts，语义不变。
        assert src.count(f"'{k}'") >= 2, f"{k} 未同步到前端卡片（字段表/保存列表）"
    # 比率项按 float 钳制（不是 int 截断）
    assert srv._coerce_setting("window_high_ratio", "0.7") == 0.7
    # 工作窗口须 >=1
    try:
        srv._coerce_setting("working_window_tokens", 0)
        raise AssertionError("工作窗口 0 应被拒绝")
    except ValueError:
        pass


def test_waterline_event_registered_both_places():
    """CONTEXT_WATERLINE 在 Events 枚举与 EVENT_MODE 两处同时登记（项目硬约束）。"""
    assert Events.CONTEXT_WATERLINE in all_events()
    assert EVENT_MODE.get(Events.CONTEXT_WATERLINE) == "emit"


# ---------------------------------------------------------------------------
# 端到端：run 收尾存档点发水位体检
# ---------------------------------------------------------------------------

def test_waterline_emitted_at_run_teardown(tmp_path):
    """react run 收尾必须发 ``agent/context/waterline``，phase=run_teardown、时机闸为真。"""
    c = _build(tmp_path, patch={"agent": {"react": {"max_steps": 2}}})
    got: list[dict] = []

    async def s():
        await c.assemble()
        _register_echo(c.root)
        c.root.get(Services.EVENTS).on(
            c.root, Events.CONTEXT_WATERLINE, lambda p: got.append(p))
        c.root.react._reasoner = _AlwaysToolReasoner()
        await c.root.react.run(c.root, "跑两步", session_id="w1")
        assert got, "run 收尾必须发水位体检事件"
        p = got[-1]
        assert p["stage"] == "react" and p["phase"] == "run_teardown"
        assert p["level"] in ("normal", "high") and p["suggested"] in ("none", "clip")
        assert p["gates"]["timing"]["at_run_teardown"] is True
        assert "window" in p and "capability" in p, "双窗口必须同时可观测"
        await c.teardown()

    _run(s())
