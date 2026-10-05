"""主循环测试 —— produce 驱动：重叠 skip / deadline 上限 / 卸载无残留（P7）/ 完整离线闭环。"""

from __future__ import annotations

import asyncio

from agent.api import Services
from agent.profiles import build_agent


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path):
    return {"db_path": str(tmp_path / "agent.db")}


def test_loop_full_offline_closure_and_teardown_no_residue(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        r = await c.root.loop.run(c.root, task="agent.ping", params={})
        assert r.ok and r.span_ms >= 0
        # teardown：对称回收 —— 事件监听/任务注册全部解除（P7）
        events, tasks = c.root.events, c.root.tasks
        await c.teardown()
        assert c.root.closed
        assert events.listeners("agent/llm/pre-call") == []
        assert events.listeners("agent/tasks/pre-execute") == []
        assert tasks.names() == []
        assert not c.root.has(Services.DB)

    _run(s())


def test_loop_overlap_skip(tmp_path):
    """同源未结束 → 后到的 skip（确定性窗口：显式慢任务 + 先让前一个起飞）。"""
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        async def _slow(**_kw):
            await asyncio.sleep(0.15)
            return "ok"
        c.root.tasks.register(c.root, "test.slowping", schema={}, handler=_slow, owner="test")
        first = asyncio.create_task(c.root.loop.run(c.root, task="test.slowping", params={}))
        await asyncio.sleep(0.01)                 # 前一个已进入 running
        second = await c.root.loop.run(c.root, task="test.slowping", params={})
        assert second.skipped                      # 同源重叠 → skip
        first_result = await first
        assert first_result.ok
        await c.teardown()

    _run(s())


def test_loop_deadline_timeout(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        async def _slow(**kw):
            await asyncio.sleep(5)
            return "done"
        c.root.tasks.register(c.root, "test.slow", schema={}, handler=_slow, owner="test")
        c.root.config["agent"]["loop"]["deadline_s"] = 0.05
        r = await c.root.loop.run(c.root, task="test.slow", params={})
        assert not r.ok and "deadline" in (r.error or "")
        await c.teardown()

    _run(s())


def test_loop_unknown_task(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        r = await c.root.loop.run(c.root, task="no.such.task", params={})
        assert not r.ok and "不存在" in (r.error or "")
        await c.teardown()

    _run(s())
