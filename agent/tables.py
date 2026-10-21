"""agent.tables —— agent_* 表的 DDL。

所有 agent 侧表统一在此声明，避免各子系统各自建造成分漂移。
只含 DDL，不含任何业务实现逻辑。
"""

from __future__ import annotations

_DIALECTS = ("sqlite",)


def _per(dialect: str, *stmts: str) -> dict:
    return {k: list(stmts) for k in _DIALECTS}


_EVENTS = [
    "CREATE TABLE IF NOT EXISTS agent_events ("
    " seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, payload TEXT, at TEXT,"
    " request_id TEXT, run_id TEXT, session TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_agent_events_event ON agent_events(event)",
    "CREATE INDEX IF NOT EXISTS idx_agent_events_request ON agent_events(request_id)",
    # 6.5：会话/run 维度查询补足索引。
    "CREATE INDEX IF NOT EXISTS idx_agent_events_session ON agent_events(session)",
    "CREATE INDEX IF NOT EXISTS idx_agent_events_run ON agent_events(run_id)",
]
_BUDGET = [
    "CREATE TABLE IF NOT EXISTS agent_budget_log ("
    " seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, model TEXT,"
    " tokens_in INTEGER, tokens_out INTEGER, est_cost REAL, at TEXT)",
]
_SYSTEM = [
    "CREATE TABLE IF NOT EXISTS agent_migrations (name TEXT PRIMARY KEY, applied_at TEXT)",
]
_MEMORIES = [
    "CREATE TABLE IF NOT EXISTS agent_memories ("
    " id TEXT PRIMARY KEY, kind TEXT, content TEXT, entity_ids TEXT,"
    " score_observed REAL, last_accessed_at TEXT, accessed_count INTEGER,"
    " created_at TEXT, valid_at TEXT, invalid_at TEXT, source TEXT, extra TEXT,"
    " session TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_agent_memories_kind ON agent_memories(kind)",
    "CREATE INDEX IF NOT EXISTS idx_agent_memories_last ON agent_memories(last_accessed_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_memories_session ON agent_memories(session)",
    # 6.5：召回按 ``session`` 过滤 + ``last_accessed_at`` 排序，
    # 单列索引各自独立无法同时满足「过滤+排序」，大表下退化为全扫+排序。补复合索引。
    "CREATE INDEX IF NOT EXISTS idx_agent_memories_session_last "
    "ON agent_memories(session, last_accessed_at)",
]
_ENTITIES = [
    "CREATE TABLE IF NOT EXISTS agent_entities ("
    " id TEXT PRIMARY KEY, kind TEXT, name TEXT, meta TEXT, created_at TEXT)",
]
_EDGES = [
    "CREATE TABLE IF NOT EXISTS agent_memory_edges ("
    " source_id TEXT, target_id TEXT, rel_type TEXT, weight REAL, at TEXT,"
    " PRIMARY KEY (source_id, target_id, rel_type))",
]
_MESSAGES = [
    "CREATE TABLE IF NOT EXISTS agent_messages ("
    " seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, at TEXT,"
    # 6.3：这两列原经迁移 agent_012 的裸 ALTER 追加；裸 ALTER 在「列已存在」的
    # 存量库上会 duplicate column ⇒ MigrationRegistry 整组回滚 ⇒ 服务起不来。改为 DDL 直接带上
    # 旧库由 short_term 插件装配期容错 ALTER 补。
    " token_count INTEGER, compressed_into INTEGER)",
    "CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_id, seq)",
]

# 短期记忆摘要表：覆盖「所有不在当前 kept 里的历史」。
# version 递增、tokens 供预算；与 agent_messages 同库。
_SUMMARIES = [
    "CREATE TABLE IF NOT EXISTS agent_session_summaries ("
    " session_id TEXT PRIMARY KEY, summary TEXT, version INTEGER DEFAULT 1,"
    " covers_up_to_seq INTEGER, tokens INTEGER, updated_at TEXT)",
]

# agent_messages 扩展列。定义收口本文件，执行由 short_term 插件装配期容错跑
# 兼容「更旧的、建表时尚无该列」的库。
MESSAGE_FIELDS = [
    ("token_count", "INTEGER"),
    ("compressed_into", "INTEGER"),
]

# agent_budget_log 的记账明细列。
# 为什么不写成上面那样的 MIGRATIONS 条目：这些列在存量库里已被早期运行时 ALTER
# 加过，而 MigrationRegistry 无容错
# ⇒ `ALTER TABLE ADD COLUMN` 会报 duplicate column ⇒ 迁移失败 ⇒ 服务起不来。
# SQLite 亦不支持 `ADD COLUMN IF NOT EXISTS`，迁移条目只接受裸 SQL，塞不进条件判断。
# 故采取：定义收口到本文件，
# 执行仍由 budget 插件在装配期以 try/except 容错跑。
# 注：`run_id` 已出现在上方 _BUDGET 的建表 DDL 中，此处保留是为了兼容「更旧的、
# 建表时尚无该列」的库——运行时补列因此也是幂等的。
BUDGET_LOG_FIELDS = [
    ("run_id", "TEXT"),
    ("cache_hit", "INTEGER"),
    ("reasoning_tokens", "INTEGER"),
    ("session", "TEXT"),
]


# ---- 已废弃迁移 ----
# 早期领域版本曾建 agent_runs / agent_scores / agent_picks / agent_candidates 四表；代码已移除，
# 但旧库已应用这些迁移名。MigrationRegistry 的防脏武校验规定
# 「已应用 ∖ 当前定义 ≠ ∅ → 拒绝启动」，故此处只登记名称、语句集留空；
# 旧表与其中数据保留在旧库中，不删。
_DEPRECATED = [
    ("agent_001_runs", _per("sqlite"), "已废弃：领域期 run 审计表（不再新建）"),
    ("agent_002_scores", _per("sqlite"), "已废弃：领域期逐候选打分表（不再新建）"),
    ("agent_003_picks", _per("sqlite"), "已废弃：领域期短名单表（不再新建）"),
    ("agent_004_candidates", _per("sqlite"), "已废弃：领域期候选池表（不再新建）"),
]


# storage 插件装配时全量应用
MIGRATIONS = [
    ("agent_sys_001", _per("sqlite", *_SYSTEM), "迁移登记表"),
    *_DEPRECATED,
    ("agent_005_events", _per("sqlite", *_EVENTS), "durable 事件日志（审计脊柱）"),
    ("agent_006_budget_log", _per("sqlite", *_BUDGET), "token/成本账"),
    ("agent_007_memories", _per("sqlite", *_MEMORIES), "记忆（Zep 式：事实/意图/决策/观察 + 打分字段）"),
    ("agent_008_entities", _per("sqlite", *_ENTITIES), "记忆实体"),
    ("agent_009_edges", _per("sqlite", *_EDGES), "记忆关系边"),
    ("agent_010_messages", _per("sqlite", *_MESSAGES), "会话转写（短期记忆：session_id + role/content 时序）"),
    ("agent_011_session_summaries", _per("sqlite", *_SUMMARIES), "短期记忆滚动摘要（covers_up_to_seq 覆盖非 kept 历史）"),
    # 6.3：原 ALTER 已下移为 short_term 插件装配期的容错补列；
    # 此处保留迁移名仅为兼容「已应用过该名」的存量库，避免防脏武校验误判 stray ⇒ 拒绝启动。
    ("agent_012_message_fields", _per("sqlite"), "agent_messages 扩 token_count + compressed_into（兼容旧库 ALTER·已下移为容错补列）"),
]

# ``agent_events`` 的补列
# 增三列：``request_id``、
#"agent_messages 扩 token_count + compressed_into"不是一条迁移：
# - 走迁移的话，新库会先由 _EVENTS 的 DDL 建出带列的表，紧接着那条 ALTER 再加一次
# ⇒ duplicate column ⇒ MigrationRegistry 整组回滚 ⇒ 服务起不来。要走迁移就必须
# 同时把列从 _EVENTS 里删掉——那样可行，但一旦出现
# 「列已存在、迁移未登记」的库，同样会炸。
# - 现在的做法：DDL 给新库、容错 ALTER 补旧库，两条路径都声明在本文件，且 ALTER 逐列独立容错 ⇒ 任何状态下都不会因重复列而启动失败。
# 代价：同一张表有两条到达同一结构的路径。
EVENTS_EXTRA_COLUMNS = [
    ("request_id", "TEXT"),
    ("run_id", "TEXT"),
    ("session", "TEXT"),
]

# 补列之后建索引。
EVENTS_REQUEST_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_agent_events_request ON agent_events(request_id)"
)

# ``agent_memories`` 的会话归属列。
# 与 ``EVENTS_EXTRA_COLUMNS`` 同一套取舍：
# ``MigrationRegistry.apply`` 无容错、失败整组回滚，而 ``ADD COLUMN`` 在已补过的库上
# 报 duplicate column ⇒ 整组回滚 ⇒ 服务起不来。故不写成迁移条目。
# 语义，必须记住，否则会误判召回结果：
# ``session``=``{user}:{session}``
# NULL / 空串 = 无归属
# 召回规则见 ``plugins/memory``：本会话优先，无归属行仍可见 —— 这样存量记忆不会
# 因为加了列而凭空消失，同时新增记忆严格按会话隔离。
# 「还有多少行没有归属」由 ``stats()['unscoped']`` 可见，不静默。
MEMORIES_EXTRA_COLUMNS = [
    ("session", "TEXT"),
]

# 补列之后建索引。
MEMORIES_SESSION_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_agent_memories_session ON agent_memories(session)"
)

__all__ = ["MIGRATIONS", "BUDGET_LOG_FIELDS", "MESSAGE_FIELDS", "EVENTS_EXTRA_COLUMNS",
           "EVENTS_REQUEST_INDEX", "MEMORIES_EXTRA_COLUMNS",
           "MEMORIES_SESSION_INDEX"]
