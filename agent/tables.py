"""agent.tables —— agent_* 表的 DDL（namespace=agent · 独立库）。

所有 agent 侧表统一在此声明（storage 插件装配时建），避免各子系统各自建造成分漂移。
只含 DDL，不含任何业务实现逻辑（业务词允许——本模块在内容层，不受纯度守卫扫描）。
"""

from __future__ import annotations

_DIALECTS = ("sqlite",)


def _per(dialect: str, *stmts: str) -> dict:
    return {k: list(stmts) for k in _DIALECTS}


_EVENTS = [
    "CREATE TABLE IF NOT EXISTS agent_events ("
    " seq INTEGER PRIMARY KEY AUTOINCREMENT, event TEXT, payload TEXT, at TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_agent_events_event ON agent_events(event)",
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
    " created_at TEXT, valid_at TEXT, invalid_at TEXT, source TEXT, extra TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_agent_memories_kind ON agent_memories(kind)",
    "CREATE INDEX IF NOT EXISTS idx_agent_memories_last ON agent_memories(last_accessed_at)",
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
    " seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, at TEXT)",
    "CREATE INDEX IF NOT EXISTS idx_agent_messages_session ON agent_messages(session_id, seq)",
]


# ---- 已废弃迁移（仅登记名称，不再执行任何 DDL） ----
# 早期领域版本曾建 agent_runs / agent_scores / agent_picks / agent_candidates 四表；代码已移除，
# 但**旧库（如 data/agent_chat.db）已应用这些迁移名**。MigrationRegistry 的防脏武校验规定
# 「已应用 ∖ 当前定义 ≠ ∅ → 拒绝启动」，故此处只登记名称、语句集留空（幂等 no-op）；
# 旧表与其中数据保留在旧库中，不删。
_DEPRECATED = [
    ("agent_001_runs", _per("sqlite"), "已废弃：领域期 run 审计表（不再新建）"),
    ("agent_002_scores", _per("sqlite"), "已废弃：领域期逐候选打分表（不再新建）"),
    ("agent_003_picks", _per("sqlite"), "已废弃：领域期短名单表（不再新建）"),
    ("agent_004_candidates", _per("sqlite"), "已废弃：领域期候选池表（不再新建）"),
]


# storage 插件装配时全量应用（sqlite 幂等）
MIGRATIONS = [
    ("agent_sys_001", _per("sqlite", *_SYSTEM), "迁移登记表"),
    *_DEPRECATED,
    ("agent_005_events", _per("sqlite", *_EVENTS), "durable 事件日志（审计脊柱）"),
    ("agent_006_budget_log", _per("sqlite", *_BUDGET), "token/成本账"),
    ("agent_007_memories", _per("sqlite", *_MEMORIES), "记忆（Zep 式：事实/意图/决策/观察 + 打分字段）"),
    ("agent_008_entities", _per("sqlite", *_ENTITIES), "记忆实体"),
    ("agent_009_edges", _per("sqlite", *_EDGES), "记忆关系边"),
    ("agent_010_messages", _per("sqlite", *_MESSAGES), "会话转写（短期记忆：session_id + role/content 时序）"),
]

__all__ = ["MIGRATIONS"]