"""agent.sys.storage —— agent.db+ 迁移注册器。

只提供 execute/query/transaction/迁移执行，不承载任何业务语义；业务词只允许出现在
agent.tables。换后端 = 换本插件 provider。
"""

from __future__ import annotations

import asyncio
import logging
import threading

from agent.api import DEFAULT_DB_PATH, AgentError, Services, inc_counter, now_iso, plugin
from agent.tables import EVENTS_EXTRA_COLUMNS, EVENTS_REQUEST_INDEX, MIGRATIONS

logger = logging.getLogger("agent.storage")

# : 迁移前自动备份的保留份数
BACKUP_KEEP = 5


def _backup_slot(path: str, keep: int = BACKUP_KEEP) -> str:
    """挑一个备份槽位：``keep`` 个槽中最老的那个。

     刻意用固定槽位覆盖而不是「新建 + 删最老」：本项目的宿主环境对文件删除
     挂了守卫，``os.remove`` / ``Path.unlink`` 会直接抛异常。
     覆盖写不需要删除，且磁盘占用恒定为 ``keep`` 份，行为更可预测。
     """
    import os

    slots = [f"{path}.bak.{i}" for i in range(1, max(1, keep) + 1)]
    free = [s for s in slots if not os.path.exists(s)]
    if free:
        return free[0]
    return min(slots, key=lambda s: os.path.getmtime(s))


class SqliteDb:
    """通用数据访问：execute/query + dict 行。

     同步 sqlite3 调用统一经 ``asyncio.to_thread`` 卸载到后台线程，不再阻塞事件循环；
     否则 await 期间的锁等待与长事务会冻结整个进程的全部并发请求，即「假异步」。

     单连接 + 双重锁：
     - ``_lock``：协程级串行化，保证一回合 DB 操作不被其它协程的提交插空；
     - ``_tlock``：线程级串行化，真正保护 sqlite 连接。``check_same_thread=False`` 因此必须开启。
     """

    def __init__(self, path: str):
        import sqlite3

        self._sqlite3 = sqlite3
        self._path = path
        self._conn = None
        self._lock = None
        self._tlock = None
        self._in_tx = False                     # S4：事务块内 execute 不自行提交
        self._tx_owner = None                   # 见 transaction()：把嵌套死锁变成明确报错

    # ---- 同步原语：在线程池 worker 上执行，且全程持有 ``_tlock`` 保护连接 ----
    def _execute(self, sql: str, params: tuple) -> None:
        with self._tlock:
            self._conn.execute(sql, params or ())

    def _query(self, sql: str, params: tuple) -> list[dict]:
        with self._tlock:
            cur = self._conn.execute(sql, params or ())
            rows = cur.fetchall()
            cur.close()
            return [dict(r) for r in rows]

    def _executemany(self, sql: str, rows: list[tuple]) -> int:
        with self._tlock:
            cur = self._conn.executemany(sql, rows)
            return int(cur.rowcount or 0)

    async def start(self) -> None:
        from pathlib import Path

        await asyncio.to_thread(Path(self._path).parent.mkdir, parents=True, exist_ok=True)
        # check_same_thread=False：``to_thread`` 会把调用派发到线程池 worker，
        # 必须放开线程亲和，由 ``_tlock`` 保证同一时刻只有一个线程触碰连接。
        conn = await asyncio.to_thread(
            self._sqlite3.connect, self._path, check_same_thread=False)
        conn.row_factory = self._sqlite3.Row
        conn.isolation_level = None       # 显式事务模式：BEGIN/COMMIT 由本类掌控
        self._conn = conn
        await asyncio.to_thread(conn.execute, "PRAGMA journal_mode=WAL")    # S7：多进程并发读写更稳
        await asyncio.to_thread(conn.execute, "PRAGMA busy_timeout=5000")  # S7：锁冲突等待 5s 而非立即抛错
        self._lock = asyncio.Lock()
        self._tlock = threading.Lock()

    async def execute(self, ctx, sql: str, params: tuple = ()) -> None:
        if self._in_tx:
            await asyncio.to_thread(self._execute, sql, params)   # 事务块内：由 transaction 统一提交
            return
        async with self._lock:
            await asyncio.to_thread(self._execute, sql, params)

    async def query(self, ctx, sql: str, params: tuple = ()) -> list[dict]:
        if self._in_tx:
            return await asyncio.to_thread(self._query, sql, params)
        async with self._lock:
            return await asyncio.to_thread(self._query, sql, params)

    async def executemany(self, ctx, sql: str, seq: list[tuple]) -> int:
        """批量执行同一条语句，返回受影响行数。

                 为记忆召回的 ``touch`` 补的：改前 top-8 逐条 UPDATE，
                 改后 1 次。不需要拼 ``IN (...)`` ⇒ 无字符串注入面，也不撞 SQLite 变量上限。

                 事务语义与 :meth:`execute` 完全一致。
                 """
        rows = list(seq or ())
        if not rows:
            return 0
        if self._in_tx:
            return await asyncio.to_thread(self._executemany, sql, rows)
        async with self._lock:
            return await asyncio.to_thread(self._executemany, sql, rows)

    def transaction(self, ctx):
        """S4：显式事务。

         ``async with db.transaction(ctx): ...``——块内多条语句同生共死：
         任一异常 → ROLLBACK，正常退出 → COMMIT。块内持锁，迁移等原子性依赖此原语。
         """
        db = self

        class _Tx:
            async def __aenter__(self):
                # 嵌套保护：``_lock`` 是 asyncio.Lock，不可重入。
                # 事务块内再开一个事务 ⇒ 协程等自己持有的锁 ⇒ 永久静默死锁
                # 此处主动识别并 fail-fast。
                # 两个易错点：
                # 1) 检查必须在 ``acquire()`` 之前——死锁就发生在 acquire 那一句，
                # 放在它后面永远走不到；
                # 2) 判定必须用 task 归属而非"锁是否被占"——后者会把"另一个协程
                # 正在事务中、本协程正常排队"误判成嵌套而报错。
                me = asyncio.current_task()
                if db._tx_owner is me:
                    raise AgentError(
                        "不支持嵌套事务：SqliteDb 的锁不可重入，嵌套会立即死锁。"
                        "请把内层的语句并入外层事务块。")
                await db._lock.acquire()
                db._tx_owner = me
                db._in_tx = True
                await asyncio.to_thread(db._execute, "BEGIN", ())
                return db

            async def __aexit__(self, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        await asyncio.to_thread(db._execute, "COMMIT", ())
                    else:
                        await asyncio.to_thread(db._execute, "ROLLBACK", ())
                finally:
                    db._tx_owner = None
                    db._in_tx = False
                    db._lock.release()
                return False

        return _Tx()

    async def backup(self, dest: str) -> str:
        """在线备份到 ``dest``。

                 为什么用它而不是拷贝文件：WAL 模式下直接复制 ``.db`` 会拿到不一致的快照。``Connection.backup`` 在
                 SQLite 内部取一致性视图，不需要停服，也不依赖 WAL 是否已 checkpoint。

                 全程持 ``_tlock``：与业务读写串行化，备份期间连接不被别的线程触碰。
                 """
        def _do() -> None:
            if self._conn is None:
                raise AgentError("数据库未启动，无法备份")
            target = self._sqlite3.connect(dest)
            try:
                with self._tlock:
                    self._conn.backup(target)
            finally:
                target.close()

        await asyncio.to_thread(_do)
        return dest

    async def backup_rotating(self, keep: int = BACKUP_KEEP) -> str:
        """备份到轮转槽位，返回实际写入的路径。

         槽位策略见 :func:`_backup_slot` —— 只覆盖、从不删除。
         """
        slot = await asyncio.to_thread(_backup_slot, self._path, keep)
        return await self.backup(slot)

    async def close(self) -> None:
        # WAL 无需手工 checkpoint：sqlite3 在最后一个连接关闭时会自动 checkpoint
        # 并删除 ``-wal`` 文件。长驻进程运行期看到的大 WAL 是 ``wal_autocheckpoint``
        # 的稳态，不是泄漏——故此处刻意不加 PRAGMA。
        conn = self._conn
        if conn is not None:
            self._conn = None
            try:
                await asyncio.to_thread(conn.close)
            except Exception:  # noqa: BLE001
                pass


class MigrationRegistry:
    """迁移注册器：路径内幂等应用 + 防脏武。"""

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
        # 迁移前自动备份：只在真有迁移要跑时触发。
        # 为什么用备份而不是实现 ``down``：
        # - down 迁移要为每个迁移手写反向 SQL，写错就是数据损坏，而它几乎从不被执行
        # ⇒ 收益极低、风险极高；
        # - 备份是无条件正确的回退手段，且能覆盖 down 覆盖不到的情况
        # 失败不外泄：备份只是保险，失败不应让服务起不来——
        # 但要可见，否则"以为有备份其实没有"。
        pending = [m[0] for m in migrations if m[0] not in applied]
        if pending:
            try:
                dest = await self._db.backup_rotating()
                logger.info("迁移前已备份 %s ⇒ %s（待应用 %d 组）",
                            self._db._path, dest, len(pending))
            except Exception as exc:                  # noqa: BLE001
                inc_counter("agent_db_backup_failed_total")
                logger.warning("迁移前备份失败（迁移仍将继续，但无回退点）: %s", exc)

        ran: list[str] = []
        for name, stmts, _desc in migrations:
            if name in applied:
                continue
            # 每组迁移的全部语句 + 名字登记包进单个事务——
            # 中途失败 → 整组回滚，下次启动可完整重放。
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
    """agent.db 独立库 + 迁移。装配即建全量表。"""

    name = "agent.storage"
    layer = "system"
    provides = [Services.DB, "migrations"]

    async def apply(self, ctx) -> None:
        db_path = (ctx.config or {}).get("db_path", DEFAULT_DB_PATH)
        db = SqliteDb(db_path)
        await db.start()
        ctx.provide(Services.DB, db)
        ctx.provide("migrations", MigrationRegistry(db))
        ctx.effect(lambda: db.close())
        await ctx.migrations.apply(ctx, MIGRATIONS, "sqlite", namespace="agent")
        await self._ensure_events_columns(ctx, db)

    async def _ensure_events_columns(self, ctx, db: "SqliteDb") -> None:
        """#5 可观测性：给存量库的 ``agent_events`` 容错补三列并建索引。

         新建库由 ``_EVENTS`` 的 DDL 直接带上；存量库走这里。刻意不用迁移：
         ``MigrationRegistry.apply`` 无容错、失败整组回滚，而这三列在"已补过的库"上
         会报 duplicate column ⇒ 整组回滚 ⇒ 服务起不来。逐列独立容错 ⇒ 单列失败不影响其余；
         全失败也只意味着这批账本缺列。

         失败必须可见：原实现是 ``except: continue`` 全静默，
         补列失败时表现为"``durable`` 写的关联列莫名其妙是空的"，排查时日志里什么都没有，
         只能靠手工 ``PRAGMA table_info`` 才发现。故真失败记 ``warning`` + 计数；
         ``duplicate column`` 属正常幂等路径，不算失败，否则每次启动都误报。
         """
        added, failed = 0, []
        for col, decl in EVENTS_EXTRA_COLUMNS:
            try:
                await db.execute(
                    ctx, f"ALTER TABLE agent_events ADD COLUMN {col} {decl}", ())
                added += 1
            except Exception as exc:             # noqa: BLE001 —— 列已存在/表未建 ⇒ 跳过
                if "duplicate column" not in str(exc).lower():
                    failed.append((col, str(exc)))
        try:
            await db.execute(ctx, EVENTS_REQUEST_INDEX, ())
        except Exception as exc:                 # noqa: BLE001 —— 索引失败不影响记账
            logger.warning("agent_events request_id 索引创建失败（审计仍可写，查询会慢）: %s", exc)
        # 6.5：会话/run 维度索引。
        for _idx in (
            "CREATE INDEX IF NOT EXISTS idx_agent_events_session ON agent_events(session)",
            "CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id)",
        ):
            try:
                await db.execute(ctx, _idx, ())
            except Exception as exc:             # noqa: BLE001
                logger.warning("agent_events 索引创建失败（查询会慢）: %s", exc)
        if failed:
            inc_counter("agent_events_column_backfill_failed_total")
            logger.warning("agent_events 补列失败 %s/%s ⇒ 关联列将缺失（durable 会写空串）: %s",
                           len(failed), len(EVENTS_EXTRA_COLUMNS), failed)
        if added:
            logger.info("agent_events 已补可观测列 %s/%s + request_id 索引",
                        added, len(EVENTS_EXTRA_COLUMNS))


__all__ = ["SqliteDb", "MigrationRegistry"]
