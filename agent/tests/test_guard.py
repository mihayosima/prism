"""权限网关测试 —— 默认 open（不先限能力）· 收窄矩阵拒绝 · 审计落 durable 事件（P4 证明）。"""

from __future__ import annotations

import asyncio

import pytest

from agent.api import CapabilityNotImplemented
from agent.profiles import TIGHT_PATCH, build_agent


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path):
    return {"db_path": str(tmp_path / "agent.db")}


def test_offline_default_open_allows_all(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        assert c.root.guard.policy(c.root)["default"] == "open"
        r = await c.root.loop.run(c.root, task="agent.ping", params={})
        assert r.ok
        rows = await c.root.db.query(
            c.root, "SELECT payload FROM agent_events"
                    " WHERE event='agent/permission/decision' ORDER BY seq DESC LIMIT 1")
        assert "allow" in rows[0]["payload"]
        await c.teardown()

    _run(s())


def test_tight_deny_pick_via_capability_event(tmp_path):
    """矩阵收窄 → 拒绝走 guard 的 capability 事件路径（非散装 if）+ 审计落 durable。"""
    c = build_agent(config=_cfg(tmp_path), patch=TIGHT_PATCH)

    async def s():
        await c.assemble()
        r = await c.root.loop.run(c.root, task="agent.ping", params={})
        assert not r.ok
        assert "guard" in (r.error or "")
        rows = await c.root.db.query(
            c.root, "SELECT payload FROM agent_events"
                    " WHERE event='agent/permission/decision' ORDER BY seq DESC LIMIT 1")
        assert "deny" in rows[0]["payload"]
        assert rows[0]["payload"].find("agent.ping") != -1
        await c.teardown()

    _run(s())


def test_guard_llm_deny_matrix(tmp_path):
    c = build_agent(config=_cfg(tmp_path),
                    patch={"agent": {"permissions": {"llm": "deny"}}})

    async def s():
        await c.assemble()
        with pytest.raises(CapabilityNotImplemented):
            await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}],
                                      schema={}, caller="t")
        await c.teardown()

    _run(s())


def test_matrix_is_config_not_code(tmp_path):
    """矩阵是可配置数据：运行期改配置即换行为（不用改架构/代码）。"""
    c = build_agent(config=_cfg(tmp_path), patch=TIGHT_PATCH)

    async def s():
        await c.assemble()
        r = await c.root.loop.run(c.root, task="agent.ping", params={})
        assert not r.ok
        c.root.config["agent"]["permissions"]["task_deny"].remove("agent.ping")
        r2 = await c.root.loop.run(c.root, task="agent.ping", params={})
        assert r2.ok
        await c.teardown()

    _run(s())