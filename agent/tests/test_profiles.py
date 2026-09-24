"""装配 profile 测试 —— P6 叠层/patch：同一套插件，patch 差异决定行为；offline 默认全开 vs tight 收窄。"""

from __future__ import annotations

import asyncio

from agent.profiles import TIGHT_PATCH, build_agent


def _run(coro):
    return asyncio.run(coro)


def test_profiles_same_plugins_different_patch(tmp_path):
    off = build_agent(config={"db_path": str(tmp_path / "off.db")})
    tight = build_agent(config={"db_path": str(tmp_path / "tight.db")}, patch=TIGHT_PATCH)

    async def s():
        await off.assemble()
        await tight.assemble()
        r_off = await off.root.loop.run(off.root, task="agent.ping", params={})
        r_tight = await tight.root.loop.run(tight.root, task="agent.ping", params={})
        assert r_off.ok
        assert not r_tight.ok and "guard" in (r_tight.error or "")
        # 同键服务两容器并存（@plugin 单例跨容器，子系统读 ctx.config / ctx.state 隔离）
        assert off.root.tasks.has("agent.ping")
        assert tight.root.tasks.has("agent.ping")
        await off.teardown()
        await tight.teardown()

    _run(s())


def test_patch_overrides_base_key(tmp_path):
    """patch 深度覆盖 base（P6 叠层最后覆盖）。"""
    c = build_agent(config={"db_path": str(tmp_path / "a.db")},
                    patch={"agent": {"budget": {"max_tokens_per_run": 7}}})
    base = build_agent(config={"db_path": str(tmp_path / "b.db")})

    async def s():
        await c.assemble()
        await base.assemble()
        assert c.root.config["agent"]["budget"]["max_tokens_per_run"] == 7
        assert base.root.config["agent"]["budget"]["max_tokens_per_run"] != 7
        await c.teardown()
        await base.teardown()

    _run(s())