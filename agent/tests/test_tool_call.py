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