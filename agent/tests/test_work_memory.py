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
    """Redis 不可用 → 降级 InMemoryWorkMemory，不致崩。

    用**显式不可达 URL**（端口 1）构造「无 Redis」，不再依赖「本机恰好没跑 Redis」
    —— 后者会让本测试在起了 Redis 的环境（本地部署验证、CI 加 redis service）里假红。
    """
    c = build_agent(
        config={"db_path": str(tmp_path / "agent.db"),
                "agent": {"work_memory": {"url": "redis://127.0.0.1:1/0"}}},
        plugin_mgr=_pm(tmp_path))

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


# ---------------------------------------------------------------------------
# Redis 连接串解析：显式配置 > PRISM_REDIS_URL > 默认
#
# 为什么要有 env 通道：容器/编排部署下无法预置 data/agent_plugin_config.json
# （data/ 是挂载卷且被 gitignore），而 compose 网络内必须用服务名 redis:6379。
# ---------------------------------------------------------------------------

def test_resolve_url_prefers_explicit_config(monkeypatch):
    from agent.plugins.work_memory import resolve_redis_url
    monkeypatch.setenv("PRISM_REDIS_URL", "redis://from-env:6379/0")
    assert resolve_redis_url({"url": "redis://from-config:6379/0"}) == \
        "redis://from-config:6379/0"


def test_resolve_url_falls_back_to_env(monkeypatch):
    from agent.plugins.work_memory import resolve_redis_url
    monkeypatch.setenv("PRISM_REDIS_URL", "redis://from-env:6379/0")
    assert resolve_redis_url({}) == "redis://from-env:6379/0"
    assert resolve_redis_url(None) == "redis://from-env:6379/0"


def test_resolve_url_default_when_nothing_set(monkeypatch):
    from agent.plugins.work_memory import resolve_redis_url
    monkeypatch.delenv("PRISM_REDIS_URL", raising=False)
    assert resolve_redis_url({}) == "redis://localhost:6379/0"


def test_resolve_url_ignores_blank_values(monkeypatch):
    """空串/纯空白视同未设置 —— 否则编排里一个空的 ${PRISM_REDIS_URL}
    会把连接串变成空串，报出难以理解的解析错误。"""
    from agent.plugins.work_memory import resolve_redis_url
    monkeypatch.setenv("PRISM_REDIS_URL", "   ")
    assert resolve_redis_url({"url": "  "}) == "redis://localhost:6379/0"


def test_env_url_reaches_redis_client(tmp_path, monkeypatch):
    """端到端：env 里的 URL 确实被**交给 Redis 客户端**（而非只在纯函数里生效）。

    换掉 ``redis.asyncio.from_url`` 捕获入参 —— 只看 ``resolve_redis_url`` 的返回值
    证明不了「apply 真的用了它」。
    """
    import redis.asyncio as aioredis

    seen: dict = {}

    def _fake_from_url(url, **kw):                           # noqa: ANN001
        seen["url"] = url
        raise RuntimeError("stop-here：仅为捕获 url，不求连通")

    monkeypatch.setattr(aioredis, "from_url", _fake_from_url)
    monkeypatch.setenv("PRISM_REDIS_URL", "redis://from-env:6379/0")

    c = build_agent(
        config={"db_path": str(tmp_path / "agent.db"),
                "agent": {"work_memory": {"required": True}}},   # 故意不写 url
        plugin_mgr=_pm(tmp_path))

    async def s():
        try:
            await c.assemble()
        except Exception:                                    # noqa: BLE001 —— 预期装配失败
            pass

    _run(s())
    assert seen.get("url") == "redis://from-env:6379/0", \
        f"env 的 url 未被交给 redis 客户端：{seen}"


def test_resolve_required_defaults_false(monkeypatch):
    from agent.plugins.work_memory import resolve_required
    monkeypatch.delenv("PRISM_REDIS_REQUIRED", raising=False)
    assert resolve_required({}) is False
    assert resolve_required(None) is False


def test_resolve_required_explicit_config_wins(monkeypatch):
    """显式配置优先于 env —— 与 url 的优先级口径一致。"""
    from agent.plugins.work_memory import resolve_required
    monkeypatch.setenv("PRISM_REDIS_REQUIRED", "1")
    assert resolve_required({"required": False}) is False
    assert resolve_required({"required": True}) is True


def test_resolve_required_from_env(monkeypatch):
    from agent.plugins.work_memory import resolve_required
    for v in ("1", "true", "TRUE", "yes", "on", " 1 "):
        monkeypatch.setenv("PRISM_REDIS_REQUIRED", v)
        assert resolve_required({}) is True, f"{v!r} 应判为 True"
    for v in ("", " ", "0", "false", "no", "off", "anything"):
        monkeypatch.setenv("PRISM_REDIS_REQUIRED", v)
        assert resolve_required({}) is False, f"{v!r} 应判为 False"
