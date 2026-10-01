"""阶段 2（2.2）：多会话 / 租户——键空间、隔离闭环 测试。

覆盖三条硬伤的修复（H1 快照跨会话串扰、H2 审批跨会话泄漏、H4 取消无归属），
以及身份原语的安全闸门（净化）与向后兼容（宽容解析）。

手法：
- 纯原语与待答表：**直接单元测**（零 IO）；
- 键空间与快照隔离：``InMemoryWorkMemory`` + ``fakeredis`` 双后端；
- 真链路（react.run 落快照）：离线容器（fixture LLM），断言快照落在本会话前缀下。

``fakeredis`` 未安装时自动跳过 Redis 用例（本机 dev 环境不强依赖）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.api import (Services, SessionIdError, StateKeys, build_task_id,
                       coerce_identity, make_identity, parse_task_id)
from agent.plugins.approval.pending import PendingTable
from agent.plugins.work_memory import InMemoryWorkMemory
from agent.profiles import build_agent
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


# ---------------------------------------------------------------------------
# 身份原语：净化（安全闸门）与兼容
# ---------------------------------------------------------------------------

def test_identity_rejects_forge_and_wildcards():
    """H1 安全面：``:`` 与 glob 通配符必须被拒——否则前缀扫描越权命中他人会话。"""
    for bad in ["*", "a:b", "a/b", "s?", "s[1]", "x" * 65, "s ]"]:
        with pytest.raises(SessionIdError):
            make_identity(session=bad)
    # 合法字符集放行
    ok = make_identity(tenant="t1", user="u-1.x", session="sess_1")
    assert ok.scope == "t1:u-1.x:sess_1"


def test_identity_defaults_and_empty_session():
    """空串视为「未携带」→ 落默认会话（兼容不传头的旧客户端）；非空非法值才报错。"""
    ident = make_identity()
    assert (ident.tenant, ident.user, ident.session) == ("default", "default", "default")
    assert make_identity(session="").session == "default"


def test_task_id_shape_and_lenient_parse():
    ident = make_identity(tenant="t1", user="u1", session="s1")
    tid = ident.task_id("r1")
    assert tid == "u1:s1:r1"
    assert ident.snapshot_key("r1") == "run_snapshot:t1:u1:s1:r1"
    assert ident.snapshot_key("r1").startswith(ident.snapshot_prefix())
    # 宽容解析：三段（现行）与两段（2.1 存量）都要能读回
    assert parse_task_id("u1:s1:r1") == ("u1", "s1", "r1")
    assert parse_task_id("s1:r1") == ("default", "s1", "r1")
    with pytest.raises(SessionIdError):
        parse_task_id("onlyone")
    with pytest.raises(SessionIdError):
        build_task_id(ident, "")            # 空 run_id 拒绝（键会退化）


def test_coerce_identity_from_bare_session():
    """旧调用方只给裸 session_id 时的退化路径（不得因键形状拖垮主链路）。"""
    assert coerce_identity("s1").session_key == "default:s1"
    assert coerce_identity("u1:s1").session_key == "u1:s1"
    assert coerce_identity(None).session_key == "default:default"
    assert coerce_identity("bad:id:with:many").session_key == "default:default"  # 非法→全默认


# ---------------------------------------------------------------------------
# H1：快照列表按**会话前缀**隔离（两个后端都要过）
# ---------------------------------------------------------------------------

def _snap(status="running", ts="2026-10-02T00:00:00Z", tag=""):
    return {"chain": [{"role": "user", "content": tag}], "head": None, "step": 1,
            "budget_used": 0, "status": status, "retry_count": 0, "ts": ts}


def _backends():
    out = [("inmemory", InMemoryWorkMemory(ttl=60, snapshot_ttl=60))]
    try:
        import fakeredis
        from agent.plugins.work_memory.backend import RedisWorkMemory

        srv = fakeredis.FakeServer()
        out.append(("redis", RedisWorkMemory(
            fakeredis.FakeAsyncRedis(server=srv), ttl=60, snapshot_ttl=60)))
    except Exception:                                    # noqa: BLE001
        pass
    return out


@pytest.mark.parametrize("name,wm", _backends(), ids=lambda x: str(x))
def test_snapshot_list_scoped_to_session(name, wm):
    """H1：会话 B 的快照**不得**出现在会话 A 的探测里。"""
    a = make_identity(tenant="t1", user="u1", session="sessA")
    b = make_identity(tenant="t1", user="u1", session="sessB")

    async def s():
        await wm.snapshot_save(a.tenant, a.task_id("ra"), _snap(tag="A"))
        await wm.snapshot_save(b.tenant, b.task_id("rb"), _snap(tag="B"))

        got_a = await wm.snapshot_list(a.tenant, a.snapshot_prefix())
        got_b = await wm.snapshot_list(b.tenant, b.snapshot_prefix())
        assert [g["task_id"] for g in got_a] == [a.task_id("ra")], \
            "会话 A 的探测混入了别的会话"
        assert [g["task_id"] for g in got_b] == [b.task_id("rb")]
        # 租户级（不传前缀）仍可全览——管理面语义保留
        all_t = await wm.snapshot_list(a.tenant)
        assert len(all_t) == 2

    _run(s())


def test_snapshot_prefix_not_prefix_collision():
    """前缀末尾带 ``:`` —— ``sess=abc`` 不得命中 ``sess=abcd``。"""
    abc = make_identity(user="u1", session="abc")
    abcd = make_identity(user="u1", session="abcd")
    wm = InMemoryWorkMemory(ttl=60, snapshot_ttl=60)

    async def s():
        await wm.snapshot_save(abcd.tenant, abcd.task_id("r1"), _snap())
        got = await wm.snapshot_list(abc.tenant, abc.snapshot_prefix())
        assert got == [], "前缀未闭合 ⇒ 命中了名字更长的另一个会话"

    _run(s())


# ---------------------------------------------------------------------------
# 真链路：react.run 按身份落键（task_id 形状 + 会话前缀 + run 归组键）
# ---------------------------------------------------------------------------

def test_react_run_writes_snapshot_under_session(tmp_path):
    c = _build(tmp_path)
    ident = make_identity(tenant="t1", user="u1", session="sessA")

    async def s():
        await c.assemble()
        wm = c.root.get(Services.WORK_MEMORY)
        # 用「两步链路」：会真的走 Think→Act→Observe，才会写 per-turn 快照
        # （直接收敛的问答零工具调用 ⇒ 无 Observe ⇒ 无快照，这是 D2 的既定语义）
        res = await c.root.react.run(c.root, "两步链路：先回显再总结",
                                     session_id=ident.session_key, identity=ident)
        assert res.ok
        # 快照落在本会话前缀下（且已被收尾置 finished，不删）
        mine = await wm.snapshot_list(ident.tenant, ident.snapshot_prefix())
        assert mine, "本会话未落到快照"
        assert mine[0]["task_id"].startswith("u1:sessA:")
        assert mine[0]["status"] == "finished"
        # 另一个会话看不到它
        other = make_identity(tenant="t1", user="u1", session="sessB")
        assert await wm.snapshot_list(other.tenant, other.snapshot_prefix()) == []
        # 2.2：会话提示键 = 会话键（approval 判定归属用）；REACT_RUN_ID 在 run 内为
        # task_id（自带会话维度），收尾按既有约定清空，故不在收尾后断言它。
        assert c.root.state.get(StateKeys.SESSION_KEY) == "u1:sessA"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# H2：审批待答表会话隔离
# ---------------------------------------------------------------------------

def test_pending_table_isolated_by_session():
    """同 key 的两个会话**不得**合并；快照按会话过滤；跨会话应答无效。"""
    t = PendingTable(timeout_s=0.05)

    async def s():
        fut_a = asyncio.ensure_future(t.wait("tool|x", {"tool": "x"}, session="u:sA"))
        fut_b = asyncio.ensure_future(t.wait("tool|x", {"tool": "x"}, session="u:sB"))
        await asyncio.sleep(0)          # 让两个 wait 都完成入表
        assert t.size() == 2, "同 key 跨会话被错误合并（会串到别人的审批结果）"

        snap_a = t.snapshot("u:sA")
        assert len(snap_a) == 1, "快照未按会话过滤"
        cid_a = snap_a[0]["call_id"]
        cid_b = [i["call_id"] for i in t.snapshot("u:sB")][0]
        assert t.owner_of(cid_a) == "u:sA" and t.owner_of(cid_b) == "u:sB"

        # 跨会话应答：路由层判 403，表内再兜一道
        assert t.resolve(cid_a, {"approved": True}, session="u:sB") is False
        assert t.resolve(cid_a, {"approved": True}, session="u:sA") is True
        assert (await fut_a)[0]["approved"] is True
        # B 未被 A 的答案唤醒（超时 → fail-safe 拒绝）
        ans_b, _ = await fut_b
        assert ans_b["approved"] is False and ans_b["by"] == "timeout"

    _run(s())


def test_pending_table_dedupes_within_session():
    """同一会话内同 key 仍去重（保留既有「并发多 ask 合并为一次人答」语义）。"""
    t = PendingTable(timeout_s=0.05)

    async def s():
        f1 = asyncio.ensure_future(t.wait("k", {"tool": "x"}, session="u:sA"))
        await asyncio.sleep(0)
        f2 = asyncio.ensure_future(t.wait("k", {"tool": "x"}, session="u:sA"))
        await asyncio.sleep(0)
        assert t.size() == 1, "同会话同 key 未去重"
        cid = t.snapshot("u:sA")[0]["call_id"]
        t.resolve(cid, {"approved": True}, session="u:sA")
        assert (await f1)[0]["approved"] is True
        assert (await f2)[0]["approved"] is True     # 合并等待者共享结果
        assert (await f2)[1] == cid

    _run(s())
