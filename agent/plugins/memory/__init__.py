"""插件包：memory —— 记忆子系统（参考 Zep · 折叠为单一 `plugins.memory` 插件）。

管理：本包位于 agent/plugins/，经「插件」页可停用 / 启用 / 卸载（引擎式 desired/actual/delta）。
语义上是**系统组件**（layer=system，与 guard/llm 同级）；放进 plugins 目录只是采用
「目录插件」这一生命周期形态 —— 来源决定能否启停卸，layer 只是标签，二者正交。
默认启用（见 data/agent_plugin_state.json）；停用后 ctx.has("memory") 为 False，chat 自动跳过记忆。

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
from typing import Any

from agent.api import CapabilityNotImplemented, Events, plugin
from agent.sys.audit import _now_iso, durable

logger = logging.getLogger("plugins.memory")

KINDS = ("fact", "intention", "decision", "observation")

# ---- 提取结构化契约（走 ctx.llm；guard/budget 照生效） ----

EXTRACT_SCHEMA = {
    "facts": {"type": "array"},
    "intentions": {"type": "array"},
    "entities": {"type": "array"},
    "relations": {"type": "array"},
}

EXTRACT_SYSTEM = (
    "<!--memory:extract-->\n"
    "你是记忆提取器。从给定的对话/运行载荷中提取新信息，只输出 JSON：\n"
    '{"facts": ["短句持久事实"], "intentions": [{"text": "用户/任务意图"}],'
    ' "entities": [{"name": "...", "kind": "person|org|topic"}],'
    ' "relations": [{"source": "...", "rel": "...", "target": "..."}]}'
)

_WORD = re.compile(r"[\w一-鿿]+")


def _tokenize(text: str) -> set[str]:
    return {t for t in _WORD.findall(text.lower()) if len(t) >= 2}


def _days_between(a: str, b: str) -> float:
    try:
        return max(0.0, (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds() / 86400.0)
    except Exception:  # noqa: BLE001 —— 坏时间戳按 0
        return 0.0


def _mem_id(content: str) -> str:
    return "mem_" + hashlib.sha1(content.encode("utf-8")).hexdigest()[:16]


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
    extra: dict = None  # type: ignore[assignment]

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "content": self.content,
                "entity_ids": list(self.entity_ids), "score_observed": self.score_observed,
                "created_at": self.created_at, "last_accessed_at": self.last_accessed_at,
                "accessed_count": self.accessed_count, "valid_at": self.valid_at,
                "invalid_at": self.invalid_at, "source": self.source, "extra": self.extra or {}}


def _from_row(r: dict) -> MemoryItem:
    return MemoryItem(id=r["id"], kind=r["kind"], content=r["content"],
                  entity_ids=json.loads(r["entity_ids"] or "[]"),
                  score_observed=float(r["score_observed"] or 0.0),
                  created_at=r["created_at"], last_accessed_at=r["last_accessed_at"],
                  accessed_count=int(r["accessed_count"] or 0),
                  valid_at=r.get("valid_at"), invalid_at=r.get("invalid_at"),
                  source=r.get("source"),
                  extra=json.loads(r["extra"]) if r.get("extra") else {})


# ---------------------------------------------------------------------------
# Store / Extractor / Search（可换 seam）
# ---------------------------------------------------------------------------


class MemoryStore:
    """持久化（agent_* 表）。"""

    def __init__(self, db: Any):
        self._db = db

    async def upsert(self, ctx, m: MemoryItem) -> None:
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

    async def touch(self, ctx, m: MemoryItem) -> None:
        await self._db.execute(
            ctx,
            "UPDATE agent_memories SET last_accessed_at=?, accessed_count=accessed_count+1 WHERE id=?",
            (_now_iso(), m.id))

    async def valid(self, ctx) -> list[MemoryItem]:
        now = _now_iso()
        rows = await self._db.query(
            ctx, "SELECT * FROM agent_memories WHERE invalid_at IS NULL OR invalid_at > ?", (now,))
        return [_from_row(r) for r in rows]

    async def by_kind(self, ctx, kind: str) -> list[MemoryItem]:
        rows = await self._db.query(ctx, "SELECT * FROM agent_memories WHERE kind=?", (kind,))
        return [_from_row(r) for r in rows]

    async def delete(self, ctx, mid: str) -> None:
        await self._db.execute(ctx, "DELETE FROM agent_memories WHERE id=?", (mid,))


class MemoryExtractor:
    """LLM 提取（走 ctx.llm；离线 Fixture 路由 `<!--memory:extract-->`）。"""

    async def extract(self, ctx, payload: dict) -> dict:
        text = json.dumps(payload, ensure_ascii=False)
        messages = [{"role": "system", "content": EXTRACT_SYSTEM},
                    {"role": "user", "content": text[:4000]}]
        try:
            data = (await ctx.llm.complete(ctx, messages, schema=EXTRACT_SCHEMA,
                                           caller="plugins.memory")).data
        except CapabilityNotImplemented:
            raise                             # 提取被拒 → 上层诚实跳过
        return {
            "facts": [str(x) for x in (data.get("facts") or [])],
            "intentions": [str(x.get("text") if isinstance(x, dict) else x)
                           for x in (data.get("intentions") or [])],
            "entities": list(data.get("entities") or []),
            "relations": list(data.get("relations") or []),
        }


class MemorySearch:
    """召回打分：词面 ×{0.5} + 新鲜度 ×{0.3} + 重访 ×{0.2}。MVP 词面；向量/MMR 后置。"""

    def __init__(self, *, half_life_days: float = 14.0, access_hl_days: float = 7.0):
        self._hl = half_life_days
        self._ahl = access_hl_days

    def score(self, query: str, m: MemoryItem, now: str) -> float:
        q_toks = _tokenize(query)
        c_toks = _tokenize(m.content)
        lex = 0.5 if not q_toks else (len(q_toks & c_toks) / len(q_toks))
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

    def __init__(self, store: MemoryStore, extractor: MemoryExtractor, search: MemorySearch):
        self._store = store
        self._extractor = extractor
        self._search = search

    # ---- 摄取 ----

    async def remember(self, ctx, payload: dict) -> dict:
        """LLM 提取 → 落库。被 guard/预算拒绝时诚实跳过（记为事件），不假记。"""
        try:
            out = await self._extractor.extract(ctx, payload)
        except CapabilityNotImplemented as exc:
            await durable(ctx, Events.MEMORY_REMEMBERED, {
                "ok": False, "reason": str(exc), "at": _now_iso()})
            return {"ok": False, "reason": str(exc)}
        added = []
        for kind, texts in (("fact", out["facts"]), ("intention", out["intentions"])):
            for t in texts:
                await self._store.upsert(ctx, MemoryItem(
                    id=_mem_id(t), kind=kind, content=t, entity_ids=[],
                    score_observed=1.0, created_at=_now_iso(), last_accessed_at=_now_iso(),
                    accessed_count=1))
                added.append({"kind": kind, "content": t})
        await durable(ctx, Events.MEMORY_REMEMBERED, {
            "ok": True, "added": len(added), "at": _now_iso()})
        return {"ok": True, "added": added}

    async def add(self, ctx, *, kind: str, content: str, entity_ids: list[str] | None = None,
                  score_observed: float = 1.0, source: str | None = None,
                  invalid_at: str | None = None) -> MemoryItem:
        """手动/决策直写（不经过 LLM），合并去重。"""
        if kind not in KINDS:
            raise ValueError(f"kind 须 ∈ {KINDS}")
        now = _now_iso()
        m = MemoryItem(id=_mem_id(content), kind=kind, content=content,
                   entity_ids=entity_ids or [], score_observed=score_observed,
                   created_at=now, last_accessed_at=now, accessed_count=1,
                   invalid_at=invalid_at, source=source)
        await self._store.upsert(ctx, m)
        return m

    # ---- 召回 ----

    async def recall(self, ctx, query: str, limit: int = 8) -> list[dict]:
        now = _now_iso()
        mems = await self._store.valid(ctx)
        scored = sorted(
            ((self._search.score(query, m, now), m) for m in mems),
            key=lambda t: t[0], reverse=True,
        )[:limit]
        out = []
        for score, m in scored:
            await self._store.touch(ctx, m)
            d = m.as_dict()
            d["score"] = score
            out.append(d)
        await durable(ctx, Events.MEMORY_RECALL, {"query": query, "hits": len(out), "at": now})
        return out

    async def context(self, ctx, query: str, limit: int = 5, max_chars: int = 1200) -> str:
        """组装记忆上下文块（to_messages 本地版），翻越上限即截断。"""
        rec = await self.recall(ctx, query, limit=limit)
        if not rec:
            return ""
        lines = ["[相关记忆]"]
        used = 0
        for r in rec:
            seg = f"- ({r['kind']}) {r['content']}  (score {r['score']:.2f} · {_days_between(r['created_at'], _now_iso()):.0f}d前)"
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
        return {"by_kind": {r["kind"]: r["n"] for r in rows}, "total": total[0]["n"]}

    async def prune(self, ctx, *, older_than_days: float = 30.0, min_score: float = 0.25) -> int:
        """淡化清理：足够旧且低分（以空查询打分）的记忆删除；无效记忆先清。"""
        now = _now_iso()
        removed = 0
        for m in await self._store.valid(ctx):
            age = _days_between(m.created_at, now)
            if m.invalid_at or (age > older_than_days and self._search.score("", m, now) < min_score):
                await self._store.delete(ctx, m.id)
                removed += 1
        return removed

    async def wipe(self, ctx, *, kind: str | None = None) -> int:
        if kind is None:
            rows = await ctx.db.query(ctx, "SELECT id FROM agent_memories")
        else:
            rows = await ctx.db.query(ctx, "SELECT id FROM agent_memories WHERE kind=?", (kind,))
        for r in rows:
            await self._store.delete(ctx, r["id"])
        return len(rows)


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
    provides = ["memory"]
    inject = ["db", "events", "llm"]
    tables = ("agent_memories", "agent_entities", "agent_memory_edges")
    description = "记忆：LLM 提取(过 guard/budget) → 打分衰减 → recall/context 注入"

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("memory") or {}
        driver = MemoryDriver(
            MemoryStore(ctx.db),
            MemoryExtractor(),
            MemorySearch(half_life_days=float(cfg.get("half_life_days", 14.0)),
                         access_hl_days=float(cfg.get("access_hl_days", 7.0))),
        )
        ctx.provide("memory", driver)


__all__ = ["Memory", "MemoryItem", "MemoryDriver", "MemoryExtractor", "MemorySearch",
           "MemoryStore"]
