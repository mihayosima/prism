"""agent.sys.storage —— agent.db（独立库）+ 迁移注册器（namespace=agent）。

只提供 execute/query/transaction/迁移执行，不承载任何业务语义；业务词只允许出现在
agent.tables（DDL）。换后端 = 换本插件 provider（现 sqlite 实装，dialect 映射留位）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from agent.api import AgentError, Services, plugin
from agent.tables import MIGRATIONS

logger = logging.getLogger("agent.storage")


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


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

    async def start(self) -> None:
        from pathlib import Path

        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._sqlite3.connect(self._path)
        self._conn.row_factory = self._sqlite3.Row
        self._lock = asyncio.Lock()

    async def execute(self, ctx, sql: str, params: tuple = ()) -> None:
        async with self._lock:
            with self._conn:                          # with 连接 = 提交/回滚
                self._conn.execute(sql, params or ())

    async def query(self, ctx, sql: str, params: tuple = ()) -> list[dict]:
        async with self._lock:
            cur = self._conn.execute(sql, params or ())
            rows = cur.fetchall()
            cur.close()
            return [dict(r) for r in rows]

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
            for sql in stmts.get(dialect, ()):
                await self._db.execute(ctx, sql)
            await self._db.execute(
                ctx, "INSERT INTO agent_migrations(name, applied_at) VALUES (?,?)",
                (name, _now_iso()),
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


__all__ = ["SqliteDb", "MigrationRegistry"]