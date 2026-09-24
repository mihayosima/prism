"""系统机制子系统测试 —— storage / tasks / llm / budget（离线确定性）。"""

from __future__ import annotations

import asyncio

import pytest

from agent.api import AgentError, CapabilityNotImplemented
from agent.profiles import build_agent


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path):
    return {"db_path": str(tmp_path / "agent.db")}


# ---------- storage：迁移 / 幂等 / 防脏武 ----------

def test_storage_migrations_create_tables(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        rows = await c.root.db.query(c.root, "SELECT name FROM agent_migrations ORDER BY name")
        assert len(rows) >= 7
        await c.teardown()

    _run(s())


def test_storage_migrations_idempotent_restart(tmp_path):
    path = str(tmp_path / "agent.db")

    async def s():
        c = build_agent(config={"db_path": path})
        await c.assemble()
        await c.teardown()
        c2 = build_agent(config={"db_path": path})
        await c2.assemble()          # 再次装配幂等，不重复建表
        assert c2.root.has("db")
        await c2.teardown()

    _run(s())


def test_storage_migrations_dirty_rejected(tmp_path):
    path = str(tmp_path / "agent.db")

    async def s():
        c = build_agent(config={"db_path": path})
        await c.assemble()
        await c.root.db.execute(c.root,
                                "INSERT INTO agent_migrations(name, applied_at) VALUES ('stray_mig','x')")
        await c.teardown()
        c2 = build_agent(config={"db_path": path})
        with pytest.raises(AgentError):
            await c2.assemble()      # 防脏武：已应用但代码已删 → 拒绝启动

    _run(s())


# ---------- tasks：注册 / schema 校验 / ping ----------

def test_tasks_ping_schema_and_unknown_kwargs(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        assert c.root.tasks.has("agent.ping")
        r = await c.root.tasks.invoke(c.root, "agent.ping")
        assert r["ok"] is True
        with pytest.raises(AgentError):            # 未知参数拒绝
            await c.root.tasks.invoke(c.root, "agent.ping", nope=1)
        with pytest.raises(AgentError):            # 类型校验
            await c.root.tasks.invoke(c.root, "agent.ping", action=3)
        await c.teardown()

    _run(s())


def test_tasks_unknown_task_errors(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        with pytest.raises(AgentError):
            await c.root.tasks.invoke(c.root, "no.such.task")
        await c.teardown()

    _run(s())


# ---------- llm：fixture 确定性 + 预算护栏 ----------

def test_llm_fixture_and_budget_deny(tmp_path):
    c = build_agent(config={**_cfg(tmp_path),
                            "agent": {"budget": {"max_tokens_per_run": 120}}})

    async def s():
        await c.assemble()
        lr = await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}],
                                       schema={}, caller="test")
        assert isinstance(lr.data, dict)
        assert lr.tokens_in > 0
        denied = False
        for _ in range(40):
            try:
                await c.root.llm.complete(c.root, [{"role": "user", "content": "x" * 80}],
                                          schema={}, caller="test")
            except CapabilityNotImplemented:
                denied = True
                break
        assert denied, "预算越限应触发拒绝"
        await asyncio.sleep(0.05)                # 让 post-call 记账任务落地（emit 为 fire-and-forget）
        rows = await c.root.db.query(c.root, "SELECT COUNT(*) AS n FROM agent_budget_log")
        assert rows[0]["n"] >= 1                 # 记账已落
        await c.teardown()

    _run(s())


def test_llm_placeholder_honest_when_real_unconfigured(tmp_path):
    c = build_agent(config={**_cfg(tmp_path),
                            "agent": {"llm": {"provider": "anthropic", "model": "x"}}})

    async def s():
        await c.assemble()
        with pytest.raises(CapabilityNotImplemented):
            await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}],
                                      schema={}, caller="t")
        await c.teardown()

    _run(s())