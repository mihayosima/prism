"""B5/B6 测试：进度桥接（B5.1）+ 取消（B6）。

**绕开 HTTP 层**：测试环境（envs/default）无 httpx / starlette，SSE 端点无法做 ASGI 单测。
故这里只测绕开 HTTP 的部分——桥接层与取消路径；端点本身靠真实服务冒烟验证。
慢任务沿用 test_loop_offline.py 的 test.slow 模板；触发工具调用沿用 test_tool_call.py 的"调用工具"。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.api import Events
from agent.profiles import build_agent
from agent.sys.progress import tap_progress


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path):
    return {"db_path": str(tmp_path / "agent.db")}


def test_tap_progress_receives_frames_and_auto_disposes(tmp_path):
    """桥接层：能收到帧；退出作用域后监听**自动注销**（scope effect 对称回收）。"""
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        await c.start()
        try:
            async with tap_progress(c.root) as q:
                await c.root.events.emit(Events.REACT_STEP_START,
                                         {"step": 1, "session_id": "s"})
                frame = await asyncio.wait_for(q.get(), timeout=2.0)
                assert frame["event"] == "progress"
                assert frame["payload"]["source"] == Events.REACT_STEP_START
                assert frame["payload"]["data"] == {"step": 1, "session_id": "s"}
                assert len(c.root.events.listeners(Events.REACT_STEP_START)) == 1
            assert c.root.events.listeners(Events.REACT_STEP_START) == []
        finally:
            await c.teardown()

    _run(s())


def test_tap_progress_degrades_without_react(tmp_path):
    """与 ReAct 零耦合：无 react 时收不到 REACT_* 帧属**优雅降级**，不报错。"""
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        await c.start()
        try:
            assert not c.root.has("react")
            async with tap_progress(c.root) as q:
                await c.root.events.emit(Events.TASKS_POST_EXECUTE,
                                         {"task": "agent.ping", "caller": "test"})
                frame = await asyncio.wait_for(q.get(), timeout=2.0)
                assert frame["event"] == "progress"
                assert frame["payload"]["source"] == Events.TASKS_POST_EXECUTE
        finally:
            await c.teardown()

    _run(s())


def test_produce_driver_cancel_and_when_idle(tmp_path):
    """B6 协议层：`cancel()` 即时中断在跑任务；`when_idle()` 等到空闲。"""
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        await c.start()
        try:
            async def _slow():
                await asyncio.sleep(5)
                return "done"
            c.root.tasks.register(c.root, "test.slow", schema={}, handler=_slow, owner="test")
            drv = c.root.loop
            t = asyncio.create_task(drv.run(c.root, task="test.slow", params={}))
            await asyncio.sleep(0.05)
            assert "test.slow" in drv._running
            await drv.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t
            await drv.when_idle()
            assert drv._running == {}
        finally:
            await c.teardown()

    _run(s())


def test_cancel_mid_tool_is_recorded_as_cancelled(tmp_path):
    """B6 诚实收尾：中途取消 → CancelledError 上浮，且 CHAT_DONE 如实标注 cancelled。"""
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        await c.start()
        try:
            async def _slow_echo(text="hi"):
                await asyncio.sleep(5)
                return {"echo": text}
            c.root.tasks.register(
                c.root, "test.echo",
                schema={"text": {"type": "str", "required": False, "default": "hi"}},
                handler=_slow_echo, owner="test", description="回显参数")
            t = asyncio.create_task(c.root.chat.send(c.root, "调用工具"))
            await asyncio.sleep(0.3)              # 等规划完成、进入工具执行
            t.cancel()
            with pytest.raises(asyncio.CancelledError):
                await t
            rows = await c.root.db.query(
                c.root, "SELECT payload FROM agent_events"
                        " WHERE event='agent/chat/done' ORDER BY seq DESC LIMIT 5")
            assert any('"cancelled": true' in (r["payload"] or "") for r in rows), rows
        finally:
            await c.teardown()

    _run(s())
