"""G2.0（P3：视图与不变量）测试。

覆盖《上下文管理器架构评估与重构方案》§6 P3 的三项 + 四条不变量断言：

- **P3-1 Inspector ``/context`` 视图**：分类视图渲染（层汇总 / 合计 / I-4 行）；
- **P3-2 四条不变量断言**：I-1（配对）/ I-2（单元原子性）在装配对象面（治理器谓词）；
  I-3（前缀稳定性可测）/ I-4（预算守恒）在记账面（纯函数）；
- **P3-3 轮次全景账本 ``RoundLedger``**：合并四端点 + 逐端点对账 ``Σ块+附件+unaccounted=预算``。

另含端到端验收：真会话跑一轮后，经 ``/api/context`` 的**模块级逻辑**（``_context_view_payload``）
自证守恒（任取一轮可自证——P3 验收指标）。全部离线确定性（fixture provider），不触网。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.plugins.context import ContextGovernor
from agent.plugins.context.inspector import (INVARIANTS, LedgerEntry, RoundLedger,
                                             entry_from_payload, ledger_from_payloads)
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


def _gov() -> ContextGovernor:
    return ContextGovernor(max_context_tokens=100000, reserve_output_tokens=4000)


def _payload_from_report(rep, *, stage: str = "plan") -> dict:
    """把 ``AssemblyReport`` 投影为 governed 载荷（与 chat/react 落盘形态一致）。"""
    return {
        "stage": stage, "at": "t",
        "input_budget": int(rep.input_budget), "budget_raw": int(rep.budget_raw),
        "total_used": int(rep.total_used), "unaccounted_tokens": int(rep.unaccounted_tokens),
        "stable_prefix_tokens": int(rep.stable_prefix_tokens),
        "volatile_blocks": list(rep.volatile_blocks), "missing_blocks": list(rep.missing_blocks),
        "blocks": [{"key": b.key, "layer": b.layer, "present": b.present, "reason": b.reason,
                    "requested": b.requested, "allocated": b.allocated, "used": b.used,
                    "cap": b.cap} for b in rep.blocks],
        "attachments": [{"key": a.key, "claimed": a.claimed, "used": a.used}
                        for a in rep.attachments],
    }


def _unit(n: int) -> list[dict]:
    """一个 wire 原子单元：``assistant{reasoning_content}+tool_calls`` + ``tool results``。"""
    return [
        {"role": "assistant", "content": "think",
         "tool_calls": [{"id": f"c{n}", "name": "t", "arguments": {"p": "x"}}],
         "reasoning_content": "R"},
        {"role": "user", "content": {"tool_results": [{"name": "t", "content": "res"}]}},
    ]


# ---------------------------------------------------------------------------
# P3-1 Inspector /context 视图
# ---------------------------------------------------------------------------

def test_view_lists_layers_totals_and_i4():
    """分类视图按层汇总 + 合计行 + I-4 对账行（数据驱动，不臆造字段）。"""
    g = _gov()
    blocks = [
        {"key": "sys", "kind": "text", "present": True, "payload": "s" * 400,
         "rigid": True, "priority": 0, "layer": "resident"},
        {"key": "tools", "kind": "text", "present": True, "payload": "t" * 400,
         "priority": 10, "layer": "on_demand"},
    ]
    asm = g.assemble(blocks, phase="plan")
    text = g.render_entry(_payload_from_report(asm.report))
    assert "resident" in text and "on_demand" in text        # 层名（契约词汇）
    assert "常驻" in text and "按需" in text                  # 层显示名
    assert "合计" in text and "原始预算" in text
    assert "I-4 预算守恒：✓" in text


def test_view_render_is_pure_no_state():
    """渲染是纯只读旁路：同一载荷连续渲染两次逐字节相同。"""
    g = _gov()
    p = _payload_from_report(g.assemble([{"key": "a", "kind": "text", "present": True,
                                          "payload": "x" * 100, "layer": "resident"}]).report)
    assert g.render_entry(p) == g.render_entry(p)


# ---------------------------------------------------------------------------
# P3-2 四条不变量
# ---------------------------------------------------------------------------

def test_invariant_ids_complete():
    assert INVARIANTS == ("I-1", "I-2", "I-3", "I-4")


def test_i1_pairing_predicate_detects_orphan_result():
    """I-1：孤儿 ``tool_results``（无前置 ``assistant.tool_calls``）→ 违规。"""
    g = _gov()
    orphan = [{"role": "user", "content": {"tool_results": [{"name": "t", "content": "r"}]}}]
    assert g._pair_ok(orphan) is False
    assert g._pair_ok(_unit(1)) is True


def test_i2_unit_atomic_predicate_detects_half_edit():
    """I-2：单元被半改（只留上半部）→ 违规；整单元保留 → 合规。"""
    g = _gov()
    before = _unit(1)
    assert g._unit_atomic_ok(before, [before[0]]) is False      # 只留 assistant（丢回灌）
    assert g._unit_atomic_ok(before, [before[1]]) is False      # 只留回灌（丢上半部）
    assert g._unit_atomic_ok(before, list(before)) is True      # 整单元保留（原对象）


def test_i3_flags_volatile_at_head():
    """I-3：volatile 块位于装配序最前（稳定前缀为 0）→ 违规；中段 volatile → 合规。"""
    head = entry_from_payload({"stage": "x", "budget_raw": 100, "total_used": 50,
                               "stable_prefix_tokens": 0, "volatile_blocks": ["k"],
                               "blocks": [{"key": "k", "used": 50}]})
    assert head.prefix_measurable() is False
    assert "I-3" in head.violations()
    mid = entry_from_payload({"stage": "x", "budget_raw": 100, "total_used": 50,
                              "stable_prefix_tokens": 20, "volatile_blocks": ["k"],
                              "blocks": [{"key": "a", "used": 20}, {"key": "k", "used": 30}]})
    assert mid.prefix_measurable() is True
    assert mid.prefix_affected() == 30                          # 体检量：volatile 之后受影响


def test_i4_budget_conservation_from_real_assembly():
    """I-4：真装配的 ``Σ块used + Σ附件used + unaccounted = budget_raw``（残差 0）。"""
    g = _gov()
    asm = g.assemble(
        [{"key": "a", "kind": "text", "present": True, "payload": "x" * 400, "layer": "resident"}],
        attachments=[{"key": "native_tools", "claimed_tokens": 1234}], phase="plan")
    rep = asm.report
    e = entry_from_payload(_payload_from_report(rep))
    assert e.accounted() + e.unaccounted_tokens == rep.budget_raw
    assert e.budget_residual() == 0 and e.conserved() is True
    assert e.invariants() == {"I-3": True, "I-4": True}


def test_i4_detects_over_account():
    """I-4：重复计账（accounted > 预算）→ 残差为正、判违规。"""
    e = entry_from_payload({"stage": "plan", "budget_raw": 1000, "unaccounted_tokens": 0,
                            "blocks": [{"key": "a", "used": 600}, {"key": "b", "used": 600}]})
    assert e.budget_residual() == 200
    assert e.conserved() is False
    assert e.violations() == ["I-4"]


# ---------------------------------------------------------------------------
# P3-3 轮次全景账本 RoundLedger
# ---------------------------------------------------------------------------

def test_ledger_merges_stages_and_reconciles():
    """四端点合并：stage 保序、逐端点对账、全局合计。"""
    g = _gov()
    payloads = []
    for st in ("prior", "plan", "react", "narrate"):
        asm = g.assemble([{"key": f"{st}_k", "kind": "text", "present": True,
                           "payload": "x" * 400, "layer": "resident"}], phase=st)
        payloads.append(_payload_from_report(asm.report, stage=st))
    led = g.ledger(payloads)
    assert led.stages() == ["prior", "plan", "react", "narrate"]
    assert all(led.reconcile().values()), led.reconcile()
    assert led.violations() == {}
    tot = led.totals()
    assert tot["entries"] == 4 and tot["block_used"] > 0
    assert tot["block_used"] + tot["attach_used"] + tot["unaccounted"] == tot["budget_raw"]


def test_ledger_entry_takes_latest_per_stage():
    """同 stage 多次装配 → ``entry()`` 取最新一次。"""
    a = entry_from_payload({"stage": "react", "budget_raw": 10, "total_used": 1})
    b = entry_from_payload({"stage": "react", "budget_raw": 20, "total_used": 2})
    led = RoundLedger(entries=(a, b))
    assert led.entry("react").budget_raw == 20
    assert led.entry("missing") is None


def test_entry_backward_compat_without_budget_raw():
    """旧载荷无 ``budget_raw`` → 退化为生效预算（不误判、不崩）。"""
    e = entry_from_payload({"stage": "plan", "input_budget": 960, "total_used": 10,
                            "blocks": [{"key": "a", "used": 10}]})
    assert e.budget_raw == 960


def test_ledger_render_contains_stages_and_reconcile():
    g = _gov()
    payloads = [_payload_from_report(
        g.assemble([{"key": "k", "kind": "text", "present": True, "payload": "x" * 100}],
                   phase=st).report, stage=st) for st in ("plan", "narrate")]
    text = g.render_ledger(payloads)
    assert "stage=plan" in text and "stage=narrate" in text
    assert "对账（I-4 预算守恒）：✓" in text


def test_governor_facade_delegates_and_degrades():
    """门面：``ledger`` / ``render_ledger`` / ``entry`` / ``render_entry`` / ``check_invariants``。"""
    g = _gov()
    p = _payload_from_report(g.assemble([{"key": "a", "kind": "text", "present": True,
                                          "payload": "x" * 50, "layer": "resident"}]).report)
    assert isinstance(g.ledger([p]), RoundLedger)
    assert isinstance(g.entry(p), LedgerEntry)
    assert g.check_invariants(p) == {"I-3": True, "I-4": True}
    assert g.ledger(None).entries == ()           # 空入参 → 空账本（不崩）
    assert g.render_ledger(None)                  # 空账本也可渲染


# ---------------------------------------------------------------------------
# 端到端：/api/context 的模块级逻辑（P3 验收：任取一轮可自证守恒）
# ---------------------------------------------------------------------------

def test_context_view_from_real_events(tmp_path):
    """真会话跑一轮 → ``_context_view_payload`` 自证守恒、载荷携带 P3 字段。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        await c.root.react.run(c.root, "两步链路：先回显再总结")
        out = await srv._context_view_payload(c.root)
        assert out["available"] is True, out
        assert out["stages"], "应有 stage 记账"
        assert all(out["reconcile"].values()), out["reconcile"]
        assert out["violations"] == {}, out["violations"]
        assert "I-4" in out["text"]
        rows = await c.root.db.query(
            c.root, "SELECT payload FROM agent_events"
                    " WHERE event='agent/context/governed' LIMIT 1")
        p = json.loads(rows[0]["payload"])
        assert "budget_raw" in p, "载荷须携带 P3 对照基数"
        assert any("layer" in b for b in p["blocks"]), "载荷块须携带 layer（视图分类用）"
        await c.teardown()

    _run(s())


def test_context_view_without_governor(tmp_path):
    """无治理器 → ``available=False``（软依赖降级，绝不崩）。"""
    c = _build(tmp_path, context=False)

    async def s():
        await c.assemble()
        out = await srv._context_view_payload(c.root)
        assert out["available"] is False and out["reason"] == "no_governor"
        await c.teardown()

    _run(s())


def test_ledger_from_payloads_preserves_order():
    """``ledger_from_payloads`` 纯函数：保序重建。"""
    led = ledger_from_payloads([{"stage": "a"}, {"stage": "b"}])
    assert led.stages() == ["a", "b"]
    assert isinstance(led.entries[0].attachments, tuple)
    assert led.entries[0].blocks == ()
