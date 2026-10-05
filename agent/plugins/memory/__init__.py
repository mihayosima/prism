"""插件包：memory —— 记忆子系统（参考 Zep · 折叠为单一 `plugins.memory` 插件）。

管理：本包位于 agent/plugins/，经「插件」页可停用 / 启用 / 卸载（引擎式 desired/actual/delta）。
语义上是**系统组件**（layer=system，与 guard/llm 同级）；放进 plugins 目录只是采用
「目录插件」这一生命周期形态 —— 来源决定能否启停卸，layer 只是标签，二者正交。
已启用（依据 data/agent_plugin_state.json；**代码级默认是 opt-in** —— 该文件缺失时全部目录插件停用）；停用后 ctx.has("memory") 为 False，chat 自动跳过记忆。

把「时序事实 + 打分衰减 + 上下文注入」压成一个自洽插件：
  表：agent_memories / agent_entities / agent_memory_edges（namespace agent_，DDL 在 agent/tables.py 由 Storage 建）
  动作：remember（LLM 提取，走 ctx.llm → guard/budget 照生效）· add（手动/决策直写）
        recall（词面×新鲜度×重访 打分排序）· context（组装注入块）· stats / prune / wipe
  事件：durable agent/memory/remembered · agent/memory/recall
  提醒：零特权——提取每次都经 ctx.llm；写库只经 ctx.db。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import datetime
from dataclasses import dataclass
from typing import Any, Protocol

from agent.api import (
    CapabilityNotImplemented,
    Events,
    MARK_MEMORY_EXTRACT,
    Services,
    current_session_key,
    durable,
    inc_counter,
    now_iso,
    plugin,
)
from agent.tables import MEMORIES_EXTRA_COLUMNS, MEMORIES_SESSION_INDEX

logger = logging.getLogger("plugins.memory")

KINDS = ("fact", "intention", "decision", "observation")

# ---- 提取结构化契约（走 ctx.llm；guard/budget 照生效） ----

MARK_BASE = MARK_MEMORY_EXTRACT

#: 基础提取契约（事实 + 意图）—— **默认只要这两项**。
BASE_EXTRACT_SCHEMA: dict = {"facts": {"type": "array"}, "intentions": {"type": "array"}}
#: 实体/关系：**仅在有人接的时候才要求模型输出**（见 ``MemoryEntitySinkLike``）。
ENTITY_EXTRACT_SCHEMA: dict = {"entities": {"type": "array"}, "relations": {"type": "array"}}
#: 完整契约（兼容旧引用；内置提取器按是否挂了 sink 决定用哪一份）。
EXTRACT_SCHEMA: dict = {**BASE_EXTRACT_SCHEMA, **ENTITY_EXTRACT_SCHEMA}

_BASE_EXTRACT_BODY = (
    '{"facts": ["短句持久事实"], "intentions": [{"text": "用户/任务意图"}]}'
)
_ENTITY_EXTRACT_BODY = (
    ', "entities": [{"name": "...", "kind": "person|org|topic"}],'
    ' "relations": [{"source": "...", "rel": "...", "target": "..."}]}'
)

#: 完整版提示词（含实体/关系；兼容旧引用）
EXTRACT_SYSTEM = MARK_MEMORY_EXTRACT + "\n" + (
    "你是记忆提取器。从给定的对话/运行载荷中提取新信息，只输出 JSON：\n"
    + _BASE_EXTRACT_BODY[:-1] + _ENTITY_EXTRACT_BODY
)


def build_extract_system(want_entities: bool = False) -> str:
    """按是否需要实体来组装提示词。

    ★ **默认不要求 entities/relations**（2026-10-04）：
    修复前提示词**总是**要求模型输出这两项，而 ``remember`` 只遍历 facts/intentions
    ⇒ 它们被**原样丢弃**（``agent_entities`` / ``agent_memory_edges`` 两张表全仓
    **零 INSERT**）。代价是**每轮一次 LLM 调用白输出两项**——直接计入 token 成本。
    现在：没人接 ⇒ 不要求 ⇒ 省钱且诚实；挂了 sink ⇒ 自动要求并落库。
    """
    body = _BASE_EXTRACT_BODY if not want_entities else (
        _BASE_EXTRACT_BODY[:-1] + _ENTITY_EXTRACT_BODY)
    return MARK_MEMORY_EXTRACT + "\n" + (
        "你是记忆提取器。从给定的对话/运行载荷中提取新信息，只输出 JSON：\n" + body)

_WORD = re.compile(r"[\w一-鿿]+")


def _tokenize(text: str) -> set[str]:
    return {t for t in _WORD.findall(text.lower()) if len(t) >= 2}


def _days_between(a: str, b: str) -> float:
    try:
        return max(0.0, (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400.0)
    except Exception:  # noqa: BLE001 —— 坏时间戳按 0
        return 0.0


#: 召回候选上限（2026-10-04 性能护栏）：按 ``last_accessed_at`` 倒序在 SQL 侧限量。
#: 取值刻意**大** ⇒ 正常规模下等于不截断（行为与优化前逐条一致），
#: 只在记忆量涨到上万条后才自动收敛成本。可经 ``agent.memory.recall_candidate_cap``
#: 覆盖（``<=0`` ⇒ 不限量，回到全表扫描）。
_RECALL_CANDIDATE_CAP = 2000


def _mem_id(content: str, session: str = "") -> str:
    """记忆 id = sha1(session + content) 前 16 位。

    ★ **纳入 ``session`` 是有意的**（2026-10-04 会话化）：若只按 content 哈希，
    同一句事实在 A/B 两个会话会被**合并成一行** ⇒ 去重键反过来成了串味通道
    （``accessed_count`` / ``last_accessed_at`` 跨会话共享，时间衰减互相污染）。
    纳入会话后，每个会话各存一条，隔离才成立。同会话内的同内容仍会合并（去重语义保留）。
    """
    raw = f"{session or ''}\x00{content}"
    return "mem_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _ent_id(name: str) -> str:
    return "ent_" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class MemoryItem:
    id: str
    kind: str
    content: str
    entity_ids: list[str]
    score_observed: float
    created_at: str
    last_accessed_at: str
    accessed_count: int
    valid_at: str | None = None
    invalid_at: str | None = None
    source: str | None = None
    #: 会话归属 ``{user}:{session}``（``core/session.py`` 同源）。
    #: **空串 = 无归属**（存量行 / 显式跨会话共享），召回时仍可见（详见 ``MemoryStore.valid``）。
    session: str = ""
    extra: dict = None  # type: ignore[assignment]

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "content": self.content,
                "entity_ids": list(self.entity_ids), "score_observed": self.score_observed,
                "created_at": self.created_at, "last_accessed_at": self.last_accessed_at,
                "accessed_count": self.accessed_count, "valid_at": self.valid_at,
                "invalid_at": self.invalid_at, "source": self.source,
                "session": self.session, "extra": self.extra or {}}


def _from_row(r: dict) -> MemoryItem:
    return MemoryItem(id=r["id"], kind=r["kind"], content=r["content"],
                  entity_ids=json.loads(r["entity_ids"] or "[]"),
                  score_observed=float(r["score_observed"] or 0.0),
                  created_at=r["created_at"], last_accessed_at=r["last_accessed_at"],
                  accessed_count=int(r["accessed_count"] or 0),
                  valid_at=r.get("valid_at"), invalid_at=r.get("invalid_at"),
                  source=r.get("source"), session=r.get("session") or "",
                  extra=json.loads(r["extra"]) if r.get("extra") else {})


# ---------------------------------------------------------------------------
# Store / Extractor / Search（可换 seam）
# ---------------------------------------------------------------------------


class MemoryStoreLike(Protocol):
    """持久化组件契约（D2 固化；Driver 消费面：upsert/touch/valid/delete）。"""

    async def upsert(self, ctx, m: "MemoryItem") -> None: ...

    async def touch(self, ctx, m: "MemoryItem") -> None: ...

    async def valid(self, ctx) -> list["MemoryItem"]: ...

    async def delete(self, ctx, mid: str) -> None: ...


class MemoryExtractorLike(Protocol):
    """提取组件契约：payload → {facts, intentions, entities, relations}（结构走 schema）。"""

    async def extract(self, ctx, payload: dict) -> dict: ...


class MemorySearchLike(Protocol):
    """召回打分契约（纯函数）：query × item × now → 分数（越大越相关）。"""

    def score(self, query: str, m: "MemoryItem", now: str) -> float: ...


class MemoryEntitySinkLike(Protocol):
    """实体/关系落库契约（**可选**；不挂 ⇒ 不要求模型输出实体，省 token）。

    ``agent_entities`` / ``agent_memory_edges`` 两张表此前是**死表**（零 INSERT）——
    模型按要求输出了，却被 ``remember`` 丢弃。做成接缝后：
    想要图记忆 ⇒ 挂一个 sink，提取提示词会自动带上实体段并把结果交给它；
    不想要 ⇒ 不挂，默认路径不再为用不上的输出付费。
    """

    async def write(self, ctx, entities: list, relations: list) -> int: ...


class MemoryStore:
    """持久化（agent_* 表）。

    ★ ``scoped`` = ``agent_memories.session`` 列是否可用（装配期补列后探测）。
    不可用（补列失败的老库）时**降级为无会话语义的 SQL**，绝不因此让写入整体失败 ——
    会话归属是增强维度，不是硬前提（与 ``core/session.py::coerce_identity`` 同款纪律）。
    """

    def __init__(self, db: Any, scoped: bool = False):
        self._db = db
        #: session 列可用性（由 ``apply`` 探测后注入；未探测 = False ⇒ 走兼容 SQL）
        self.scoped = bool(scoped)

    # ---- 无归属兜底：session 为空的行（存量）仍可见 ----
    _UNSCOPED_SQL = "(session IS NULL OR session = '')"

    async def upsert(self, ctx, m: MemoryItem) -> None:
        if not self.scoped:
            await self._db.execute(
                ctx,
                "INSERT INTO agent_memories(id, kind, content, entity_ids, score_observed,"
                " last_accessed_at, accessed_count, created_at, valid_at, invalid_at, source, extra)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET score_observed=MAX(agent_memories.score_observed,"
                " excluded.score_observed), last_accessed_at=excluded.last_accessed_at,"
                " accessed_count=agent_memories.accessed_count+1, invalid_at=excluded.invalid_at,"
                " source=excluded.source, extra=excluded.extra",
                (m.id, m.kind, m.content, json.dumps(m.entity_ids, ensure_ascii=False),
                 m.score_observed, m.last_accessed_at, m.accessed_count, m.created_at,
                 m.valid_at, m.invalid_at, m.source,
                 json.dumps(m.extra or {}, ensure_ascii=False)),
            )
            return
        await self._db.execute(
            ctx,
            "INSERT INTO agent_memories(id, kind, content, entity_ids, score_observed,"
            " last_accessed_at, accessed_count, created_at, valid_at, invalid_at, source,"
            " session, extra)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET score_observed=MAX(agent_memories.score_observed,"
            " excluded.score_observed), last_accessed_at=excluded.last_accessed_at,"
            " accessed_count=agent_memories.accessed_count+1, invalid_at=excluded.invalid_at,"
            " source=excluded.source, session=excluded.session, extra=excluded.extra",
            (m.id, m.kind, m.content, json.dumps(m.entity_ids, ensure_ascii=False),
             m.score_observed, m.last_accessed_at, m.accessed_count, m.created_at,
             m.valid_at, m.invalid_at, m.source, m.session,
             json.dumps(m.extra or {}, ensure_ascii=False)),
        )

    async def touch(self, ctx, m: MemoryItem) -> None:
        await self._db.execute(
            ctx,
            "UPDATE agent_memories SET last_accessed_at=?, accessed_count=accessed_count+1 WHERE id=?",
            (now_iso(), m.id))

    async def touch_many(self, ctx, mids: list[str]) -> int:
        """批量 touch：N 次 UPDATE 合成 **1 条**（2026-10-04 性能修）。

        语义与逐条 :meth:`touch` **完全等价**：同一轮召回里所有命中的
        ``last_accessed_at`` 本就取同一个 ``now_iso()``，``accessed_count`` 同样 +1。
        改前 recall(limit=8) = 1 次全表读 + **8 次写**；改后 1 读 + 1 写。

        用 ``executemany`` 而非拼 ``IN (...)``：不拼字符串（无注入面）、
        不受 SQLite 变量数上限（999）约束。
        """
        ids = [m for m in dict.fromkeys(mids or []) if m]      # 去重且保序
        if not ids:
            return 0
        stamp = now_iso()
        cur = await self._db.executemany(
            ctx,
            "UPDATE agent_memories SET last_accessed_at=?,"
            " accessed_count=accessed_count+1 WHERE id=?",
            [(stamp, mid) for mid in ids])
        return int(cur or 0)

    async def valid(self, ctx, session: str = "", include_unscoped: bool = True,
                    limit: int | None = None) -> list[MemoryItem]:
        """有效记忆（按会话过滤）。

        ★ **过滤语义**（勿改成严格等值，否则存量记忆会凭空消失）：
        ``session = ?`` **或**无归属（``NULL``/空串，当 ``include_unscoped``）。
        ⇒ 新增记忆严格按会话隔离（根治跨会话串味），存量无归属行仍可见。
        「还剩多少无归属行」由 ``stats()['unscoped']`` 可见，不静默。

        ``session`` 为空 ⇒ 只取无归属行（等价于"共享记忆"查询）。
        ``scoped=False``（列不可用）⇒ 退化为无会话过滤（不劣化）。

        ★ ``limit``（2026-10-04 性能修）：按 ``last_accessed_at`` 倒序取候选。
        **这不是"随便截断"** —— 打分里的新鲜度/重访项本身就以 ``last_accessed_at``
        为主要输入，取最近的候选等价于"先按时间粗筛、再按词面精排"。
        ⚠️ 取舍：极老的记忆可能因落选而召不回。默认上限取得大（2000），
        正常规模下**等于不截断**；超过则自动护栏（否则召回成本随数据量线性上升）。
        ``prune`` 传 ``None`` —— 淘汰必须看**全部**记忆，截断会让它漏删。
        """
        now = now_iso()
        # ★ 括号是**必须的**：``OR`` 优先级低于 ``AND``，
        # 写成 ``invalid_at IS NULL OR invalid_at > ? AND session = ?`` 会被解析成
        # ``invalid_at IS NULL OR (invalid_at > ? AND session = ?)``
        # ⇒ 所有未失效的行都被放行，会话条件形同虚设（本 bug 由会话隔离测试实测抓到）。
        where = "(invalid_at IS NULL OR invalid_at > ?)"
        args: list = [now]
        if self.scoped:
            if session and include_unscoped:
                where += f" AND (session = ? OR {self._UNSCOPED_SQL})"
                args.append(session)
            elif session:
                where += " AND session = ?"
                args.append(session)
            else:
                # 未绑定会话 ⇒ 只看共享域（无归属行），不把别人的会话记忆捞出来
                where += f" AND {self._UNSCOPED_SQL}"
        sql = f"SELECT * FROM agent_memories WHERE {where} ORDER BY last_accessed_at DESC"
        if limit is not None and limit > 0:
            sql += " LIMIT ?"
            args.append(int(limit))
        rows = await self._db.query(ctx, sql, tuple(args))
        return [_from_row(r) for r in rows]

    async def by_kind(self, ctx, kind: str) -> list[MemoryItem]:
        rows = await self._db.query(ctx, "SELECT * FROM agent_memories WHERE kind=?", (kind,))
        return [_from_row(r) for r in rows]

    async def recent_rows(self, ctx, limit: int = 40) -> list[dict]:
        """只读：最近记忆行（UI 展示用；归属表唯一出口在 owner 插件内）。"""
        cols = ("id, kind, content, score_observed, created_at, last_accessed_at,"
                " accessed_count, invalid_at, source")
        if self.scoped:
            cols += ", session"
        return await self._db.query(
            ctx,
            f"SELECT {cols} FROM agent_memories"
            " ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    async def count_unscoped(self, ctx) -> int:
        """无归属（``session`` 为空）记忆条数 —— 会话化的**迁移进度**指标。"""
        if not self.scoped:
            return 0
        rows = await self._db.query(
            ctx, f"SELECT COUNT(*) AS n FROM agent_memories WHERE {self._UNSCOPED_SQL}")
        return int(rows[0]["n"]) if rows else 0

    async def delete(self, ctx, mid: str) -> None:
        await self._db.execute(ctx, "DELETE FROM agent_memories WHERE id=?", (mid,))


class MemoryExtractor:
    """LLM 提取（走 ctx.llm；离线 Fixture 按 MARK_MEMORY_EXTRACT 标记路由）。

    ``want_entities=True`` ⇒ 提示词与 schema 带上实体/关系（**只在挂了 sink 时开**）。
    """

    def __init__(self, want_entities: bool = False):
        self.want_entities = bool(want_entities)
        self.schema = dict(EXTRACT_SCHEMA) if want_entities else dict(BASE_EXTRACT_SCHEMA)
        self.system = build_extract_system(want_entities)

    async def extract(self, ctx, payload: dict) -> dict:
        text = json.dumps(payload, ensure_ascii=False)
        messages = [{"role": "system", "content": self.system},
                    {"role": "user", "content": text[:4000]}]
        try:
            data = (await ctx.llm.complete(ctx, messages, schema=self.schema,
                                           caller="plugins.memory")).data
        except CapabilityNotImplemented:
            raise                             # 提取被拒 → 上层诚实跳过
        out = {
            "facts": [str(x) for x in (data.get("facts") or [])],
            "intentions": [str(x.get("text") if isinstance(x, dict) else x)
                           for x in (data.get("intentions") or [])],
            "entities": list(data.get("entities") or []),
            "relations": list(data.get("relations") or []),
        }
        return out


class MemorySearch:
    """召回打分：词面 ×{0.5} + 新鲜度 ×{0.3} + 重访 ×{0.2}。MVP 词面；向量/MMR 后置。"""

    def __init__(self, *, half_life_days: float = 14.0, access_hl_days: float = 7.0):
        self._hl = half_life_days
        self._ahl = access_hl_days

    def score(self, query: str, m: MemoryItem, now: str) -> float:
        """召回打分：词面 ×{0.5} + 新鲜度 ×{0.3} + 重访 ×{0.2}。

        ★ **空查询不给词面基线**（2026-10-04 修）：原先 ``lex = 0.5 if not q_toks``，
        使空查询的分数下界 = ``0.5*0.5 = 0.25`` —— 恰好等于 ``prune`` 默认的
        ``min_score``，而其余三项恒非负 ⇒ ``score < 0.25`` **永不成立** ⇒
        ``prune`` 的"旧且低分"分支是**数学上的死条件**，``agent_memories`` 只增不减
        （实测：60 天 0.2611 / 3650 天 0.2569，均 ≥ 0.25）。
        改为 0 后下界降到 0.0069 ⇒ 判据可达。真实查询（有词）的行为**不受影响**。
        """
        q_toks = _tokenize(query)
        c_toks = _tokenize(m.content)
        lex = 0.0 if not q_toks else (len(q_toks & c_toks) / len(q_toks))
        freshness = math.exp(-_days_between(m.created_at, now) / self._hl)
        access = math.exp(-_days_between(m.last_accessed_at, now) / self._ahl)
        s = 0.50 * lex + 0.30 * freshness + 0.20 * access
        s += 0.05 * min(1.0, math.log1p(m.accessed_count) / 5.0)
        if m.invalid_at:
            s *= 0.5
        return round(s, 4)


# ---------------------------------------------------------------------------
# 驱动器
# ---------------------------------------------------------------------------


class MemoryDriver:
    """ctx.memory —— remember / add / recall / context / stats / prune / wipe。"""

    def __init__(self, store: MemoryStore, extractor: MemoryExtractor, search: MemorySearch,
                 entity_sink: MemoryEntitySinkLike | None = None):
        self._store = store
        self._extractor = extractor
        self._search = search
        #: 实体/关系落库处（可选）。**为 None 时不要求模型输出实体** ⇒ 不为用不上的输出付费。
        self._sink = entity_sink
        #: 召回候选上限（``None``/``<=0`` = 不限量，回到全表扫描）。
        #: 取值大 ⇒ 正常规模下**等于不截断**，只在数据涨上来后自动护栏。
        self._candidate_cap: int | None = _RECALL_CANDIDATE_CAP

    # ---- 会话归属 ----
    # ★ 会话键**自己取**（``core/session.py`` 的 ContextVar），不要求调用方传
    # ⇒ 既有调用点（``sys/chat.py`` 两处 remember）**零改动**即获得会话隔离。
    # 手法与 ``core/obs.py`` 用 LogRecordFactory 注入 rid 同款：一处生效，不指望
    # 所有调用点都记得传身份。
    def _scope(self, ctx) -> str:
        try:
            return str(current_session_key(ctx) or "")
        except Exception:                       # noqa: BLE001 —— 身份通道不可用 ⇒ 共享域
            return ""

    # ---- 摄取 ----

    async def remember(self, ctx, payload: dict) -> dict:
        """LLM 提取 → 落库（带会话归属）。被 guard/预算拒绝时诚实跳过（记为事件），不假记。"""
        try:
            out = await self._extractor.extract(ctx, payload)
        except CapabilityNotImplemented as exc:
            await durable(ctx, Events.MEMORY_REMEMBERED, {
                "ok": False, "reason": str(exc), "at": now_iso()})
            return {"ok": False, "reason": str(exc)}
        sk = self._scope(ctx)
        added = []
        # ★ 6.2（2026-10-04 修复）：facts / intentions 的批量 upsert 包进一个事务。
        # 此前每条 autocommit，中途异常 ⇒ 部分事实已落库、部分没落 ⇒ 静默不一致。
        # 现在同生共死：任一写入失败整体回滚，由调用方决定重试或跳过。
        db = ctx.get(Services.DB)
        if db is not None:
            async with db.transaction(ctx):
                for kind, texts in (("fact", out["facts"]), ("intention", out["intentions"])):
                    for t in texts:
                        await self._store.upsert(ctx, MemoryItem(
                            id=_mem_id(t, sk), kind=kind, content=t, entity_ids=[],
                            score_observed=1.0, created_at=now_iso(), last_accessed_at=now_iso(),
                            accessed_count=1, session=sk))
                        added.append({"kind": kind, "content": t})
        else:
            for kind, texts in (("fact", out["facts"]), ("intention", out["intentions"])):
                for t in texts:
                    await self._store.upsert(ctx, MemoryItem(
                        id=_mem_id(t, sk), kind=kind, content=t, entity_ids=[],
                        score_observed=1.0, created_at=now_iso(), last_accessed_at=now_iso(),
                        accessed_count=1, session=sk))
                    added.append({"kind": kind, "content": t})
        await durable(ctx, Events.MEMORY_REMEMBERED, {
            "ok": True, "added": len(added), "session": sk, "at": now_iso()})
        # 实体/关系：**有 sink 才落**（无 sink 时提示词里根本没要求，out 里是空列表）
        if self._sink is not None and (out["entities"] or out["relations"]):
            try:
                await self._sink.write(ctx, out["entities"], out["relations"])
            except Exception as exc:                     # noqa: BLE001 —— 图侧失败不否掉事实记忆
                logger.warning("memory entity sink 落库失败: %s", exc)
        return {"ok": True, "added": added}

    async def add(self, ctx, *, kind: str, content: str, entity_ids: list[str] | None = None,
                  score_observed: float = 1.0, source: str | None = None,
                  invalid_at: str | None = None, session: str | None = None,
                  shared: bool = False) -> MemoryItem:
        """手动/决策直写（不经过 LLM），合并去重。

        ``shared=True`` ⇒ **显式声明为跨会话共享**（``session`` 留空）；
        否则取当前会话（未绑定 ⇒ 共享域）。共享是**显式选择**，不再是默认泄露路径。
        """
        if kind not in KINDS:
            raise ValueError(f"kind 须 ∈ {KINDS}")
        now = now_iso()
        sk = "" if shared else (session if session is not None else self._scope(ctx))
        m = MemoryItem(id=_mem_id(content, sk), kind=kind, content=content,
                   entity_ids=entity_ids or [], score_observed=score_observed,
                   created_at=now, last_accessed_at=now, accessed_count=1,
                   invalid_at=invalid_at, source=source, session=sk)
        await self._store.upsert(ctx, m)
        return m

    # ---- 召回 ----

    async def recall(self, ctx, query: str, limit: int = 8,
                     include_unscoped: bool = True) -> list[dict]:
        """按**当前会话**召回（``include_unscoped`` 控制无归属存量行是否参与）。

        性能（2026-10-04）：候选按 ``last_accessed_at`` 倒序在 **SQL 侧**限量
        （默认 ``_RECALL_CANDIDATE_CAP``，可配 ``agent.memory.recall_candidate_cap``；
        配 ``<=0`` = 不限量，回到全表扫描），命中项的 touch 合成 **1 条** UPDATE。
        """
        now = now_iso()
        sk = self._scope(ctx)
        cap = self._candidate_cap
        mems = await self._store.valid(ctx, sk, include_unscoped=include_unscoped,
                                       limit=cap if cap and cap > 0 else None)
        scored = sorted(
            ((self._search.score(query, m, now), m) for m in mems),
            key=lambda t: t[0], reverse=True,
        )[:limit]
        # 批量 touch（一次写；改前是 N 次写）
        await self._store.touch_many(ctx, [m.id for _, m in scored])
        out = []
        for score, m in scored:
            d = m.as_dict()
            d["score"] = score
            out.append(d)
        await durable(ctx, Events.MEMORY_RECALL,
                      {"query": query, "hits": len(out), "session": sk, "at": now})
        return out

    async def list_recent(self, ctx, limit: int = 40) -> list[dict]:
        """只读：最近记忆行（无打分、不 touch）——UI 展示走本接口，消费方零 SQL。"""
        return await self._store.recent_rows(ctx, limit)

    async def context(self, ctx, query: str, limit: int = 5, max_chars: int = 1200) -> str:
        """组装记忆上下文块（to_messages 本地版），翻越上限即截断。"""
        rec = await self.recall(ctx, query, limit=limit)
        if not rec:
            return ""
        lines = ["[相关记忆]"]
        used = 0
        for r in rec:
            seg = f"- ({r['kind']}) {r['content']}  (score {r['score']:.2f} · {_days_between(r['created_at'], now_iso()):.0f}d前)"
            if used + len(seg) > max_chars:
                break
            lines.append(seg)
            used += len(seg)
        return "\n".join(lines)

    # ---- 运维 ----

    async def stats(self, ctx) -> dict:
        rows = await ctx.db.query(
            ctx, "SELECT kind, COUNT(*) AS n FROM agent_memories GROUP BY kind")
        total = await ctx.db.query(ctx, "SELECT COUNT(*) AS n FROM agent_memories")
        out = {"by_kind": {r["kind"]: r["n"] for r in rows}, "total": total[0]["n"],
               "scoped": self._store.scoped}
        # ★ 「还有多少条没有会话归属」必须可见 —— 这是存量数据待收敛的量，
        # 也是"跨会话串味还剩多少"的度量。不静默。
        if self._store.scoped:
            out["unscoped"] = await self._store.count_unscoped(ctx)
        return out

    async def prune(self, ctx, *, older_than_days: float = 30.0, min_score: float = 0.25) -> int:
        """淡化清理：足够旧且低分（以空查询打分）的记忆删除；无效记忆先清。"""
        now = now_iso()
        to_delete = []
        for m in await self._store.valid(ctx):
            age = _days_between(m.created_at, now)
            if m.invalid_at or (age > older_than_days and self._search.score("", m, now) < min_score):
                to_delete.append(m.id)
        if not to_delete:
            return 0
        # 6.4（2026-10-04 修复）：先收集命中 id，再批量 DELETE（IN 占位符，绕开变量上限）。
        CHUNK = 900
        for i in range(0, len(to_delete), CHUNK):
            chunk = to_delete[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            await ctx.db.execute(
                ctx, f"DELETE FROM agent_memories WHERE id IN ({placeholders})", tuple(chunk))
        return len(to_delete)

    async def wipe(self, ctx, *, kind: str | None = None) -> int:
        if kind is None:
            rows = await ctx.db.query(ctx, "SELECT id FROM agent_memories")
        else:
            rows = await ctx.db.query(ctx, "SELECT id FROM agent_memories WHERE kind=?", (kind,))
        ids = [r["id"] for r in rows]
        if not ids:
            return 0
        # 6.4（2026-10-04 修复）：原实现先全量 SELECT id 再逐条 DELETE（N+1）。
        # 改为单条 ``DELETE ... WHERE id IN (...)``（占位符按批量大小动态生成，
        # 避免撞 SQLite 变量上限；同时规避字符串拼接注入面）。
        CHUNK = 900
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            placeholders = ",".join("?" * len(chunk))
            await ctx.db.execute(
                ctx, f"DELETE FROM agent_memories WHERE id IN ({placeholders})", tuple(chunk))
        return len(ids)


async def _ensure_session_column(ctx, db: Any) -> bool:
    """给 ``agent_memories`` 补 ``session`` 列（幂等），返回**该列是否真的可用**。

    与 ``sys/budget.py`` 补列同款：定义归口 ``agent/tables.py``，执行在 owner 插件
    装配期容错跑（``MigrationRegistry`` 无容错，走迁移会整组回滚 ⇒ 服务起不来）。
    SQLite 无 ``ADD COLUMN IF NOT EXISTS`` ⇒ 只能 try/except。

    ★ **返回可用性而不是假设成功**：补列可能因库只读/磁盘满/权限失败，
    若假设成功却在后续 ``INSERT ... session`` 报 "no such column" ⇒ **记忆写入全崩**。
    故装配期用 ``PRAGMA table_info`` **真探测**，不可用则整条路径降级为无会话语义
    （增强维度不是硬前提）。降级**必须可见**（计数 + warning），不静默。
    """
    for col, decl in MEMORIES_EXTRA_COLUMNS:
        try:
            await db.execute(ctx, f"ALTER TABLE agent_memories ADD COLUMN {col} {decl}", ())
        except Exception as exc:                      # noqa: BLE001 —— 列已存在/表未建 ⇒ 尽力而为
            if "duplicate column" not in str(exc).lower():
                inc_counter("agent_memories_column_backfill_failed_total")
                logger.warning("agent_memories 补列 %s 失败 ⇒ 该列将缺失: %s", col, exc)
    try:
        await db.execute(ctx, MEMORIES_SESSION_INDEX, ())
    except Exception as exc:                          # noqa: BLE001 —— 索引失败不影响读写
        logger.warning("agent_memories session 索引创建失败: %s", exc)
    # 6.5（2026-10-04）：召回的 (session, last_accessed_at) 复合索引（存量库补建）。
    try:
        await db.execute(
            ctx, "CREATE INDEX IF NOT EXISTS idx_agent_memories_session_last "
                 "ON agent_memories(session, last_accessed_at)", ())
    except Exception as exc:                          # noqa: BLE001
        logger.warning("agent_memories 复合索引创建失败（召回会慢）: %s", exc)
    try:
        rows = await db.query(ctx, "PRAGMA table_info(agent_memories)")
        ok = any(str(r.get("name")) == "session" for r in rows)
    except Exception:                          # noqa: BLE001 —— 探测失败即按不可用处理
        ok = False
    if not ok:
        inc_counter("agent_memory_unscoped_fallback_total")
        logger.warning("agent_memories 缺少 session 列（补列失败）⇒ 记忆退化为无会话隔离")
    return ok


@plugin
class Memory:
    """ctx.memory —— 记忆子系统插件（Zep 式：时序事实 + 打分衰减 + 上下文注入）。

    系统组件（layer=system），但**以目录插件形态**落在 agent/plugins/memory：
    来源（目录）决定生命周期 —— 可经插件页启用 / 停用 / 卸载；layer 只是语义标签。
    停用后不装配 → ctx.has("memory") 为 False，chat 自动跳过记忆。
    """

    name = "plugins.memory"
    version = "0.1.0"
    layer = "system"
    provides = [Services.MEMORY]
    inject = [Services.DB, Services.EVENTS, Services.LLM]
    tables = ("agent_memories", "agent_entities", "agent_memory_edges")
    description = "记忆：LLM 提取(过 guard/budget) → 打分衰减 → recall/context 注入"

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("memory") or {}
        # 三组件 = 可选注入的接缝服务（与 react 四组件同构，D2 开缝）：
        # ctx.get("memory.store" | "memory.extractor" | "memory.search") 存在即用注入实现。
        #
        # 会话化（2026-10-04）：列由**本插件**自管补（owner 拥有自己的表结构），
        # 与 sys/budget.py 同款；补列结果注入 store ⇒ 不可用时整条路径降级，不崩。
        injected_store = ctx.get("memory.store")
        scoped = False if injected_store is not None else await _ensure_session_column(ctx, ctx.db)
        # 实体/关系 sink（**可选接缝**）：不挂 ⇒ 默认提取器连实体都不向模型要（省 token）。
        injected_extractor = ctx.get("memory.extractor")
        sink = ctx.get("memory.entities")
        cap = cfg.get("recall_candidate_cap", _RECALL_CANDIDATE_CAP)
        try:
            cap = int(cap)
        except (TypeError, ValueError):
            cap = _RECALL_CANDIDATE_CAP
        driver = MemoryDriver(
            injected_store or MemoryStore(ctx.db, scoped=scoped),
            injected_extractor or MemoryExtractor(want_entities=sink is not None),
            ctx.get("memory.search")
            or MemorySearch(half_life_days=float(cfg.get("half_life_days", 14.0)),
                            access_hl_days=float(cfg.get("access_hl_days", 7.0))),
            entity_sink=sink,
        )
        driver._candidate_cap = cap
        ctx.provide(Services.MEMORY, driver)
        self._driver = driver

    async def status(self, ctx) -> dict:
        """运行态摘要（``Container.plugin_statuses`` 消费）。

        此前四个记忆插件**都没有** ``status()``，而 guard/approval/sandbox/mcp/react 都有
        ⇒ 插件页看不到记忆层任何运行态（"装配成功"与"真的在工作"无法区分）。
        这里报**可验证的事实**：条数、按 kind 分布、会话隔离是否生效、无归属存量。
        """
        drv = getattr(self, "_driver", None)
        if drv is None:
            return {"error": "driver 未初始化"}
        try:
            st = await drv.stats(ctx)
        except Exception as exc:                       # noqa: BLE001 —— 自述失败不拖垮整批
            return {"error": str(exc)}
        return {
            "total": st.get("total", 0),
            "by_kind": st.get("by_kind", {}),
            # ★ 会话隔离是否真的生效（补列失败会退化为 False —— 那是"串味"状态，必须可见）
            "scoped": st.get("scoped", False),
            "unscoped": st.get("unscoped"),
            "recall_candidate_cap": drv._candidate_cap,
            "entity_sink": drv._sink is not None,
        }


__all__ = ["Memory", "MemoryItem", "MemoryDriver", "MemoryExtractor", "MemorySearch",
           "MemoryStore", "MemoryStoreLike", "MemoryExtractorLike", "MemorySearchLike",
           "MemoryEntitySinkLike", "build_extract_system", "BASE_EXTRACT_SCHEMA",
           "ENTITY_EXTRACT_SCHEMA", "EXTRACT_SCHEMA", "EXTRACT_SYSTEM"]
