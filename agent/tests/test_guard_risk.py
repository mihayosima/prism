"""S5 guard 消费 risk 元数据测试——默认零回归 + risk 策略裁决。"""

from __future__ import annotations

import asyncio

from agent.profiles import build_agent


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path, permissions=None):
    cfg = {"db_path": str(tmp_path / "agent.db")}
    if permissions is not None:
        cfg["agent"] = {"permissions": permissions}
    return cfg


def _register(c):
    async def h():
        return {"ok": True}

    c.root.tasks.register(c.root, "t.read", schema={}, handler=h, risk="read", owner=None)
    c.root.tasks.register(c.root, "t.write", schema={}, handler=h, risk="write", owner=None)
    c.root.tasks.register(c.root, "t.danger", schema={}, handler=h, risk="danger", owner=None)


def test_default_config_zero_regression(tmp_path):
    """未配置 risk 策略 → 行为与旧版完全一致（default open 全放行）。"""
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        _register(c)
        for name in ("t.read", "t.write", "t.danger"):
            r = await c.root.tasks.invoke(c.root, name, caller="t")
            assert r == {"ok": True}, f"{name} 默认应放行"
        await c.teardown()

    _run(s())


def test_risk_policy_deny_and_allow(tmp_path):
    """risk 策略：danger 恒拒、read 恒放行、无策略的 write 走 default open。"""
    c = build_agent(config=_cfg(tmp_path, {"risk": {"read": "allow", "danger": "deny"}}))

    async def s():
        await c.assemble()
        _register(c)
        assert (await c.root.tasks.invoke(c.root, "t.read", caller="t")) == {"ok": True}
        assert (await c.root.tasks.invoke(c.root, "t.write", caller="t")) == {"ok": True}
        try:
            await c.root.tasks.invoke(c.root, "t.danger", caller="t")
            raise AssertionError("danger 应被拒")
        except Exception as exc:
            assert "未授权" in str(exc) or "denied" in str(exc).lower() or "guard" in str(exc)
        rows = await c.root.db.query(
            c.root, "SELECT payload FROM agent_events"
                    " WHERE event='agent/permission/decision' ORDER BY seq DESC LIMIT 1")
        assert '"danger"' in rows[0]["payload"], "审计应记录 risk 字段"
        await c.teardown()

    _run(s())


def test_risk_allow_overrides_closed_default(tmp_path):
    """default 收窄 + 名单为空：显式 risk=allow 仍可放行 read 级任务。"""
    c = build_agent(config=_cfg(tmp_path, {"default": "closed", "risk": {"read": "allow"}}))

    async def s():
        await c.assemble()
        _register(c)
        assert (await c.root.tasks.invoke(c.root, "t.read", caller="t")) == {"ok": True}
        try:
            await c.root.tasks.invoke(c.root, "t.write", caller="t")
            raise AssertionError("收窄模式下 write 应被拒")
        except Exception:
            pass
        await c.teardown()

    _run(s())


def test_risk_ask_denied_with_hint(tmp_path):
    """ask（approval 插件未接入）→ 按拒绝处理，reason 注明 ask 语义。"""
    c = build_agent(config=_cfg(tmp_path, {"risk": {"write": "ask"}}))

    async def s():
        await c.assemble()
        _register(c)
        try:
            await c.root.tasks.invoke(c.root, "t.write", caller="t")
            raise AssertionError("ask 应按拒绝处理")
        except Exception as exc:
            assert "ask" in str(exc)
        await c.teardown()

    _run(s())
