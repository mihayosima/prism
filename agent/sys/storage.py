"""agent.sys.storage —— agent.db（独立库）+ 迁移注册器（namespace=agent）。

只提供 execute/query/transaction/迁移执行，不承载任何业务语义；业务词只允许出现在
agent.tables（DDL）。换后端 = 换本插件 provider（现 sqlite 实装，dialect 映射留位）。
"""

from __future__ import annotations

import asyncio
import logging

from agent.api import AgentError, Services, now_iso, plugin
from agent.tables import EVENTS_EXTRA_COLUMNS, EVENTS_REQUEST_INDEX, MIGRATIONS

logger = logging.getLogger("agent.storage")


class SqliteDb:
    """通用数据访问（stdlib sqlite3 实装）：execute/query + dict 行。

    零后台线程（本地操作微秒级，事件循环内直接跑；asyncio.Lock 串行化防并发写），
    避免 aiosqlite 后台线程跨事件循环导致的进程退出悬挂 / loop-closed 竞态。
    """

    def __init__(self, path: str):
        import sqlite3

        self._sqlite3 = sqlite3
        self._path = path
        self._conn = None
        self._lock = None
        self._in_tx = False                     # S4：事务块内 execute 不自行提交

    async def start(self) -> None:
        from pathlib import Path

        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._sqlite3.connect(self._path)
        self._conn.row_factory = self._sqlite3.Row
        self._conn.isolation_level = None       # 显式事务模式：BEGIN/COMMIT 由本类掌控
        self._conn.execute("PRAGMA journal_mode=WAL")    # S7：多进程并发读写更稳
        self._conn.execute("PRAGMA busy_timeout=5000")   # S7：锁冲突等待 5s 而非立即抛错
        self._lock = asyncio.Lock()

    async def execute(self, ctx, sql: str, params: tuple = ()) -> None:
        if self._in_tx:
            self._conn.execute(sql, params or ())   # 事务块内：由 transaction 统一提交
            return
        async with self._lock:
            self._conn.execute(sql, params or ())

    async def query(self, ctx, sql: str, params: tuple = ()) -> list[dict]:
        async with self._lock:
            cur = self._conn.execute(sql, params or ())
            rows = cur.fetchall()
            cur.close()
            return [dict(r) for r in rows]

    def transaction(self, ctx):
        """S4：显式事务（异步上下文管理器；普通方法，返回 cm 而非协程）。

        ``async with db.transaction(ctx): ...``——块内多条语句同生共死：
        任一异常 → ROLLBACK，正常退出 → COMMIT。块内持锁，迁移等原子性依赖此原语。
        """
        db = self

        class _Tx:
            async def __aenter__(self):
                await db._lock.acquire()
                db._in_tx = True
                db._conn.execute("BEGIN")
                return db

            async def __aexit__(self, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        db._conn.execute("COMMIT")
                    else:
                        db._conn.execute("ROLLBACK")
                finally:
                    db._in_tx = False
                    db._lock.release()
                return False

        return _Tx()

    async def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None


class MigrationRegistry:
    """迁移注册器：路径内幂等应用 + 防脏武（已应用但代码已删 → 拒绝启动）。"""

    def __init__(self, db: SqliteDb):
        self._db = db

    async def apply(self, ctx, migrations: list[tuple], dialect: str,
                    namespace: str = "agent") -> list[str]:
        await self._db.execute(
            ctx, "CREATE TABLE IF NOT EXISTS agent_migrations(name TEXT PRIMARY KEY, applied_at TEXT)"
        )
        rows = await self._db.query(ctx, "SELECT name FROM agent_migrations")
        applied = {r["name"] for r in rows}
        defined = {m[0] for m in migrations}
        stray = sorted(applied - defined)
        if stray:
            raise AgentError(
                f"迁移已应用但当前代码不含（防脏武，勿删旧迁移）: {stray}"
            )
        ran: list[str] = []
        for name, stmts, _desc in migrations:
            if name in applied:
                continue
            # S4：每组迁移的全部语句 + 名字登记包进单个事务——
            # 中途失败 → 整组回滚（不留半套表、不留名字），下次启动可完整重放。
            async with self._db.transaction(ctx):
                for sql in stmts.get(dialect, ()):
                    await self._db.execute(ctx, sql)
                await self._db.execute(
                    ctx, "INSERT INTO agent_migrations(name, applied_at) VALUES (?,?)",
                    (name, now_iso()),
                )
            ran.append(name)
        return ran


@plugin
class Storage:
    """agent.db 独立库 + 迁移。装配即建全量表（namespace=agent）。"""

    name = "agent.storage"
    layer = "system"
    provides = [Services.DB, "migrations"]

    async def apply(self, ctx) -> None:
        db_path = (ctx.config or {}).get("db_path", "data/agent.db")
        db = SqliteDb(db_path)
        await db.start()
        ctx.provide(Services.DB, db)
        ctx.provide("migrations", MigrationRegistry(db))
        ctx.effect(lambda: db.close())
        await ctx.migrations.apply(ctx, MIGRATIONS, "sqlite", namespace="agent")
        await self._ensure_events_columns(ctx, db)

    async def _ensure_events_columns(self, ctx, db: "SqliteDb") -> None:
        """#5 可观测性：给**存量库**的 ``agent_events`` 容错补三列并建索引。

        新建库由 ``_EVENTS`` 的 DDL 直接带上；存量库走这里。**刻意不用迁移**：
        ``MigrationRegistry.apply`` 无容错、失败整组回滚，而这三列在"已补过的库"上
        会报 duplicate column ⇒ 整组回滚 ⇒ **服务起不来**（详见 ``agent/tables.py``
        中 ``EVENTS_EXTRA_COLUMNS`` 的说明）。逐列独立容错 ⇒ 单列失败不影响其余；
        全失败也只意味着这批账本缺列（``durable`` 会记 warning，不崩主链路）。
        """
        added = 0
        for col, decl in EVENTS_EXTRA_COLUMNS:
            try:
                await db.execute(
                    ctx, f"ALTER TABLE agent_events ADD COLUMN {col} {decl}", ())
                added += 1
            except Exception:                 # noqa: BLE001 —— 列已存在/表未建 ⇒ 跳过
                continue
        try:
            await db.execute(ctx, EVENTS_REQUEST_INDEX, ())
        except Exception:                     # noqa: BLE001 —— 索引失败不影响记账
            pass
        if added:
            logger.info("agent_events 已补可观测列 %s/%s + request_id 索引",
                        added, len(EVENTS_EXTRA_COLUMNS))


__all__ = ["SqliteDb", "MigrationRegistry"]