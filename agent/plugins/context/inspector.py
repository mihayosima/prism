"""agent.plugins.context.inspector —— Inspector：``/context`` 视图 + 四不变量断言 + 轮次全景账本。

G2.0（P3）新增。三职责（对应方案 §4.3「Inspector 视图与不变量」）：

1. **``/context`` 分类视图**：把 ``AssemblyReport``（经 ``agent/context/governed`` 遥测
   重建）按**层**（resident / on_demand / memory / runtime / input）汇总，回答「上下文占用
   长什么样」——纯只读旁路，**不新增任何状态**；
2. **四条不变量断言**（§4.6）：I-1（配对完整）/ I-2（单元原子性）在 ``execute`` 内由治理器
   在**装配对象面**守护（见 ``governor._invariants_ok``）；I-3（前缀稳定性可测）/
   I-4（预算守恒）在**记账面**以本模块的**纯函数**提供，供每次装配后「记录 + 告警（不阻断）」；
3. **轮次全景账本** ``RoundLedger``：合并四端点（plan / prior / react / narrate）为
   「这一轮钱花在哪」，并逐端点自证 ``Σ块used + Σ附件used + unaccounted = 预算``（P3 验收）。

纪律（与治理器同级：**零状态 / 零业务语义 / 零特权**）：
- 本模块是**纯数据 + 纯函数**：不读历史、不写状态、不 import 任何插件/内核实现（无副作用）；
- 只认**契约词汇**（token 数、层名、不变量 ID）；``stage`` / ``scope`` 是**不透明归集键**
  ——本模块只把它们当作分组标签，**绝不据此做业务分支**；
- 渲染是**纯只读旁路**：任何渲染/校验失败都不得影响装配（调用方负责吞异常，见
  ``run/chat.py`` 的 ``/api/context``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

# ---- 契约词汇（与 §4.4 五层同源；未知层原样显示，不臆造语义）----
_LAYER_LABEL: dict[str, str] = {
    "resident": "常驻",
    "on_demand": "按需",
    "memory": "记忆",
    "input": "输入",
    "runtime": "运行时",
}
# 显示顺序：与「窗口序」（常驻 → 按需 → 记忆 → 输入 → 运行时）一致；未知层排最后。
_LAYER_ORDER: tuple[str, ...] = ("resident", "on_demand", "memory", "input", "runtime")

# 四不变量 ID（§4.6；与 ``ApplyResult.invariants`` 的键同源）
INVARIANTS: tuple[str, ...] = ("I-1", "I-2", "I-3", "I-4")

# I-4 预算守恒容差（token 级）：0 = 要求精确守恒（``unaccounted`` 的定义即为精确差额）。
DEFAULT_TOLERANCE = 0


def _i(v: Any, default: int = 0) -> int:
    """宽容取整（``None`` / 非数 → 缺省）；载荷来自遥测，字段可能缺席或为 str。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _layout(layer: str) -> str:
    """层显示名：契约层给中文名，未知层原样（不臆造）。"""
    return _LAYER_LABEL.get(layer, layer or "unknown")


# ---------------------------------------------------------------------------
# 记账数据（纯数据；由 governed 遥测载荷重建）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BlockUsage:
    """单块记账（``BlockReport`` 的可序列化投影）。"""

    key: str
    layer: str = ""
    present: bool = True
    reason: str = ""
    requested: int = 0
    allocated: int = 0
    used: int = 0
    cap: int = 0


@dataclass(frozen=True)
class AttachUsage:
    """单个请求附加项记账（``AttachmentReport`` 的可序列化投影）。"""

    key: str
    claimed: int = 0
    used: int = 0


@dataclass(frozen=True)
class LedgerEntry:
    """**一个装配端点的一次记账**（纯数据；由 ``CONTEXT_GOVERNED`` 载荷重建）。

    - ``input_budget``：**生效**预算（已扣预留并作因子校准，用于实际分配）；
    - ``budget_raw``：**原始**输入预算——``unaccounted_tokens`` 的对照基数；
    - ``unaccounted_tokens``：预算黑洞（＝ ``budget_raw − Σ块used − Σ附件used``，≥0）。
    """

    stage: str
    scope: str | None = None
    at: str = ""
    input_budget: int = 0
    budget_raw: int = 0
    total_used: int = 0
    unaccounted_tokens: int = 0
    stable_prefix_tokens: int = 0
    volatile_blocks: tuple[str, ...] = ()
    missing_blocks: tuple[str, ...] = ()
    attachments: tuple[AttachUsage, ...] = ()
    blocks: tuple[BlockUsage, ...] = ()

    # ---- 聚合（纯派生，不外存） ----

    def block_used(self) -> int:
        """Σ块实占 token。"""
        return sum(max(0, b.used) for b in self.blocks)

    def attach_used(self) -> int:
        """Σ附件实占 token。"""
        return sum(max(0, a.used) for a in self.attachments)

    def accounted(self) -> int:
        """已入账总量 = Σ块used + Σ附件used。"""
        return self.block_used() + self.attach_used()

    def by_layer(self) -> dict[str, int]:
        """按层汇总实占（含未标注层的兜底键 ``unknown``）。"""
        out: dict[str, int] = {}
        for b in self.blocks:
            k = b.layer or "unknown"
            out[k] = out.get(k, 0) + max(0, b.used)
        return out

    # ---- I-4 预算守恒（记账面断言） ----

    def budget_residual(self) -> int:
        """``Σ块used + Σ附件used + unaccounted − 预算``（0 = 守恒；负/正值即破口）。"""
        return self.accounted() + max(0, self.unaccounted_tokens) - self.budget_raw

    def conserved(self, *, tolerance: int = DEFAULT_TOLERANCE) -> bool:
        """**I-4**：预算守恒（``|残差| ≤ 容差``）。"""
        return abs(self.budget_residual()) <= int(tolerance)

    # ---- I-3 前缀稳定性（记账面断言 + 体检） ----

    def prefix_measurable(self) -> bool:
        """**I-3**：``stable_prefix_tokens`` 可查询且**头部非易变**。

        判据：若存在 volatile 块且稳定前缀为 0（首个块即易变），则整条前缀不可缓存 → 违规。
        中段 volatile（如 react 的 prompt 位于 system/prior 之后）**不算违规**——它是 F-1
        已识别的**已知形态**，由视图的体检行提示「其后仍有 N tok 受影响」，不上升为红灯。
        """
        return not (self.volatile_blocks and max(0, self.stable_prefix_tokens) <= 0)

    def prefix_affected(self) -> int:
        """首个 volatile 块**之后**仍受影响的装配量（缓存友好度体检量）。"""
        return max(0, self.total_used - max(0, self.stable_prefix_tokens))

    # ---- 全四条不变量（记账面可判定的两条；I-1/I-2 需消息面，归 ``execute``） ----

    def invariants(self, *, tolerance: int = DEFAULT_TOLERANCE) -> dict[str, bool]:
        """记账面可判定的不变量（I-3 / I-4）。

        I-1 / I-2 的判定需要 **messages 原始对象**（装配对象面，见 ``governor.execute``）；
        记账面（遥测投影）不含消息体，故此处不虚报——只回报可判定者。
        """
        return {"I-3": self.prefix_measurable(), "I-4": self.conserved(tolerance=tolerance)}

    def violations(self, *, tolerance: int = DEFAULT_TOLERANCE) -> list[str]:
        """未通过的不变量 ID 列表（空 = 全绿）。"""
        return [k for k, ok in self.invariants(tolerance=tolerance).items() if not ok]


@dataclass(frozen=True)
class RoundLedger:
    """**轮次全景账本**：合并四端点（plan / prior / react / narrate）为「这一轮钱花在哪」。

    纯数据容器——``entries`` 由消费方按**不透明 stage 标签**归集；本类只做聚合、对账与渲染，
    不猜测轮次边界、不新增状态（轮次归集由调用方决定，见 ``run/chat.py`` 的 ``/api/context``）。
    """

    entries: tuple[LedgerEntry, ...] = ()

    def stages(self) -> list[str]:
        """本账本覆盖的 stage 序列（保序去重）。"""
        seen: list[str] = []
        for e in self.entries:
            if e.stage not in seen:
                seen.append(e.stage)
        return seen

    def entry(self, stage: str) -> LedgerEntry | None:
        """取指定 stage 的**最后**一次记账（同 stage 多次装配时以最新为准）。"""
        found = [e for e in self.entries if e.stage == stage]
        return found[-1] if found else None

    def totals(self) -> dict[str, int]:
        """四端点累计（「这一轮」总量）：块/附件/未入账/原始预算/生效预算/总占用。"""
        return {
            "entries": len(self.entries),
            "block_used": sum(e.block_used() for e in self.entries),
            "attach_used": sum(e.attach_used() for e in self.entries),
            "unaccounted": sum(max(0, e.unaccounted_tokens) for e in self.entries),
            "budget_raw": sum(e.budget_raw for e in self.entries),
            "input_budget": sum(e.input_budget for e in self.entries),
            "total_used": sum(e.total_used for e in self.entries),
        }

    def reconcile(self, *, tolerance: int = DEFAULT_TOLERANCE) -> dict[str, bool]:
        """逐端点的 **I-4** 守恒结果（stage → 是否守恒）。"""
        return {e.stage: e.conserved(tolerance=tolerance) for e in self.entries}

    def violations(self, *, tolerance: int = DEFAULT_TOLERANCE) -> dict[str, list[str]]:
        """逐端点未通过的不变量（stage → 违规 ID 列表；只含非空项）。"""
        out: dict[str, list[str]] = {}
        for e in self.entries:
            bad = e.violations(tolerance=tolerance)
            if bad:
                out[e.stage] = bad
        return out

    # ---- 渲染 ----

    def render(self, *, tolerance: int = DEFAULT_TOLERANCE,
               title: str = "轮次全景账本（RoundLedger）") -> str:
        """渲染整轮视图：逐端点分类视图 + 全局合计 + 对账结论。"""
        lines = [title, "=" * 56]
        if not self.entries:
            lines.append("（无记账数据）")
            return "\n".join(lines)
        for e in self.entries:
            lines.append(render_entry(e, tolerance=tolerance))
            lines.append("")
        tot = self.totals()
        lines.append("─" * 56)
        lines.append(
            f"合计（{tot['entries']} 端点）：块 {tot['block_used']:,} + 附件 {tot['attach_used']:,}"
            f" + 未入账 {tot['unaccounted']:,} = {tot['block_used'] + tot['attach_used'] + tot['unaccounted']:,}"
            f"  / 原始预算 {tot['budget_raw']:,}")
        recon = self.reconcile(tolerance=tolerance)
        ok_all = all(recon.values())
        lines.append("对账（I-4 预算守恒）：" + ("✓ 全端点守恒" if ok_all else
                     "✗ 破口 " + ", ".join(f"{k}" for k, v in recon.items() if not v)))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 纯函数：载荷 → 记账（消费层零 import：载荷为普通 dict）
# ---------------------------------------------------------------------------


def entry_from_payload(payload: Mapping[str, Any]) -> LedgerEntry:
    """把 ``CONTEXT_GOVERNED`` 遥测载荷（普通 dict）重建为 ``LedgerEntry``（纯函数）。"""
    blocks = tuple(
        BlockUsage(
            key=str(b.get("key", "")),
            layer=str(b.get("layer", "") or ""),
            present=bool(b.get("present", True)),
            reason=str(b.get("reason", "") or ""),
            requested=_i(b.get("requested")),
            allocated=_i(b.get("allocated")),
            used=_i(b.get("used")),
            cap=_i(b.get("cap")),
        )
        for b in (payload.get("blocks") or [])
        if isinstance(b, Mapping)
    )
    atts = tuple(
        AttachUsage(key=str(a.get("key", "")),
                    claimed=_i(a.get("claimed")),
                    used=_i(a.get("used")))
        for a in (payload.get("attachments") or [])
        if isinstance(a, Mapping)
    )
    input_budget = _i(payload.get("input_budget"))
    return LedgerEntry(
        stage=str(payload.get("stage", "")),
        scope=payload.get("scope"),
        at=str(payload.get("at", "") or ""),
        input_budget=input_budget,
        # 向后兼容：旧载荷无 ``budget_raw`` → 退化为生效预算（此时差额仅含因子/预留项）。
        budget_raw=_i(payload.get("budget_raw"), input_budget),
        total_used=_i(payload.get("total_used")),
        unaccounted_tokens=_i(payload.get("unaccounted_tokens")),
        stable_prefix_tokens=_i(payload.get("stable_prefix_tokens")),
        volatile_blocks=tuple(str(x) for x in (payload.get("volatile_blocks") or [])),
        missing_blocks=tuple(str(x) for x in (payload.get("missing_blocks") or [])),
        attachments=atts,
        blocks=blocks,
    )


def ledger_from_payloads(payloads: Iterable[Mapping[str, Any]]) -> RoundLedger:
    """把一串 governed 载荷重建为 ``RoundLedger``（纯函数，保序）。"""
    return RoundLedger(entries=tuple(entry_from_payload(p) for p in payloads))


# ---------------------------------------------------------------------------
# 渲染：``/context`` 分类视图
# ---------------------------------------------------------------------------


def _pct(part: int, whole: int) -> str:
    if whole <= 0:
        return "—"
    return f"{100.0 * part / whole:.1f}%"


def render_entry(entry: LedgerEntry, *, tolerance: int = DEFAULT_TOLERANCE) -> str:
    """渲染**单个端点**的 ``/context`` 分类视图（§4.3 形态；数据驱动，不臆造字段）。"""
    scope = entry.scope if entry.scope else "default"
    head = f"上下文占用  stage={entry.stage}  scope={scope}"
    if entry.at:
        head += f"  at={entry.at}"
    lines = [head]

    by_layer = entry.by_layer()
    base = entry.budget_raw or entry.input_budget or 0   # 百分比分母：优先原始预算
    ordered = [ly for ly in _LAYER_ORDER if ly in by_layer]
    ordered += [ly for ly in by_layer if ly not in _LAYER_ORDER]
    for ly in ordered:
        used = by_layer[ly]
        lines.append(f"  {_layout(ly):<4} {ly:<9} {used:>8,} tok  {_pct(used, base):>6}")

    if entry.attachments:
        for a in entry.attachments:
            lines.append(f"  附件  {a.key:<14} {max(0, a.used):>8,} tok  {_pct(a.used, base):>6}")

    lines.append("  " + "─" * 50)
    lines.append(
        f"  合计 块 {entry.block_used():,} + 附件 {entry.attach_used():,}"
        f" + 未入账 {max(0, entry.unaccounted_tokens):,}"
        f" = {entry.accounted() + max(0, entry.unaccounted_tokens):,}"
        f" / 原始预算 {entry.budget_raw:,}（生效 {entry.input_budget:,}）")

    # I-4 对账
    residual = entry.budget_residual()
    lines.append(f"  I-4 预算守恒：{'✓' if entry.conserved(tolerance=tolerance) else '✗'}"
                 f"（残差 {residual:+,}）")

    # I-3 前缀体检
    vol = "、".join(entry.volatile_blocks) if entry.volatile_blocks else "无"
    mark = ""
    if entry.volatile_blocks and entry.prefix_affected() > 0:
        mark = f" → ⚠ 其后仍有 {entry.prefix_affected():,} tok 受影响"
    lines.append(f"  稳定前缀 {max(0, entry.stable_prefix_tokens):,} tok"
                 f"（volatile 块：{vol}）{mark}")
    if entry.missing_blocks:
        lines.append(f"  漏报块（declared 但未提交）：{'、'.join(entry.missing_blocks)}")
    return "\n".join(lines)


def render_context_view(entry: LedgerEntry, *, tolerance: int = DEFAULT_TOLERANCE) -> str:
    """单端点分类视图的公共别名（供调试端点 / CLI 直接调用）。"""
    return render_entry(entry, tolerance=tolerance)


__all__ = [
    "INVARIANTS",
    "DEFAULT_TOLERANCE",
    "BlockUsage",
    "AttachUsage",
    "LedgerEntry",
    "RoundLedger",
    "entry_from_payload",
    "ledger_from_payloads",
    "render_entry",
    "render_context_view",
]
