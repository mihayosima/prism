"""记忆插件测试（agent/plugins/memory）—— add/recall/打分衰减/LLM 提取/上下文注入/诚实拒绝
    + 目录插件语义：默认启用时装、停用不装（离线确定性）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from agent.api import Services
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path, *, enable: bool = True) -> PluginManager:
    """指向真实 agent/plugins 的管理器；期望态落在 tmp，避免污染 data/。"""
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "plugin_state.json"))
    if enable:
        pm.set_enabled("plugins.memory", True)
    return pm


def _build(tmp_path, *, enable: bool = True, patch: dict | None = None):
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch, plugin_mgr=_pm(tmp_path, enable=enable))


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
