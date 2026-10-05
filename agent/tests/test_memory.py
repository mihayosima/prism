"""记忆插件测试（agent/plugins/memory）—— add/recall/打分衰减/LLM 提取/上下文注入/诚实拒绝
    + 目录插件语义：默认启用时装、停用不装（离线确定性）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent.api import Services
from agent.core.session import set_current_session_key
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path, *, enable: bool = True, enable_all: bool = False) -> PluginManager:
    """指向真实 agent/plugins 的管理器；期望态落在 tmp，避免污染 data/。"""
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "plugin_state.json"))
    if enable_all:
        for pid in ("plugins.memory", "plugins.short_term",
                    "plugins.work_memory", "plugins.system_memory"):
            pm.set_enabled(pid, True)
    elif enable:
        pm.set_enabled("plugins.memory", True)
    return pm


def _build(tmp_path, *, enable: bool = True, patch: dict | None = None,
           enable_all: bool = False):
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch,
                       plugin_mgr=_pm(tmp_path, enable=enable, enable_all=enable_all))


def test_plugin_is_manageable_directory_plugin():
    """记忆是「目录插件」（来源 = agent/plugins 目录）→ 插件页可启用 / 停用 / 卸载。

    可管理性由「来源」决定（delta ≠ system/builtin），与 layer 无关：
    本插件语义上是系统组件（layer=system），照样可启停卸。
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                           state_file=str(Path(d) / "state.json"))
        cands = pm.discovered()
        mem = [c for c in cands if c["name"] == "plugins.memory"]
        assert mem, "plugins.memory 未出现在插件发现列表"
        assert mem[0]["layer"] == "system"            # 系统组件（语义标签，不闸管理）
        assert mem[0]["provides"] == ["memory"]
        assert "agent_memories" in mem[0]["tables"]


def test_add_recall_and_invalid_excluded(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        await c.root.memory.add(c.root, kind="fact", content="用户偏好低库存类目")
        hits = await c.root.memory.recall(c.root, "低库存 偏好", limit=5)
        assert hits and hits[0]["content"] == "用户偏好低库存类目"
        assert hits[0]["score"] > 0.3
        # 失效（invalid_at 早于现在）→ 召回排除
        await c.root.memory.add(c.root, kind="fact", content="用户偏好高库存类目",
                                invalid_at="2000-01-01T00:00:00")
        hits2 = await c.root.memory.recall(c.root, "高库存 偏好", limit=10)
        assert not any("高库存" in h["content"] for h in hits2)
        await c.teardown()

    _run(s())


def test_add_merge_dedup_by_content(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        await c.root.memory.add(c.root, kind="fact", content="记住一条")
        await c.root.memory.add(c.root, kind="fact", content="记住一条")
        stats = await c.root.memory.stats(c.root)
        assert stats["by_kind"].get("fact", 0) == 1          # 同内容合并
        rows = await c.root.db.query(
            c.root, "SELECT accessed_count FROM agent_memories WHERE content='记住一条'")
        assert rows[0]["accessed_count"] >= 2                # 重写计数递增
        await c.teardown()

    _run(s())


def test_remember_llm_extract_and_context(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        r = await c.root.memory.remember(c.root, {"user": "我下周要重跑一次复盘"})
        assert r["ok"] is True and r["added"]               # fixture 提取出 fact
        block = await c.root.memory.context(c.root, "复盘 下周", limit=5)
        assert ("复盘" in block) or ("下周" in block)
        stats = await c.root.memory.stats(c.root)
        assert stats["total"] >= 1
        await c.teardown()

    _run(s())


def test_prune_old_low_score(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        await c.root.memory.add(c.root, kind="fact", content="很旧的记忆ZXQ")
        old = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S")
        await c.root.db.execute(
            c.root, "UPDATE agent_memories SET created_at=?, last_accessed_at=? WHERE content=?",
            (old, old, "很旧的记忆ZXQ"))
        pruned = await c.root.memory.prune(c.root, older_than_days=30, min_score=0.3)
        assert pruned >= 1                                   # 旧+低分 → 清理
        await c.teardown()

    _run(s())


def test_prune_default_threshold_is_reachable(tmp_path):
    """★ 回归：``prune`` 的**默认**阈值必须真的删得掉东西。

    修复前 ``score()`` 给空查询 ``lex=0.5`` ⇒ 空查询分数下界 = ``0.5*0.5 = 0.25``，
    恰好等于默认 ``min_score``，其余三项恒非负 ⇒ ``score < 0.25`` **永不成立**
    ⇒ 默认参数下 prune 是死代码，记忆只增不减。
    """
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        await c.root.memory.add(c.root, kind="fact", content="默认阈值下的旧记忆PRN")
        old = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S")
        await c.root.db.execute(
            c.root, "UPDATE agent_memories SET created_at=?, last_accessed_at=? WHERE content=?",
            (old, old, "默认阈值下的旧记忆PRN"))
        # 全部用默认值：older_than_days=30, min_score=0.25
        pruned = await c.root.memory.prune(c.root)
        assert pruned >= 1, "默认阈值下 prune 删不掉任何东西 ⇒ 淘汰条件不可达"
        rows = await c.root.db.query(
            c.root, "SELECT id FROM agent_memories WHERE content=?", ("默认阈值下的旧记忆PRN",))
        assert rows == []
        await c.teardown()

    _run(s())


def test_prune_keeps_fresh_memory(tmp_path):
    """反向验证：新的、高分的记忆**不该**被误删（防把阈值修过头）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        await c.root.memory.add(c.root, kind="fact", content="刚记住的新鲜事FRESH")
        pruned = await c.root.memory.prune(c.root)
        assert pruned == 0, "刚写入的记忆被误删 ⇒ 阈值过激"
        stats = await c.root.memory.stats(c.root)
        assert stats["total"] >= 1
        await c.teardown()

    _run(s())


def test_chat_round_trip_writes_memory(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        reply = await c.root.chat.send(c.root, "帮我整理上周的会议记录")
        assert reply.ok
        stats = await c.root.memory.stats(c.root)
        assert stats["total"] >= 1                            # 会话记忆（经 LLM 提取）
        hits = await c.root.memory.recall(c.root, "会议", limit=5)
        assert hits                                          # 下次可真召回
        await c.teardown()

    _run(s())


def test_remember_honest_when_llm_denied(tmp_path):
    """guard 关掉 llm → 记忆提取被拒 → 诚实 ok=False，不假记。"""
    c = _build(tmp_path, patch={"agent": {"permissions": {"llm": "deny"}}})

    async def s():
        await c.assemble()
        r = await c.root.memory.remember(c.root, {"user": "x"})
        assert r["ok"] is False
        assert "未授权" in (r.get("reason") or "")
        await c.teardown()

    _run(s())


def test_teardown_no_residue(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        await c.root.memory.add(c.root, kind="fact", content="temp")
        assert c.root.has(Services.MEMORY)
        await c.teardown()
        assert not c.root.has(Services.MEMORY)                       # 服务回收（表仍在库中，数据保留）
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 会话归属（2026-10-04）：长期记忆不再跨会话串味
# ---------------------------------------------------------------------------


def test_memory_is_isolated_between_sessions(tmp_path):
    """★ 核心回归：A 会话的记忆**不得**出现在 B 会话的召回里。

    修复前 ``agent_memories`` 无 session 列、``recall`` 不过滤任何身份 ⇒
    一个会话记住的事实会注入另一个会话的 prompt（正确性与隐私问题）。
    """
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        set_current_session_key("u:A")
        await c.root.memory.add(c.root, kind="fact", content="A会话的私有偏好XYZ")
        set_current_session_key("u:B")
        await c.root.memory.add(c.root, kind="fact", content="B会话的私有偏好QQQ")

        # B 会话只看得到自己那条（严格模式：排除无归属存量行）
        hits_b = await c.root.memory.recall(c.root, "偏好", limit=10,
                                            include_unscoped=False)
        texts_b = [h["content"] for h in hits_b]
        assert any("B会话" in t for t in texts_b)
        assert not any("A会话" in t for t in texts_b), \
            f"跨会话串味：B 召回了 A 的记忆 {texts_b}"

        set_current_session_key("u:A")
        hits_a = await c.root.memory.recall(c.root, "偏好", limit=10,
                                            include_unscoped=False)
        texts_a = [h["content"] for h in hits_a]
        assert any("A会话" in t for t in texts_a)
        assert not any("B会话" in t for t in texts_a)
        await c.teardown()

    _run(s())


def test_same_fact_in_two_sessions_not_merged(tmp_path):
    """同内容在两个会话各存一条（去重键纳入 session），不共享计数/时间衰减。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        for sk in ("u:A", "u:B"):
            set_current_session_key(sk)
            await c.root.memory.add(c.root, kind="fact", content="同一句事实MMM")
        stats = await c.root.memory.stats(c.root)
        assert stats["by_kind"].get("fact", 0) == 2, \
            "同一事实在不同会话被合并成一行 ⇒ 跨会话共享了计数与时间衰减"
        await c.teardown()

    _run(s())


def test_unscoped_memory_still_visible_and_counted(tmp_path):
    """无归属存量行仍可见（加了列也不能让老记忆凭空消失），且数量可见。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        await c.root.db.execute(
            c.root,
            "INSERT INTO agent_memories(id, kind, content, entity_ids, score_observed,"
            " last_accessed_at, accessed_count, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("mem_legacy", "fact", "存量无归属记忆LLL", "[]", 1.0,
             "2026-10-04T00:00:00", 1, "2026-10-04T00:00:00"))
        set_current_session_key("u:A")
        hits = await c.root.memory.recall(c.root, "存量", limit=10)
        assert any("存量无归属" in h["content"] for h in hits), \
            "存量无归属记忆被会话过滤掉了 ⇒ 升级即丢数据"
        stats = await c.root.memory.stats(c.root)
        assert stats["scoped"] is True
        assert stats.get("unscoped", 0) >= 1       # 待收敛量可见，不静默
        # 严格模式下则不出现（把"是否带存量"变成显式选择）
        strict = await c.root.memory.recall(c.root, "存量", limit=10,
                                            include_unscoped=False)
        assert not any("存量无归属" in h["content"] for h in strict)
        await c.teardown()

    _run(s())


def test_shared_memory_requires_explicit_opt_in(tmp_path):
    """跨会话共享必须是**显式声明**（shared=True），不再是默认路径。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        set_current_session_key("u:A")
        await c.root.memory.add(c.root, kind="fact", content="显式共享SHARED",
                                shared=True)
        set_current_session_key("u:B")
        # 共享记忆落在无归属域 ⇒ 用默认（含无归属）召回可见；
        # 与「默认泄露」的区别在于：它是**显式声明**的，且严格模式可整体排除。
        hits = await c.root.memory.recall(c.root, "共享", limit=10)
        assert any("显式共享" in h["content"] for h in hits), \
            "显式 shared=True 的记忆应跨会话可见"
        # 对照：非共享的记忆在严格模式下不跨会话（由上方隔离用例保证）
        await c.root.memory.add(c.root, kind="fact", content="B的私有PPP")
        set_current_session_key("u:C")
        strict = await c.root.memory.recall(c.root, "私有", limit=10,
                                            include_unscoped=False)
        assert not any("B的私有" in h["content"] for h in strict), \
            "未声明 shared 的记忆不应跨会话可见"
        await c.teardown()

    _run(s())


def test_disabled_memory_not_assembled_and_chat_still_works(tmp_path):
    """停用（期望态未启用）→ 不装配 ctx.memory；chat 照常可用（解耦证明）。"""
    c = _build(tmp_path, enable=False)

    async def s():
        await c.assemble()
        assert not c.root.has(Services.MEMORY)                       # 未装配
        reply = await c.root.chat.send(c.root, "帮我整理上周的会议记录")
        assert reply.ok                                       # 无记忆也不影响主链路
        rows = await c.root.db.query(c.root, "SELECT COUNT(*) AS n FROM agent_memories")
        assert rows[0]["n"] == 0                              # 停用时不写记忆
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# D1（2026-10-04）：召回不再"1 次全表读 + N 次写"
# ---------------------------------------------------------------------------

def test_recall_touches_hits_in_one_write(tmp_path):
    """★ 命中的多条记忆必须**一次**批量 touch，而不是逐条 UPDATE。

    做法：在 db 外面套一层计数代理，数 ``execute`` / ``executemany`` 的调用次数。
    这是"性能修"唯一有意义的断言方式 —— 断言结果没变是不够的。
    """
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        real = c.root.db
        calls = {"execute": 0, "executemany": 0, "rows": 0}
        # ★ 先取出**原方法**再替换：否则代理内部再调 real.execute 就是调自己 ⇒ RecursionError
        # （踩过：这是给"性能修"写断言时最容易犯的低级错）
        orig_execute, orig_many = real.execute, real.executemany

        async def _execute(ctx, sql, params=()):
            calls["execute"] += 1
            return await orig_execute(ctx, sql, params)

        async def _executemany(ctx, sql, seq):
            calls["executemany"] += 1
            calls["rows"] += len(list(seq))
            return await orig_many(ctx, sql, seq)

        real.execute = _execute
        real.executemany = _executemany

        for i in range(5):
            await c.root.memory.add(c.root, kind="fact", content=f"批量召回样本{i}XYZ")
        before = dict(calls)
        hits = await c.root.memory.recall(c.root, "批量召回样本", limit=5)
        assert len(hits) >= 3, f"应召回多条，实际 {len(hits)}"
        delta_exec = calls["execute"] - before["execute"]
        delta_many = calls["executemany"] - before["executemany"]
        # touch 必须是 executemany 且只 1 次；普通 execute 只应剩读路径
        assert delta_many == 1, f"批量 touch 应恰好 1 次 executemany，实际 {delta_many}"
        assert calls["rows"] >= 3, "executemany 应带全部命中 id"
        assert delta_exec <= 1, f"不该有逐条 UPDATE（execute 增量 {delta_exec}）"
        await c.teardown()

    _run(s())


def test_recall_candidate_cap_is_configurable_and_default_is_generous(tmp_path):
    """候选上限可配，且默认值大到"正常规模等于不截断"。"""
    from agent.plugins.memory import _RECALL_CANDIDATE_CAP
    assert _RECALL_CANDIDATE_CAP >= 1000, "默认上限过小会改变正常规模的召回结果"

    c = _build(tmp_path)

    async def s():
        await c.assemble()
        for i in range(12):
            await c.root.memory.add(c.root, kind="fact", content=f"上限测试样本{i}")
        assert c.root.memory._candidate_cap == _RECALL_CANDIDATE_CAP
        # 上限设为 3 ⇒ 候选被限；prune 不受候选上限影响（淘汰必须看全量）
        c.root.memory._candidate_cap = 3
        hits = await c.root.memory.recall(c.root, "上限测试样本", limit=10)
        assert len(hits) <= 3, f"候选上限未生效：{len(hits)}"
        assert await c.root.memory.prune(c.root) >= 0     # 不抛即可（不限候选）
        await c.teardown()

    _run(s())


def test_all_memory_plugins_report_status(tmp_path):
    """★ 四个记忆插件都必须实现 ``status()``（2026-10-04）。

    此前 memory / short_term / work_memory / system_memory **全都没有** ``status()``，
    而 guard/approval/sandbox/mcp/react 都有 ⇒ 插件页上看不到记忆层的任何运行态，
    "装配成功"与"真的在工作"无法区分。守卫防退化。
    """
    c = _build(tmp_path, enable_all=True)

    async def s():
        await c.assemble()
        base = {x["id"]: x["status"] for x in await c.plugin_statuses()}
        await c.teardown()
        return base

    base = _run(s())
    for pid in ("plugins.memory", "plugins.short_term",
                "plugins.work_memory", "plugins.system_memory"):
        st = base.get(pid)
        assert st is not None, f"{pid} 未出现在 plugin_statuses()"
        assert not (isinstance(st, dict) and "error" in st), \
            f"{pid}.status() 报错：{st.get('error')}"


def test_memory_status_reports_scope_state(tmp_path):
    """memory 的 ``status()`` 必须报**会话隔离是否真的生效**（补列失败会退化为 False）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        # ★ 必须**绑定会话键**再写：未绑定 ⇒ 记忆落"无归属"共享域 ⇒ unscoped=1
        #   （这不是缺陷，是共享域的语义；断言要跟着语义走）
        set_current_session_key("u:A")
        await c.root.memory.add(c.root, kind="fact", content="状态自述样本QQQ")
        st = {x["id"]: x["status"] for x in await c.plugin_statuses()}
        await c.teardown()
        return st.get("plugins.memory")

    st = _run(s())
    assert st and st.get("scoped") is True, f"status 未反映会话隔离状态：{st}"
    assert st.get("total", 0) >= 1
    assert st.get("unscoped") == 0, f"绑定会话后写入的记忆不该计入无归属：{st}"
    assert st.get("recall_candidate_cap")
