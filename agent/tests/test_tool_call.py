"""tool-call 意图 + 工具目录测试（离线确定性）。"""

from __future__ import annotations

import asyncio

from agent.profiles import build_agent


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path):
    return {"db_path": str(tmp_path / "agent.db")}


def test_tool_call_intent_via_chat(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        async def _echo(text="hi"):
            return {"echo": text}
        c.root.tasks.register(c.root, "test.echo",
                              schema={"text": {"type": "str", "required": False, "default": "hi"}},
                              handler=_echo, owner="test", description="回显参数")
        reply = await c.root.chat.send(c.root, "调用工具")
        assert reply.plan["intent"] == "tool_call"
        assert reply.plan["tool"] == "test.echo"
        assert reply.evidence["ok"] is True
        assert reply.evidence["result"] == {"echo": "hi"}     # default 填充 + 执行结果
        await c.teardown()

    _run(s())


def test_tasks_catalog(tmp_path):
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        async def _echo(text="hi"):
            return text
        c.root.tasks.register(c.root, "test.echo",
                              schema={"text": {"type": "str", "required": False, "default": "hi"}},
                              handler=_echo, owner="test", description="回显参数")
        cat = c.root.tasks.catalog()
        entry = next(t for t in cat if t["name"] == "test.echo")
        assert entry["description"] == "回显参数"
        assert entry["params"]["text"]["type"] == "str"
        # include 过滤：只列白名单工具
        ping = c.root.tasks.catalog(include={"agent.ping"})
        assert [t["name"] for t in ping] == ["agent.ping"]
        await c.teardown()

    _run(s())

# ---------------------------------------------------------------------------
# H3（2026-10-04）：单次工具执行必须有上限
# ---------------------------------------------------------------------------
# 缺陷：``tasks.invoke`` 里 ``await cmd.invoke(...)`` 是裸 await，而 run 的 deadline
# 只在**循环顶**检查 ⇒ 一个卡死的非 MCP 工具会让整个 run 永久挂住
# （既到不了 deadline 检查点，也释放不了 worker）。

def test_invoke_timeout_raises_and_is_distinguishable(tmp_path):
    """超时抛 ``TaskTimeout``（独立类型，调用方可只对超时做特殊处置）。"""
    import pytest
    from agent.sys.tasks import TaskTimeout

    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()

        async def _stuck(**kw):
            await asyncio.sleep(30)

        c.root.tasks.register(c.root, "test.stuck", schema={}, handler=_stuck,
                              owner="test")
        with pytest.raises(TaskTimeout) as ei:
            await c.root.tasks.invoke(c.root, "test.stuck", timeout_s=0.05)
        assert "超时" in str(ei.value)
        await c.teardown()

    _run(s())


def test_register_level_timeout_applies_without_caller_hint(tmp_path):
    """注册时声明的 timeout 对所有调用生效（调用方不传也拦得住）。"""
    import pytest
    from agent.sys.tasks import TaskTimeout

    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()

        async def _stuck(**kw):
            await asyncio.sleep(30)

        c.root.tasks.register(c.root, "test.stuck2", schema={}, handler=_stuck,
                              owner="test", timeout_s=0.05)
        with pytest.raises(TaskTimeout):
            await c.root.tasks.invoke(c.root, "test.stuck2")     # 不传 timeout_s
        await c.teardown()

    _run(s())


def test_no_timeout_means_unlimited_by_default(tmp_path):
    """默认（未声明、未传参）= **不限时** ⇒ 不劣化既有长任务。"""
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()

        async def _slow(**kw):
            await asyncio.sleep(0.15)
            return {"ok": True}

        c.root.tasks.register(c.root, "test.slowish", schema={}, handler=_slow,
                              owner="test")
        out = await c.root.tasks.invoke(c.root, "test.slowish")
        assert out == {"ok": True}
        await c.teardown()

    _run(s())


def test_timeout_emits_post_execute_event(tmp_path):
    """超时要发 ``tasks/post-execute``（S7：审计不缺失败样本）。

    ⚠️ 该事件走 **emit（内存总线）不是 durable**，因此**不落 agent_events** ——
    断言必须订总线，不能查库（我先前查库得到空集，是测试自身的前提错误）。
    """
    import pytest
    from agent.api import Events
    from agent.sys.tasks import TaskTimeout

    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        frames: list = []
        c.root.on(Events.TASKS_POST_EXECUTE, lambda p: frames.append(p))

        async def _stuck(**kw):
            await asyncio.sleep(30)

        c.root.tasks.register(c.root, "test.stuck3", schema={}, handler=_stuck,
                              owner="test")
        with pytest.raises(TaskTimeout):
            await c.root.tasks.invoke(c.root, "test.stuck3", timeout_s=0.05)
        assert frames, "超时未发 post-execute 事件"
        last = frames[-1]
        assert last["ok"] is False and "超时" in (last["error"] or "")
        await c.teardown()

    _run(s())


def test_react_passes_remaining_deadline_to_actor(tmp_path):
    """★ react 侧：``act`` 拿到的是**剩余** deadline（源码级，防漏接线）。"""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "plugins" / "react" / "loop.py"
           ).read_text(encoding="utf-8")
    assert "_act_with_budget" in src, "loop 未按剩余 deadline 执行工具调用"
    assert "cfg.deadline_s" in src
