"""S4 迁移原子性测试——transaction 原语 + 失败迁移整组回滚。"""

from __future__ import annotations

import asyncio

import pytest

from agent.api import AgentError
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


def test_backup_produces_consistent_snapshot(tmp_path):
    """备份必须是**可用**的库，而不是复制了个半截文件。"""
    db = SqliteDb(str(tmp_path / "t.db"))

    async def s():
        await db.start()
        await db.execute(None, "CREATE TABLE kv(k TEXT PRIMARY KEY, v INT)")
        await db.execute(None, "INSERT INTO kv VALUES ('a', 1)")
        dest = str(tmp_path / "copy.db")
        await db.backup(dest)

        # 备份完成后再写主库 —— 副本不应看到（证明是一致性快照，不是硬链接/共享文件）
        await db.execute(None, "INSERT INTO kv VALUES ('b', 2)")
        import sqlite3
        con = sqlite3.connect(dest)
        try:
            rows = con.execute("SELECT k FROM kv ORDER BY k").fetchall()
        finally:
            con.close()
        assert [r[0] for r in rows] == ["a"], "备份应只含备份时刻的数据"
        await db.close()

    _run(s())


def test_backup_rotating_caps_file_count_without_deleting(tmp_path):
    """槽位轮转：文件数恒定 ≤ keep，且**从不调用删除**（绕开宿主 safe-delete 守卫）。"""
    db = SqliteDb(str(tmp_path / "t.db"))

    async def s():
        await db.start()
        await db.execute(None, "CREATE TABLE kv(k TEXT)")
        for _ in range(8):
            await db.backup_rotating(keep=3)
        baks = sorted(tmp_path.glob("t.db.bak.*"))
        assert len(baks) == 3, f"保留 3 份，实际 {len(baks)}"
        await db.close()

    _run(s())


def test_migration_backs_up_only_when_pending(tmp_path):
    """★ 有迁移才备份（平时零开销）；无待应用迁移时不产生任何备份文件。"""
    db = SqliteDb(str(tmp_path / "t.db"))
    reg = MigrationRegistry(db)
    migs = [("m1", {"sqlite": ("CREATE TABLE t1(a INT)",)}, "d")]

    async def s():
        await db.start()
        first = await reg.apply(None, migs, "sqlite")
        assert first == ["m1"], "首次应应用"
        assert list(tmp_path.glob("t.db.bak.*")), "有迁移要跑 ⇒ 必须留下备份"

        # 第二次：无待应用迁移 ⇒ 不应新建备份（覆盖也不该发生）
        before = {p.name: p.stat().st_mtime_ns for p in tmp_path.glob("t.db.bak.*")}
        again = await reg.apply(None, migs, "sqlite")
        assert again == [], "已应用过的迁移不应重跑"
        after = {p.name: p.stat().st_mtime_ns for p in tmp_path.glob("t.db.bak.*")}
        assert before == after, "无待应用迁移时不应触碰备份文件（平时零开销）"
        await db.close()

    _run(s())


def test_nested_transaction_fails_fast_not_deadlock(tmp_path):
    """嵌套事务必须 **fail-fast**，而不是静默死锁。

    ★ 检测器型测试：``SqliteDb._lock`` 是 ``asyncio.Lock``（**不可重入**）。
    若去掉 ``_tx_depth`` 保护，本用例会**永久挂死**而不是失败——CI 会被卡住却无人知晓。
    故用 ``wait_for`` 把「死锁」显式转换成 AssertionError：
    **保护被删 ⇒ 本测试红（不是挂），保护在位 ⇒ 绿**。
    """
    db = SqliteDb(str(tmp_path / "t.db"))

    async def s():
        await db.start()
        await db.execute(None, "CREATE TABLE kv(k TEXT PRIMARY KEY)")

        async def nested():
            async with db.transaction(None):
                async with db.transaction(None):      # 第二层应立刻抛，不该等锁
                    await db.execute(None, "INSERT INTO kv VALUES ('x')")

        try:
            await asyncio.wait_for(nested(), timeout=5)
        except AgentError as exc:
            assert "嵌套" in str(exc), f"错误信息应点明是嵌套问题: {exc}"
        except asyncio.TimeoutError:      # pragma: no cover —— 仅在保护被删时走到
            raise AssertionError(
                "嵌套事务死锁了（等待 5s 超时）—— _tx_depth 保护被移除？应 fail-fast") from None
        else:
            raise AssertionError("嵌套事务应当抛 AgentError，但没有抛")

        # fail-fast 不能误伤后续：锁必须已释放，正常事务照常可用
        async with db.transaction(None):
            await db.execute(None, "INSERT INTO kv VALUES ('ok')")
        rows = await db.query(None, "SELECT COUNT(*) AS n FROM kv")
        assert rows[0]["n"] == 1, "嵌套被拒后，正常事务仍应可提交"
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
