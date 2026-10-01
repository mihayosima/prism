"""工作记忆（第 3 层）测试 —— Redis 后端 + 内存降级 + react 集成。

不触网：Redis 路径用 fakeredis 内存协议；无 server 时插件自动降级内存版。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.api import Services
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager
from agent.plugins.work_memory import (InMemoryWorkMemory, RedisWorkMemory,
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
    pm.set_enabled("plugins.work_memory", True)      # 目录插件：opt-in
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
        # 注意：目录插件由 PluginManager 按**文件路径**加载，类身份与 ``agent.plugins.*``
        # 导入路径下的类对象不同（两个模块对象）→ 用类型名断言，不用 isinstance。
        assert type(wm._impl).__name__ == "InMemoryWorkMemory"
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

            async def snapshot_save(self, tenant, task_id, data, ttl=None):
                rec.append("snapshot_save")
                return await impl.snapshot_save(tenant, task_id, data, ttl)

            async def snapshot_load(self, tenant, task_id):
                return await impl.snapshot_load(tenant, task_id)

            async def snapshot_list(self, tenant):
                return await impl.snapshot_list(tenant)

            async def snapshot_mark_finished(self, tenant, task_id):
                rec.append("snapshot_mark_finished")
                return await impl.snapshot_mark_finished(tenant, task_id)

            async def snapshot_flag_finished(self, tenant, task_id):
                return await impl.snapshot_flag_finished(tenant, task_id)

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
        # 阶段 2（2.1）：每轮 Observe 后写快照、收尾置完成标记
        assert "snapshot_save" in rec and "snapshot_mark_finished" in rec
        # 任务结束 → 任务自身运行态已清空；react_chain:{session} 归档键为续接保留（设计意图，非泄漏）
        # 后端无关断言：底层 store 随 Redis/内存两种实现而异（Redis 版无 _store 属性，
        # 且本机装了 redis 库后 _impl 即为 RedisWorkMemory），故经 WorkMemory 接缝的
        # list_active() 取活跃键，仅允许 react_chain: 前缀，避免依赖具体后端字段。
        active = await wm.list_active()
        assert any(k.startswith("react_chain:") for k in active), "运行态未清空：无 react_chain 归档键"
        assert all(k.startswith("react_chain:") for k in active), "不应混入非 react_chain 键（运行态泄漏）"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 阶段 2（2.0）：跨进程 run 快照原语（键 run_snapshot:{tenant}:{task_id}）
# ---------------------------------------------------------------------------


def test_snapshot_inmemory_roundtrip_and_isolation():
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        await wm.snapshot_save("default", "s1:r1",
                               {"chain": [{"role": "user"}], "head": None,
                                "step": 2, "budget_used": 0, "status": "running",
                                "ts": "2026-10-01T00:00:00Z"})
        d = await wm.snapshot_load("default", "s1:r1")
        assert d["status"] == "running" and d["step"] == 2
        # tenant 隔离：另一 tenant 读不到
        assert await wm.snapshot_load("t2", "s1:r1") is None
        # list 仅本 tenant，按 ts 倒序
        await wm.snapshot_save("default", "s1:r2",
                               {"status": "running", "ts": "2026-10-01T00:00:09Z"})
        ids = [x["task_id"] for x in await wm.snapshot_list("default")]
        assert ids == ["s1:r2", "s1:r1"]
        assert await wm.snapshot_list("t2") == []
        # mark_finished 置位
        await wm.snapshot_mark_finished("default", "s1:r1")
        assert (await wm.snapshot_load("default", "s1:r1"))["status"] == "finished"

    _run(s())


def test_snapshot_ttl_expiry():
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=0)   # 快照立即过期

    async def s():
        await wm.snapshot_save("default", "s:r", {"status": "running"})
        assert await wm.snapshot_load("default", "s:r") is None
        assert await wm.snapshot_list("default") == []

    _run(s())


def test_finish_does_not_touch_snapshot():
    """D1 锁死：wm.finish 只删工作记忆键，绝不触碰 run_snapshot: 前缀。"""
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        await wm.begin("s1:r1", {"session_id": "s1"})
        await wm.snapshot_save("default", "s1:r1", {"status": "running"})
        await wm.finish("s1:r1")
        assert await wm.get("s1:r1") is None                                # 工作记忆已清
        assert (await wm.snapshot_load("default", "s1:r1")) is not None     # 快照仍在

    _run(s())


def test_snapshot_redis_roundtrip():
    if not _HAVE_FAKE:
        return
    client = _fa.FakeRedis(decode_responses=True)

    async def s():
        wm = RedisWorkMemory(client, ttl=60, prefix="wm:", snapshot_ttl=60)
        await wm.snapshot_save("default", "s1:r1",
                               {"chain": [{"role": "user"}], "status": "running",
                                "ts": "2026-10-01T00:00:00Z"})
        d = await wm.snapshot_load("default", "s1:r1")
        assert d["status"] == "running" and d["chain"][0]["role"] == "user"
        assert await wm.snapshot_load("t2", "s1:r1") is None
        await wm.snapshot_save("default", "s1:r2",
                               {"status": "running", "ts": "2026-10-01T00:00:09Z"})
        ids = [x["task_id"] for x in await wm.snapshot_list("default")]
        assert ids == ["s1:r2", "s1:r1"]
        # 确认键（run_snapshot_finished:）不污染 snapshot_list（前缀不同）
        await wm.snapshot_flag_finished("default", "s1:r1")
        assert [x["task_id"] for x in await wm.snapshot_list("default")] == ["s1:r2", "s1:r1"]

    _run(s())


def test_plugin_required_raises_when_redis_unavailable(tmp_path):
    """D10：required=True 且 Redis 不可用 → 装配期（启动）失败，不静默降级。

    用不可达 URL（端口 1）确保"Redis 不可用"，不依赖本机是否真跑着 Redis。
    """
    c = build_agent(
        config={"db_path": str(tmp_path / "agent.db"),
                "agent": {"work_memory": {"required": True,
                                          "url": "redis://127.0.0.1:1/0"}}},
        plugin_mgr=_pm(tmp_path))

    async def s():
        try:
            await c.assemble()
        except Exception as exc:                             # noqa: BLE001
            assert "redis unavailable" in str(exc) or "required" in str(exc)
            return
        raise AssertionError("required=True 且 Redis 不可用时应装配失败（启动失败）")

    _run(s())
