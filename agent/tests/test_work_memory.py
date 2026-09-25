"""工作记忆（第 3 层）测试 —— Redis 后端 + 内存降级 + react 集成。

不触网：Redis 路径用 fakeredis 内存协议；无 server 时插件自动降级内存版。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.api import Services
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager
from agent.sys.work_memory import (InMemoryWorkMemory, RedisWorkMemory,
                                   WorkMemory, WorkMemoryLike)

try:
    import fakeredis.aioredis as _fa
    _HAVE_FAKE = True
except Exception:                                                  # pragma: no cover
    _HAVE_FAKE = False


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path):
    PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    pm.set_enabled("plugins.react", True)
    return pm


def _build(tmp_path):
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       plugin_mgr=_pm(tmp_path))


# ---------------------------------------------------------------------------
# 内存实现：生命周期 / TTL / 检查点
# ---------------------------------------------------------------------------


def test_inmemory_lifecycle():
    wm = InMemoryWorkMemory(ttl=60)

    async def s():
        await wm.begin("t1", {"session_id": "s1", "run_id": "r1", "max_steps": 8})
        d = await wm.get("t1")
        assert d["status"] == "running" and d["step"] == 0
        await wm.update("t1", step=3, tools=["a", "b"], last_action="b")
        d = await wm.get("t1")
        assert d["step"] == 3 and d["tools"] == '["a", "b"]'
        await wm.checkpoint("t1", "step_3", {"tool_trace": ["a", "b"], "note": "ok"})
        cps = __import__("json").loads((await wm.get("t1"))["checkpoints"])
        assert len(cps) == 1 and cps[0]["name"] == "step_3"
        await wm.finish("t1")
        assert await wm.get("t1") is None

    _run(s())


def test_inmemory_ttl_expiry():
    wm = InMemoryWorkMemory(ttl=0)         # 立即过期

    async def s():
        await wm.begin("t2", {"session_id": "s2"})
        assert await wm.get("t2") is None   # TTL=0 → 已失效

    _run(s())


def test_inmemory_checkpoint_cap():
    wm = InMemoryWorkMemory(ttl=60)

    async def s():
        await wm.begin("t3", {"session_id": "s3"})
        for i in range(15):
            await wm.checkpoint("t3", f"c{i}", {"i": i})
        cps = __import__("json").loads((await wm.get("t3"))["checkpoints"])
        assert len(cps) == 10                # 列表上限 10
        assert cps[0]["name"] == "c5"        # 最旧丢弃

    _run(s())


# ---------------------------------------------------------------------------
# Redis 实现（fakeredis 内存协议；无依赖时跳过）
# ---------------------------------------------------------------------------


def test_redis_lifecycle():
    if not _HAVE_FAKE:
        return
    client = _fa.FakeRedis(decode_responses=True)

    async def s():
        wm = RedisWorkMemory(client, ttl=60, prefix="wm:")
        await wm.begin("t1", {"session_id": "s1", "run_id": "r1", "max_steps": 8})
        d = await wm.get("t1")
        assert d["status"] == "running"
        await wm.update("t1", step=3, tools=["a"], last_action="a")
        assert (await wm.get("t1"))["step"] == "3"      # Redis 侧为字符串
        await wm.checkpoint("t1", "step_3", {"x": 1})
        await wm.finish("t1")
        assert await wm.get("t1") is None
        assert await wm.list_active("s1") == []

    _run(s())


# ---------------------------------------------------------------------------
# 插件：降级纪律 + 解耦（无 Redis server → 内存版；消费方不崩）
# ---------------------------------------------------------------------------


def test_plugin_degrades_without_redis(tmp_path):
    """无 Redis server（即便装了 redis 库也无服务）→ 降级 InMemoryWorkMemory，不致崩。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        wm = c.root.get(Services.WORK_MEMORY)
        assert type(wm).__name__ == "WorkMemory"
        assert isinstance(wm._impl, InMemoryWorkMemory)
        await c.teardown()

    _run(s())


def test_react_writes_and_clears_work_memory(tmp_path):
    """集成：react 运行期经接缝写工作记忆；任务结束 finish 清空（不破解耦）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        wm = c.root.get(Services.WORK_MEMORY)
        impl = wm._impl
        # 包一层记录器（委托原实现）
        rec: list[str] = []

        class Rec(WorkMemoryLike):
            async def begin(self, t, m):
                rec.append("begin")
                return await impl.begin(t, m)

            async def update(self, t, **f):
                rec.append("update")
                return await impl.update(t, **f)

            async def checkpoint(self, t, n, d):
                rec.append("checkpoint")
                return await impl.checkpoint(t, n, d)

            async def get(self, t):
                return await impl.get(t)

            async def finish(self, t):
                rec.append("finish")
                return await impl.finish(t)

            async def list_active(self, ssn=None):
                return await impl.list_active(ssn)

        wm._impl = Rec()

        async def _echo(text: str = "hi"):
            return {"echo": text}

        c.root.tasks.register(c.root, "test.echo",
                              schema={"text": {"type": "str", "required": False, "default": "hi"}},
                              handler=_echo, owner="test", description="回显参数")
        r = await c.root.react.run(c.root, "两步链路：先回显再总结")
        assert r.ok
        assert "begin" in rec and "finish" in rec
        # 两步链路：每步一次 update + 一次 checkpoint
        assert rec.count("update") >= 1 and rec.count("checkpoint") >= 1
        # 任务结束 → 内存 store 已清空
        assert impl._store == {}
        await c.teardown()

    _run(s())
