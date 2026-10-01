"""阶段 2（2.1）：断点续传——探测 / 意图分流 / 快照截断 测试。

离线：单元测 ``_probe_resume``（fake ctx + InMemory 后端）；集成测真容器 + 注入 running
快照 → ``chat.send("继续")`` 走自动续接路径。

注（D18）：真 ``kill -9`` 双进程端到端需真 Redis 基建（fakeredis 进程内、无法跨进程），
故此处为**逻辑/单进程**验证；跨实例持久性由 Redis 自身保证。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.api import Services
from agent.plugins.react.loop import _snapshot_chain
from agent.plugins.work_memory import InMemoryWorkMemory
from agent.profiles import build_agent
from agent.sys.chat import ChatDriver
from agent.sys.plugin_manager import PluginManager


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path):
    PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    pm.set_enabled("plugins.react", True)
    pm.set_enabled("plugins.work_memory", True)
    return pm


def _build(tmp_path):
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       plugin_mgr=_pm(tmp_path))


class _Ctx:
    """最小 ctx stub：仅 ``_probe_resume`` 所需接缝。"""

    def __init__(self, wm):
        self._wm = wm

    def has(self, key):
        return key == Services.WORK_MEMORY and self._wm is not None

    def get(self, key):
        return self._wm if key == Services.WORK_MEMORY else None


def _running_snapshot(ts="2026-10-01T00:00:00Z", content="旧任务"):
    return {"chain": [{"role": "user", "content": content}],
            "head": {"prompt": content}, "step": 2, "budget_used": 100,
            "status": "running", "retry_count": 0, "ts": ts}


# ---------------------------------------------------------------------------
# _probe_resume：意图分流（D6/D8/D9/D15）
# ---------------------------------------------------------------------------


def test_probe_resume_continue_intent():
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        await wm.snapshot_save("default", "s:r1", _running_snapshot())
        drv = ChatDriver()
        state, notice = await drv._probe_resume(_Ctx(wm), "继续", True)
        assert state is not None and state["chain"][0]["content"] == "旧任务"
        assert notice and "未完成" in notice
        # 被续接者标 resumed，防重复续接
        assert (await wm.snapshot_load("default", "s:r1"))["status"] == "resumed"

    _run(s())


def test_probe_resume_new_task_marks_orphaned():
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        await wm.snapshot_save("default", "s:r1", _running_snapshot())
        drv = ChatDriver()
        state, notice = await drv._probe_resume(_Ctx(wm), "帮我查天气", False)
        assert state is None and notice is None
        assert (await wm.snapshot_load("default", "s:r1"))["status"] == "orphaned"

    _run(s())


def test_probe_resume_no_snapshot():
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        drv = ChatDriver()
        state, notice = await drv._probe_resume(_Ctx(wm), "继续", True)
        assert state is None and notice is None

    _run(s())


def test_probe_resume_multiple_keeps_latest():
    """D8：多个未完成 → 续最新 1 个（其余 orphaned）。"""
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        await wm.snapshot_save("default", "s:old",
                               _running_snapshot("2026-10-01T00:00:00Z", "老"))
        await wm.snapshot_save("default", "s:new",
                               _running_snapshot("2026-10-01T00:00:09Z", "新"))
        drv = ChatDriver()
        state, notice = await drv._probe_resume(_Ctx(wm), "继续", True)
        assert state is not None and state["chain"][0]["content"] == "新"
        assert (await wm.snapshot_load("default", "s:old"))["status"] == "orphaned"
        assert (await wm.snapshot_load("default", "s:new"))["status"] == "resumed"

    _run(s())


# ---------------------------------------------------------------------------
# 集成：真容器——注入崩溃残留 → chat.send("继续") 走自动续接
# ---------------------------------------------------------------------------


def test_auto_resume_end_to_end(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        wm = c.root.get(Services.WORK_MEMORY)
        # 注入上一轮「崩溃残留」（status=running，无收尾）
        await wm.snapshot_save("default", "default:crashed", _running_snapshot())
        reply = await c.root.chat.send(c.root, "继续")
        assert reply.notice and "未完成" in reply.notice
        # 被续接的原残留不再是 running（标 resumed → 不会重复续接）
        assert (await wm.snapshot_load("default", "default:crashed"))["status"] == "resumed"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 快照截断（D19）
# ---------------------------------------------------------------------------


def test_snapshot_chain_truncation():
    chain = [{"i": i} for i in range(50)]
    got = _snapshot_chain(chain, 20)
    assert len(got) == 20 and got[0]["i"] == 30 and got[-1]["i"] == 49
    assert _snapshot_chain(chain, 0) == [dict(m) for m in chain]   # keep<=0 → 全量
    assert len(_snapshot_chain(chain[:5], 20)) == 5
