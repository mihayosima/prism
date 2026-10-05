"""阶段 2（2.1）：断点续传——探测 / 意图分流 / 快照截断 测试。

离线：单元测 ``_probe_resume``（fake ctx + InMemory 后端）；集成测真容器 + 注入 running
快照 → ``chat.send("继续")`` 走自动续接路径。

注（D18）：真 ``kill -9`` 双进程端到端需真 Redis 基建（fakeredis 进程内、无法跨进程），
故此处为**逻辑/单进程**验证；跨实例持久性由 Redis 自身保证。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.api import Services, make_identity
from agent.plugins.react.loop import _snapshot_chain
from agent.plugins.work_memory import InMemoryWorkMemory
from agent.profiles import build_agent
from agent.sys.chat import ChatDriver
from agent.sys.plugin_manager import PluginManager

# 2.2：探测改为**会话级**前缀（H1），故快照 task_id 必须由同一身份构造
_IDENT = make_identity()          # tenant/user/session 全 default


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
        tid = _IDENT.task_id("r1")
        await wm.snapshot_save(_IDENT.tenant, tid, _running_snapshot())
        drv = ChatDriver()
        state, notice = await drv._probe_resume(_Ctx(wm), "继续", True, _IDENT)
        assert state is not None and state["chain"][0]["content"] == "旧任务"
        assert notice and "未完成" in notice
        # 被续接者标 resumed，防重复续接
        assert (await wm.snapshot_load(_IDENT.tenant, tid))["status"] == "resumed"

    _run(s())


def test_probe_resume_new_task_marks_orphaned():
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        tid = _IDENT.task_id("r1")
        await wm.snapshot_save(_IDENT.tenant, tid, _running_snapshot())
        drv = ChatDriver()
        state, notice = await drv._probe_resume(_Ctx(wm), "帮我查天气", False, _IDENT)
        assert state is None and notice is None
        assert (await wm.snapshot_load(_IDENT.tenant, tid))["status"] == "orphaned"

    _run(s())


def test_probe_resume_no_snapshot():
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        drv = ChatDriver()
        state, notice = await drv._probe_resume(_Ctx(wm), "继续", True, _IDENT)
        assert state is None and notice is None

    _run(s())


def test_probe_resume_multiple_keeps_latest():
    """D8：多个未完成 → 续最新 1 个（其余 orphaned）。"""
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        old, new = _IDENT.task_id("old"), _IDENT.task_id("new")
        await wm.snapshot_save(_IDENT.tenant, old,
                               _running_snapshot("2026-10-01T00:00:00Z", "老"))
        await wm.snapshot_save(_IDENT.tenant, new,
                               _running_snapshot("2026-10-01T00:00:09Z", "新"))
        drv = ChatDriver()
        state, notice = await drv._probe_resume(_Ctx(wm), "继续", True, _IDENT)
        assert state is not None and state["chain"][0]["content"] == "新"
        assert (await wm.snapshot_load(_IDENT.tenant, old))["status"] == "orphaned"
        assert (await wm.snapshot_load(_IDENT.tenant, new))["status"] == "resumed"

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
        tid = _IDENT.task_id("crashed")            # {user}:{session}:{run}
        await wm.snapshot_save(_IDENT.tenant, tid, _running_snapshot())
        reply = await c.root.chat.send(c.root, "继续", session_id=_IDENT.session_key,
                                       identity=_IDENT)
        assert reply.notice and "未完成" in reply.notice
        # 被续接的原残留不再是 running（标 resumed → 不会重复续接）
        assert (await wm.snapshot_load(_IDENT.tenant, tid))["status"] == "resumed"
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


# ---------------------------------------------------------------------------
# H1（2026-10-04）：取消/异常路径必须把快照挪出 ``running``
# ---------------------------------------------------------------------------
# 缺陷：``react.loop.run`` 主体只在**收敛**与**触顶**两条路径做收尾；被取消
# （客户端断开、请求超时）时整段跳过 ⇒ 快照停在 ``status="running"`` ⇒ 下次请求
# ``_probe_resume`` 把它当**崩溃残留自动续接** —— 静默的错误状态延续。

def test_abandoned_snapshot_is_not_auto_resumed():
    """标成 ``cancelled`` 的快照**不得**被自动续接（这是修 H1 的目的）。"""
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        tid = _IDENT.task_id("rX")
        await wm.snapshot_save(_IDENT.tenant, tid, _running_snapshot())
        drv = ChatDriver()
        await wm.snapshot_mark_abandoned(_IDENT.tenant, tid)
        state, notice = await drv._probe_resume(_Ctx(wm), "继续", True, _IDENT)
        assert state is None, "cancelled 的快照仍被自动续接 ⇒ 用户会看到被中断的任务复活"
        assert notice is None

    _run(s())


def test_abandon_is_not_labelled_finished():
    """★ 取消**不能**标成 ``finished``——那是对状态的谎报（审计里查不到"被中断"）。"""
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        tid = _IDENT.task_id("rY")
        await wm.snapshot_save(_IDENT.tenant, tid, _running_snapshot())
        await wm.snapshot_mark_abandoned(_IDENT.tenant, tid)
        data = await wm.snapshot_load(_IDENT.tenant, tid)
        assert data is not None
        assert data["status"] == "cancelled", f"状态被标成 {data['status']!r}"

    _run(s())


def test_abandon_marks_snapshot_cancelled(tmp_path):
    """``_abandon`` 真把 ``running`` 快照挪成 ``cancelled``（过真实 backend）。

    ⚠️ 曾经写过一版"端到端：取消 react.run ⇒ 快照不再 running"，它是**假阳性**：
    取消发生在首个 ``reason`` 之前，而快照要到 Act+Observe 之后才写 ``running``，
    压根没有 running 行可断言 ⇒ 破坏生产代码也不红。破坏实验逼出的修正。
    """
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        wm = c.root.get(Services.WORK_MEMORY)
        ident = _IDENT
        tid = ident.task_id("rZ")
        await wm.snapshot_save(ident.tenant, tid, _running_snapshot())
        box = {"task_id": tid, "ident": ident, "wm": wm, "steps": 3}
        await c.root.react._abandon(c.root, box, asyncio.CancelledError())
        data = await wm.snapshot_load(ident.tenant, tid)
        assert data is not None and data["status"] == "cancelled",             f"_abandon 未把 running 快照挪走：{data}"
        await c.teardown()

    _run(s())


def test_run_invokes_abandon_on_cancel(tmp_path):
    """★ ``run`` 的异常/取消路径**确实**调用 ``_abandon``（覆盖"包装层有没有接上"）。

    这条才是能抓回归的：把 ``run`` 里的兜底去掉，本用例立刻红。
    """
    c = _build(tmp_path)
    entered = asyncio.Event()
    seen: list = []

    class _StuckReasoner:
        async def reason(self, ctx, window, tools):
            entered.set()
            await asyncio.sleep(30)             # 卡住直到被取消

    async def s():
        await c.assemble()
        c.root.react._reasoner = _StuckReasoner()          # 故障注入（仅测试）

        async def _spy(ctx, box, exc):
            seen.append(exc)
        c.root.react._abandon = _spy

        task = asyncio.create_task(c.root.react.run(c.root, "查结构", session_id="default"))
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert seen and isinstance(seen[0], asyncio.CancelledError),             "取消路径没走 _abandon ⇒ 快照停在 running，下次会被当崩溃残留自动续接"
        await c.teardown()

    _run(s())


def test_abandon_never_raises_into_the_cancelling_task(tmp_path):
    """★ ``_abandon`` 跑在**取消上下文**里：它自身的任何失败都必须被吞掉。

    否则收尾动作会替掉**原始异常**（那才是要传给调用方的信息）——
    典型症状：客户端断开后调用方收到的是 ``AttributeError`` 而不是 ``CancelledError``，
    于是上层把它当"工具失败"重试。
    """
    c = _build(tmp_path)

    class _HostileCtx:
        """任何访问都炸——模拟收尾环境本身出问题。"""
        def __getattr__(self, name):
            raise RuntimeError("ctx 不可用")

    async def s():
        await c.assemble()
        # 缺 task_id ⇒ 直接返回
        await c.root.react._abandon(_HostileCtx(), {}, asyncio.CancelledError())
        # 有 task_id 但 wm 缺失 / 通道不可用 ⇒ 尽力而为后安静返回
        await c.root.react._abandon(_HostileCtx(),
                                    {"task_id": "t", "ident": _IDENT, "wm": None},
                                    asyncio.CancelledError())
        await c.teardown()

    _run(s())


def test_body_internal_error_is_handled_by_react_not_by_wrapper(tmp_path):
    """★ 对照用例：``reasoner`` 抛错**不会**逸出（主体自己转成触顶路径）。

    这条不是测缺陷，是给"异常从哪走"划清边界：只有**取消**与**真正逸出的异常**
    才走 ``_abandon``；主体内已处理的推理失败仍走正常收尾（不重复收尾）。
    """
    c = _build(tmp_path)

    class _Boom(Exception):
        pass

    class _BadReasoner:
        async def reason(self, ctx, window, tools):
            raise _Boom("推理炸了")

    async def s():
        await c.assemble()
        c.root.react._reasoner = _BadReasoner()
        r = await c.root.react.run(c.root, "x", session_id="default")
        assert r.ok is False and r.exhausted is True
        await c.teardown()

    _run(s())
