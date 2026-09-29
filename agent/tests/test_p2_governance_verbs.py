"""G2.0（P2：治理动词——决策、执行与不变量）测试。

覆盖《上下文管理器架构评估与重构方案》§6 P2 的七个测试点 + 登记面：

- ``test_plan_keeps_below_waterline`` / ``test_plan_off_mode_keeps``：决策表第 1 行；
- ``test_plan_prefers_clear_over_compact``：**clear 优先于 compact**（零推理成本）;
- ``test_compact_skipped_when_payback_fails``：未过回本闸 → 不产出 compact；
- ``test_unit_replacement_only_at_archive_point``：**时机闸**——run 中途请求替换 → 拒绝；
- ``test_shadow_mode_computes_but_does_not_apply``：shadow 照常计算但不执行；
- ``test_unit_replacement_atomic_no_partial_edit``：切点落在单元内部 → I-2 违规并回滚；
- ``test_invariant_pairing_rolls_back``：产生孤儿回灌 → I-1 违规并回滚；
- ``test_on_compact_exception_degrades``：回调抛异常 → 降级跳过，其余继续；
- 另补：pinned 单元不被丢弃、显式 ``target_tokens``、配置三处登记、事件两处登记、
  以及 react **收尾存档点**端到端发出 ``compact-decided``（默认 shadow）。

全部离线确定性（fixture provider），不触网。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.api import Events, Services
from agent.core.events_catalog import EVENT_MODE, all_events
from agent.plugins.context import ContextGovernor
from agent.plugins.context.protocols import (ACTION_KINDS, COMPACT_MODES, Action,
                                             ApplyResult, BlockSpec)
from agent.profiles import build_agent
from agent.run import chat as srv
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _build(tmp_path, *, context: bool = True, patch: dict | None = None):
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    pm.set_enabled("plugins.react", True)
    pm.set_enabled("plugins.context", context)
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
                            tool_calls=[ToolCall(self._tool, {"text": f"t{self._n}"},
                                                 f"c{self._n}")],
                            tokens_in=1, tokens_out=1)


# ---------------------------------------------------------------------------
# 夹具：一个 4 单元的 wire 链（两个大单元在中间，便于稳定命中压缩 target）
# ---------------------------------------------------------------------------

def _unit(n: int, big: bool = False) -> list[dict]:
    payload = "x" * (4000 if big else 40)
    return [
        {"role": "assistant", "content": "think",
         "tool_calls": [{"id": f"c{n}", "name": "t", "arguments": {"p": payload}}],
         "reasoning_content": "R" * (2000 if big else 20)},
        {"role": "user",
         "content": {"tool_results": [{"name": "t", "content": "res" + payload}]}},
    ]


def _chain() -> list[dict]:
    return _unit(1) + _unit(2, True) + _unit(3, True) + _unit(4)


def _chain_block(cb, *, payload: list[dict] | None = None) -> BlockSpec:
    return BlockSpec(key="react_chain", kind="messages", present=True,
                     payload=list(payload if payload is not None else _chain()),
                     on_compact=cb, compact_floor_ratio=0.4)


def _gov(*, mode: str = "on", **kw) -> ContextGovernor:
    return ContextGovernor(compact_mode=mode, **kw)


def _high(g: ContextGovernor, **kw):
    """high 水位（200k / 96k ≈ 2.08 ≥ 0.70）。"""
    return g.assess(200000, **kw)


_COMPACT = Action(kind="compact", target="react_chain")
_SUMMARY = lambda dropped, target: f"[过程摘要] 压缩 {len(dropped)} 条"  # noqa: E731


# ---------------------------------------------------------------------------
# 决策表（§4.2.4）
# ---------------------------------------------------------------------------

def test_plan_keeps_below_waterline():
    """水位 normal → keep（不做任何动作）。"""
    g = _gov()
    assert [a.kind for a in g.plan(g.assess(1000), blocks=[_chain_block(_SUMMARY)])] == ["keep"]


def test_plan_off_mode_keeps():
    """``mode="off"`` → keep（即便水位 high、有 compact 候选）。"""
    g = _gov()
    rep = _high(g, phase="run_teardown", remaining_steps=99)
    assert [a.kind for a in g.plan(rep, blocks=[_chain_block(_SUMMARY)], mode="off")] == ["keep"]


def test_plan_prefers_clear_over_compact():
    """**clear 优先于 compact**：有可清除候选（释放量达标）时选 clear（零推理成本）。"""
    g = _gov(clear_at_least_tokens=100)
    regen = BlockSpec(key="tools", kind="text", present=True, payload="x" * 4000,
                      layer="on_demand", regenerable=True)
    rep = _high(g, phase="run_teardown", remaining_steps=99)
    acts = g.plan(rep, blocks=[regen, _chain_block(_SUMMARY)])
    assert [a.kind for a in acts] == ["clear"]
    assert acts[0].target == "tools" and acts[0].expected_release > 0


def test_compact_skipped_when_payback_fails():
    """未过回本闸 → 不产出 compact（回落 clip，保底不做结构性改动）。"""
    g = _gov()
    blk = _chain_block(_SUMMARY)
    # ① remaining_steps < n* = ρ·r/(1−r) = 29×0.4/0.6 ≈ 19.33
    rep = _high(g, phase="run_teardown", remaining_steps=1)
    assert [a.kind for a in g.plan(rep, blocks=[blk])] == ["clip"]
    # ② 缺省不传 remaining_steps → 保守不压
    assert [a.kind for a in g.plan(_high(g, phase="run_teardown"), blocks=[blk])] == ["clip"]
    # ③ 无 on_compact 回调 → 无从摘要
    bare = BlockSpec(key="react_chain", kind="messages", present=True, payload=_chain())
    assert [a.kind for a in g.plan(rep, blocks=[bare])] == ["clip"]


def test_unit_replacement_only_at_archive_point():
    """**时机闸**：整单元替换只允许发生在 run 收尾存档点；run 中途请求 → 拒绝。"""
    g = _gov()
    blk = _chain_block(_SUMMARY)
    mid = _high(g, phase="react", remaining_steps=99)          # 循环中（非收尾）
    assert [a.kind for a in g.plan(mid, blocks=[blk])] == ["clip"], "run 中途不得替换"
    teardown = _high(g, phase="run_teardown", remaining_steps=99)
    assert [a.kind for a in g.plan(teardown, blocks=[blk])] == ["compact"]


# ---------------------------------------------------------------------------
# 执行（§4.2.5）：shadow 不执行 / on 落位 / 失败即降级
# ---------------------------------------------------------------------------

def test_shadow_mode_computes_but_does_not_apply():
    """shadow：``plan`` 照常计算，``execute`` **不执行**（只回报 shadow 跳过）。"""
    g = _gov(mode="shadow")
    blk = _chain_block(_SUMMARY)
    rep = _high(g, phase="run_teardown", remaining_steps=99)
    acts = g.plan(rep, blocks=[blk])
    assert [a.kind for a in acts] == ["compact"], "shadow 仍须计算决策"
    out, res = g.execute(acts, [blk], mode="shadow")
    assert res.applied == [] and res.released == 0
    assert res.degraded is False
    assert res.skipped and res.skipped[0][1] == "shadow"
    assert out[0].payload == blk.payload, "shadow 下块逐字节不变"


def test_execute_applies_compact_and_reduces():
    """on：整单元替换落位——链变短、保留段仍是原对象（未被改写）。"""
    g = _gov(mode="on")
    blk = _chain_block(_SUMMARY)
    out, res = g.execute([_COMPACT], [blk], mode="on")
    assert res.applied == ["compact:react_chain"]
    assert res.released > 0 and res.degraded is False
    assert res.invariants == {"I-1": True, "I-2": True}
    assert len(out[0].payload) < len(blk.payload)
    assert out[0].payload[-1] is blk.payload[-1], "保留段必须是原对象（零改写）"


def test_unit_replacement_atomic_no_partial_edit():
    """切点落在**单元内部**（只回显 assistant 半部）→ 判 I-2 违规并回滚。"""
    g = _gov(mode="on")

    def half(dropped, target):
        return [dropped[0]]                      # 只留 tool_calls，丢掉配对回灌

    blk = _chain_block(half)
    out, res = g.execute([_COMPACT], [blk], mode="on")
    assert res.applied == [] and res.degraded is True
    assert res.skipped[0][1] == "I-2_violation"
    assert out[0].payload == blk.payload


def test_invariant_pairing_rolls_back():
    """产生孤儿回灌（无前置 tool_calls）→ 判 I-1 违规并回滚。"""
    g = _gov(mode="on")

    def orphan(dropped, target):
        return [{"role": "user",
                 "content": {"tool_results": [{"name": "t", "content": "x"}]}}]

    blk = _chain_block(orphan)
    out, res = g.execute([_COMPACT], [blk], mode="on")
    assert res.applied == [] and res.degraded is True
    assert res.skipped[0][1] == "I-1_violation"
    assert out[0].payload == blk.payload


def test_on_compact_exception_degrades():
    """回调抛异常 → 该动作跳过、记 degraded；其余动作继续（这里只有一个）。"""
    g = _gov(mode="on")

    def boom(dropped, target):
        raise RuntimeError("nope")

    blk = _chain_block(boom)
    out, res = g.execute([_COMPACT], [blk], mode="on")
    assert res.applied == [] and res.degraded is True
    assert res.skipped[0][1].startswith("exception:")
    assert out[0].payload == blk.payload, "异常下块逐字节不变"


def test_execute_clear_requires_regenerable():
    """clear 只作用于「可重建」块；不可重建 → 拒绝（失败即降级为 keep）。"""
    g = _gov(mode="on")
    blk = BlockSpec(key="tools", kind="text", present=True, payload="y" * 4000,
                    layer="on_demand")                     # regenerable 缺省 False
    out, res = g.execute([Action("clear", "tools")], [blk], mode="on")
    assert res.applied == [] and res.degraded is True
    assert res.skipped[0][1] == "not_regenerable"
    assert out[0].payload == blk.payload


def test_execute_clear_releases_regenerable_text():
    """clear 命中可重建块 → 释放其全部体量。"""
    g = _gov(mode="on")
    blk = BlockSpec(key="tools", kind="text", present=True, payload="y" * 4000,
                    layer="on_demand", regenerable=True)
    out, res = g.execute([Action("clear", "tools")], [blk], mode="on")
    assert res.applied == ["clear:tools"] and res.released > 0
    assert out[0].payload == ""


def test_execute_off_mode_skips_everything():
    g = _gov(mode="on")
    blk = _chain_block(_SUMMARY)
    out, res = g.execute([_COMPACT], [blk], mode="off")
    assert res.applied == [] and out[0].payload == blk.payload
    assert res.skipped[0][1] == "mode_off"


def test_keep_and_clip_are_not_structural():
    """keep / clip 不进结构性改动（记入 skipped，不算 applied）。"""
    g = _gov(mode="on")
    blk = _chain_block(_SUMMARY)
    out, res = g.execute([Action("keep"), Action("clip")], [blk], mode="on")
    assert res.applied == [] and res.degraded is False
    assert [x[1] for x in res.skipped] == ["keep", "clip"]


# ---------------------------------------------------------------------------
# 压缩粒度：整单元 / pinned 保护 / 显式目标
# ---------------------------------------------------------------------------

def test_pinned_unit_survives_compaction():
    """``pinned`` 单元字节冻结、永不参与丢弃（块内先占语义的延续）。"""
    g = _gov(mode="on")
    chain = _chain()
    chain[0]["pinned"] = True
    blk = _chain_block(_SUMMARY, payload=chain)
    out, res = g.execute([_COMPACT], [blk], mode="on")
    assert res.applied == ["compact:react_chain"]
    after = out[0].payload
    assert chain[0] in after and chain[1] in after, "pinned 单元整体保留"
    assert all(x is not chain[2] for x in after), "非 pinned 的旧大单元被整单元替换"


def test_compact_target_tokens_from_action_payload():
    """``Action.payload={"target_tokens": …}`` 覆盖残留比折算的目标量。"""
    g = _gov(mode="on")
    blk = _chain_block(_SUMMARY)
    out, res = g.execute(
        [Action("compact", "react_chain", payload={"target_tokens": 200})], [blk], mode="on")
    assert res.applied == ["compact:react_chain"]
    assert len(out[0].payload) <= 3, "目标 200 tok → 只保留最新单元 + 摘要"


def test_single_unit_chain_is_never_compacted():
    """只有一个原子单元 → 无可替换（保最新）；不产出结构性改动。"""
    g = _gov(mode="on")
    blk = _chain_block(_SUMMARY, payload=_unit(9, True))
    out, res = g.execute([_COMPACT], [blk], mode="on")
    assert res.applied == [] and res.skipped[0][1] == "single_unit"


# ---------------------------------------------------------------------------
# 配置面（三处登记 / 枚举校验）
# ---------------------------------------------------------------------------

def test_p2_settings_registered_three_places():
    """``compact_mode`` / ``clear_at_least_tokens`` 三处登记 + 枚举钳制。"""
    for key, default in (("compact_mode", "shadow"), ("clear_at_least_tokens", 4000)):
        assert key in srv._SETTING_SCHEMA, f"{key} 缺 schema"
        assert srv._PLUGIN_OF_KEY[key] == "plugins.context", f"{key} 归属错误"
        assert srv._SETTING_DEFAULTS[key] == default, f"{key} 默认值与决议漂移"
        assert key in srv._PAGE, f"{key} 未同步到前端"
    assert srv._coerce_setting("compact_mode", "on") == "on"
    assert srv._coerce_setting("compact_mode", "shadow") == "shadow"
    with pytest.raises(ValueError):
        srv._coerce_setting("compact_mode", "bogus")
    assert srv._coerce_setting("clear_at_least_tokens", 8000) == 8000


def test_governor_rejects_bad_mode():
    for bad in ("bogus", "", "ON"):
        with pytest.raises(ValueError):
            ContextGovernor(compact_mode=bad)
    g = _gov()
    with pytest.raises(ValueError):
        g.execute([], [], mode="bogus")


def test_governor_from_config_reads_p2_keys(tmp_path):
    g = ContextGovernor.from_config({"compact_mode": "on", "clear_at_least_tokens": 777})
    assert g.compact_mode() == "on" and g.clear_at_least_tokens() == 777


# ---------------------------------------------------------------------------
# 事件面（enum + EVENT_MODE 两处登记）
# ---------------------------------------------------------------------------

def test_p2_events_registered_two_places():
    for name in ("CONTEXT_COMPACT_DECIDED", "CONTEXT_COMPACT_APPLIED",
                 "CONTEXT_INVARIANT_VIOLATION"):
        val = getattr(Events, name)
        assert val in all_events(), f"{name} 未进 enum"
        assert EVENT_MODE.get(val) == "emit", f"{name} 未登记 EVENT_MODE"


# ---------------------------------------------------------------------------
# 端到端：react **收尾存档点**发出 compact-decided（默认 shadow）
# ---------------------------------------------------------------------------

def test_react_emits_compact_decided_at_teardown(tmp_path):
    c = _build(tmp_path, patch={"agent": {"react": {"max_steps": 2}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        loop = c.root.react
        loop._reasoner = _AlwaysToolReasoner()
        r = await loop.run(c.root, "两步链路", session_id="p2")
        assert r.steps == 2 and r.exhausted

        rows = await c.root.db.query(
            c.root,
            "SELECT payload FROM agent_events WHERE event=? ORDER BY seq DESC",
            (Events.CONTEXT_COMPACT_DECIDED,))
        assert rows, "收尾存档点未发 agent/context/compact-decided"
        p = json.loads(rows[0]["payload"])
        assert p["stage"] == "react" and p["mode"] in COMPACT_MODES
        assert p["shadow"] is True, "默认 compact_mode=shadow（只算不做）"
        assert p["actions"] and p["actions"][0]["kind"] in ACTION_KINDS
        assert "payback" in p["gates"] and "timing" in p["gates"]
        # shadow 下不执行 → 不产生 applied 记录
        rows2 = await c.root.db.query(
            c.root,
            "SELECT payload FROM agent_events WHERE event=?",
            (Events.CONTEXT_COMPACT_APPLIED,))
        assert rows2 == [], "shadow 下不得有执行记录"
        await c.teardown()

    _run(s())


def test_react_teardown_governance_degrades_without_governor(tmp_path):
    """无治理器（插件停用）→ 收尾治理静默跳过（软依赖，不劣化不裸崩）。"""
    c = _build(tmp_path, context=False, patch={"agent": {"react": {"max_steps": 1}}})

    async def s():
        await c.assemble()
        assert not c.root.has(Services.CONTEXT)
        _register_echo(c.root)
        loop = c.root.react
        loop._reasoner = _AlwaysToolReasoner()
        r = await loop.run(c.root, "一步", session_id="p2b")
        assert r.exhausted
        rows = await c.root.db.query(
            c.root, "SELECT payload FROM agent_events WHERE event=?",
            (Events.CONTEXT_COMPACT_DECIDED,))
        assert rows == [], "无治理器 → 不得发治理遥测"
        await c.teardown()

    _run(s())
