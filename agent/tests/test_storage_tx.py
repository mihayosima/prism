"""S4 迁移原子性测试——transaction 原语 + 失败迁移整组回滚。"""

from __future__ import annotations

import asyncio

import pytest

from agent.sys.storage import MigrationRegistry, SqliteDb


def _run(coro):
    return asyncio.run(coro)


def test_transaction_commit_and_rollback(tmp_path):
    db = SqliteDb(str(tmp_path / "t.db"))

    async def s():
        await db.start()
        await db.execute(None, "CREATE TABLE kv(k TEXT PRIMARY KEY, v INT)")
        # 正常路径：块内多条语句一起提交
        async with db.transaction(None):
            await db.execute(None, "INSERT INTO kv VALUES ('a', 1)")
            await db.execute(None, "INSERT INTO kv VALUES ('b', 2)")
        rows = await db.query(None, "SELECT COUNT(*) AS n FROM kv")
        assert rows[0]["n"] == 2
        # 失败路径：任一异常 → 整组回滚
        with pytest.raises(ValueError):
            async with db.transaction(None):
                await db.execute(None, "INSERT INTO kv VALUES ('c', 3)")
                raise ValueError("boom")
        rows = await db.query(None, "SELECT COUNT(*) AS n FROM kv")
        assert rows[0]["n"] == 2, "异常应回滚块内全部写入"
        await db.close()

    _run(s())


def test_failed_migration_leaves_no_partial_state(tmp_path):
    """中途失败的迁移：不留半套表、不登记名字，修复后可完整重放。"""
    db = SqliteDb(str(tmp_path / "t.db"))

    async def s():
        await db.start()
        reg = MigrationRegistry(db)
        bad = [("m1", {"sqlite": ("CREATE TABLE t1(a INT)", "THIS IS NOT SQL")}, "d")]
        with pytest.raises(Exception):
            await reg.apply(None, bad, "sqlite")
        tables = await db.query(
            None, "SELECT name FROM sqlite_master WHERE type='table' AND name='t1'")
        assert tables == [], "不应残留半套表"
        logged = await db.query(None, "SELECT name FROM agent_migrations")
        assert logged == [], "失败迁移不应登记名字"
        good = [("m1", {"sqlite": ("CREATE TABLE t1(a INT)",)}, "d")]
        await reg.apply(None, good, "sqlite")     # 完整重放成功
        tables = await db.query(
            None, "SELECT name FROM sqlite_master WHERE type='table' AND name='t1'")
        assert tables, "修复后应可完整重放"
        await db.close()

    _run(s())
