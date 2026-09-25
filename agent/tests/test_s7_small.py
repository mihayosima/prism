"""S7 小修打包测试——post-execute 增益、状态文件损坏告警、WAL、有界进度队列。"""

from __future__ import annotations

import asyncio

import pytest

from agent.api import Events
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager
from agent.sys.progress import _pusher
from agent.sys.storage import SqliteDb


def _run(coro):
    return asyncio.run(coro)


def test_tasks_post_execute_payload_enriched(tmp_path):
    """TASKS_POST_EXECUTE 带 ok/span_ms/error；失败路径也发事件。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")})

    async def s():
        await c.assemble()
        seen: list[dict] = []
        c.root.on(Events.TASKS_POST_EXECUTE, lambda p: seen.append(p))

        async def ok_tool():
            return {"v": 1}

        async def bad_tool():
            raise RuntimeError("炸了")

        c.root.tasks.register(c.root, "t.ok", schema={}, handler=ok_tool, owner=None)
        c.root.tasks.register(c.root, "t.bad", schema={}, handler=bad_tool, owner=None)

        await c.root.tasks.invoke(c.root, "t.ok", caller="t")
        p = seen[-1]
        assert p["task"] == "t.ok" and p["ok"] is True
        assert p["error"] is None and isinstance(p["span_ms"], int)

        with pytest.raises(RuntimeError):
            await c.root.tasks.invoke(c.root, "t.bad", caller="t")
        p = seen[-1]
        assert p["task"] == "t.bad" and p["ok"] is False and "炸了" in (p["error"] or "")
        await c.teardown()

    _run(s())


def test_corrupt_state_file_backed_up_and_warned(tmp_path, caplog):
    """状态文件损坏：备份为 .corrupt 并告警，不再静默清空。"""
    state = tmp_path / "state.json"
    state.write_text("{not json", encoding="utf-8")
    pm = PluginManager(tmp_path / "plugins", state_file=state)
    assert pm.enabled() == []
    assert state.with_suffix(".corrupt").exists(), "坏文件应被备份"
    assert not state.exists() or state.read_text(encoding="utf-8") != "{not json"


def test_sqlite_wal_mode(tmp_path):
    db = SqliteDb(str(tmp_path / "t.db"))

    async def s():
        await db.start()
        rows = await db.query(None, "PRAGMA journal_mode")
        assert rows[0]["journal_mode"] == "wal"
        await db.close()

    _run(s())


def test_progress_bounded_queue_drops_oldest():
    """有界队列满 → 丢最旧保最新，计数落在 queue.dropped。"""
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    push = _pusher(q, "agent/llm/token")
    push({"i": 1})
    push({"i": 2})
    push({"i": 3})                       # 满 → 丢 {"i": 1}
    assert q.qsize() == 2
    assert q.get_nowait()["payload"]["data"]["i"] == 2
    assert q.get_nowait()["payload"]["data"]["i"] == 3
    assert getattr(q, "dropped", 0) == 1
