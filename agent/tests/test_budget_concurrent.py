"""D3 · budget 预借分槽并发测试——两 caller 交错调用时 est_cost 各自落值、总账精确。

旧实现 ``budget_pending_est`` 为单槽：react 与 memory 并发调 LLM 时互相覆盖，
先完成一方对账时读到对方的 est（已实测 ``est_cost=None`` 漏记）。分槽后按 caller 消账。
"""

from __future__ import annotations

import asyncio

from agent.profiles import build_agent


def _run(coro):
    return asyncio.run(coro)


def test_concurrent_callers_each_reconcile_own_est(tmp_path):
    """两个 caller 并发 complete → 各自 est_cost 落值；spent == Σ(ti+to)。"""
    c = build_agent(config={
        "db_path": str(tmp_path / "agent.db"),
        "agent": {"budget": {"max_tokens_per_run": 500_000}},
    })

    async def s():
        await c.assemble()
        short = [{"role": "user", "content": "hi"}]
        long = [{"role": "user", "content": "请详细解释一下量子纠缠的原理" * 20}]
        # 并发发起：两个 pre-call 都先落地（各自分槽），两个 post-call 再对账
        await asyncio.gather(
            c.root.llm.complete(c.root, short, caller="react"),
            c.root.llm.complete(c.root, long, caller="plugins.memory"),
        )
        for _ in range(10):                    # _post 为 async 监听器，让两个对账任务落地
            await asyncio.sleep(0)
        rows = await c.root.db.query(
            c.root, "SELECT tokens_in, tokens_out, est_cost FROM agent_budget_log")
        assert len(rows) == 2, f"应落两条记账，得到 {len(rows)}"
        assert all(r["est_cost"] is not None for r in rows), \
            f"分槽后两条 est_cost 都应落值（旧单槽会漏记）: {rows}"
        ests = sorted(r["est_cost"] for r in rows)
        assert ests[0] < ests[1], "两条预借 est 应随消息长度区分（证明未被覆盖）"
        total_actual = sum(r["tokens_in"] + r["tokens_out"] for r in rows)
        spent = c.root.state.get("budget_spent", 0)
        assert spent == total_actual, f"对账后总账应精确：spent={spent} vs Σ实际={total_actual}"
        assert not c.root.state.get("budget_pending_est"), "两个槽位都应消账"
        await c.teardown()

    _run(s())


def test_failed_call_refunds_only_own_slot(tmp_path):
    """caller A 失败只退 A 的槽；caller B 已成功的账面不受影响。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")})

    class FlakyProvider:
        """消息含 'FAIL' 即抛错，其余返回固定成功（装配后替换 provider）。"""

        label = "flaky"

        async def complete(self, ctx, messages, schema=None, tools=None):
            if any("FAIL" in str(m.get("content")) for m in messages):
                raise RuntimeError("模拟 provider 失败")
            return {"text": "ok"}

    async def s():
        await c.assemble()
        import pytest
        from agent.api import Services
        c.root.get(Services.LLM)._provider = FlakyProvider()
        # B 先成功，再 A 失败：验证失败只清 A 的槽
        await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}],
                                  caller="ok_caller")
        for _ in range(5):
            await asyncio.sleep(0)
        spent_mid = c.root.state.get("budget_spent", 0)
        with pytest.raises(RuntimeError):      # S1 契约：失败原样上抛（先发 call-failed 退款）
            await c.root.llm.complete(c.root, [{"role": "user", "content": "FAIL please"}],
                                      caller="fail_caller")
        assert c.root.state.get("budget_spent") == spent_mid, "失败退款应恰好回滚到失败前账面"
        pending = c.root.state.get("budget_pending_est") or {}
        assert not pending, f"两个槽位都应已消账: {pending}"
        await c.teardown()

    _run(s())
