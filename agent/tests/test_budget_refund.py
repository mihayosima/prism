"""S1 预算退款与估算口径测试——失败全额退款、成功对账、CJK 不再低估。"""

from __future__ import annotations

import asyncio

import pytest

from agent.api import CapabilityNotImplemented, estimate_tokens
from agent.profiles import build_agent


def _run(coro):
    return asyncio.run(coro)


def test_failed_call_refunds_pre_debit_and_audits(tmp_path):
    """provider 抛错 → 预借全额退还 + durable 失败审计落盘。"""
    c = build_agent(config={
        "db_path": str(tmp_path / "agent.db"),
        "agent": {"llm": {"provider": "stub"},
                  "budget": {"max_tokens_per_run": 100_000}},
    })

    async def s():
        await c.assemble()
        spent0 = c.root.state.get("budget_spent", 0)
        with pytest.raises(CapabilityNotImplemented):
            await c.root.llm.complete(
                c.root, [{"role": "user", "content": "你好" * 50}], caller="t")
        assert c.root.state.get("budget_spent") == spent0, "失败调用应全额退还预借"
        assert not c.root.state.get("budget_pending_est"), "pending est 分槽应清空"
        rows = await c.root.db.query(
            c.root, "SELECT event FROM agent_events WHERE event='agent/llm/call-failed'")
        assert rows, "失败应落 durable 审计"
        await c.teardown()

    _run(s())


def test_successful_call_reconciles_to_actual(tmp_path):
    """成功调用：预借含请求侧开销（tools/schema），对账回冲到实际入账值。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")})

    async def s():
        await c.assemble()
        messages = [{"role": "user", "content": "hi"}]
        await c.root.llm.complete(c.root, messages, caller="t")
        for _ in range(5):                     # _post 为 async 监听器（fire-and-forget），让其对账任务落地
            await asyncio.sleep(0)
        spent = c.root.state.get("budget_spent", 0)
        # 实际入账 = tokens_in(messages) + tokens_out(响应)；预借多出的部分已回冲
        actual = estimate_tokens(messages) + estimate_tokens({"text": "（fixture 默认响应）"})
        assert spent == actual, f"对账后 spent 应等于实际入账 {actual}，得到 {spent}"
        assert not c.root.state.get("budget_pending_est")
        rows = await c.root.db.query(
            c.root, "SELECT est_cost FROM agent_budget_log")
        assert rows and rows[-1]["est_cost"] is not None, "est_cost 应落值"
        await c.teardown()

    _run(s())


def test_cjk_estimate_no_longer_undercounts():
    """旧口径 len//4 对中文低估 2–3 倍；新口径 CJK 按 0.75 token/字。"""
    payload = {"t": "你好" * 100}          # 200 个 CJK 字符
    est = estimate_tokens(payload)
    import json
    legacy = len(json.dumps(payload, ensure_ascii=False)) // 4
    assert est >= 120, f"CJK 估算应 ≥ 0.6 token/字，得到 {est}"
    assert est > legacy * 2, f"CJK 估算应显著高于旧口径 {legacy}，得到 {est}"


def test_budget_denial_still_works_with_new_est(tmp_path):
    """口径变化后累计超限拒绝仍生效（回归既有预算行为）。"""
    c = build_agent(config={
        "db_path": str(tmp_path / "agent.db"),
        "agent": {"budget": {"max_tokens_per_run": 50}},
    })

    async def s():
        await c.assemble()
        denied = False
        for _ in range(30):
            try:
                await c.root.llm.complete(
                    c.root, [{"role": "user", "content": "x" * 40}], schema={}, caller="t")
            except CapabilityNotImplemented:
                denied = True
                break
        assert denied, "累计超限应触发预算拒绝"
        await c.teardown()

    _run(s())
