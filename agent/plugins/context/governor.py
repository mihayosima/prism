"""agent.plugins.context.governor —— 上下文治理器（G0 骨架 + G1 装配）。

预算模型：输入窗口总预算（``max_context_tokens - reserve_output_tokens``）按块分配——
  记忆块 ``memory_ratio`` · 历史窗口 ``history_ratio`` · 其余归 system/工具块/本轮消息。
裁剪一律按 token（``core.tokens.estimate_tokens``），不再按条数。

G0 提供预算查询 + 历史收窗（``pick_history``，保留供装配复用）。
G1 提供**分块装配** ``assemble(blocks)`` —— 注册制动态组装 + 统一互斥（左开右闭）+
刚性超限双通道标注 + 剩余回吐（water-filling）；消费面软依赖
``ctx.has(Services.CONTEXT)``——插件停用即退回各消费方现状，不劣化不裸崩。
"""

from __future__ import annotations

from typing import Any

from agent.api import clip_text, estimate_tokens

from .protocols import Assembly, AssemblyReport, BlockReport, BlockSpec

# 可裁块默认权重（``BlockSpec.weight <= 0`` 时按 key 取；治理器自有口径，不依赖任何插件）。
_DEFAULT_WEIGHTS = {"memory": 0.10, "tools": 0.20, "history": 0.60, "summary": 0.10}
_DEFAULT_WEIGHT = 0.10
_SEP = "\n\n"          # 非空 text 块连接符（与原 system 拼接语义一致 → 对拍逐字节等价）


class ContextGovernor:
    """上下文治理器：预算模型 + token 化收窗 + 分块装配（接缝默认实现，可注入替换）。"""

    def __init__(
        self,
        *,
        max_context_tokens: int = 24000,
        reserve_output_tokens: int = 4000,
        memory_ratio: float = 0.10,
        history_ratio: float = 0.60,
        source_clip_tokens: int = 8192,
    ):
        if max_context_tokens <= 0 or reserve_output_tokens < 0:
            raise ValueError("max_context_tokens 须 > 0 且 reserve_output_tokens 须 >= 0")
        if reserve_output_tokens >= max_context_tokens:
            raise ValueError("reserve_output_tokens 须小于 max_context_tokens")
        if not (0.0 < memory_ratio < 1.0 and 0.0 < history_ratio < 1.0):
            raise ValueError("memory_ratio / history_ratio 须在 (0,1) 区间")
        self.max_context_tokens = int(max_context_tokens)
        self.reserve_output_tokens = int(reserve_output_tokens)
        self.memory_ratio = float(memory_ratio)
        self.history_ratio = float(history_ratio)
        self.source_clip_tokens = int(source_clip_tokens)

    # ---- 构造 ----

    @classmethod
    def from_config(cls, cfg: dict | None) -> "ContextGovernor":
        """从 ``agent.context`` 配置节构造（缺省键落默认值，诚实显式）。"""
        cfg = cfg or {}
        return cls(
            max_context_tokens=int(cfg.get("max_context_tokens", 24000)),
            reserve_output_tokens=int(cfg.get("reserve_output_tokens", 4000)),
            memory_ratio=float(cfg.get("memory_ratio", 0.10)),
            history_ratio=float(cfg.get("history_ratio", 0.60)),
            source_clip_tokens=int(cfg.get("source_clip_tokens", 8192)),
        )

    # ---- 预算查询 ----

    def input_budget(self) -> int:
        """输入窗口总预算 = 总窗口 - 输出余量。"""
        return self.max_context_tokens - self.reserve_output_tokens

    def memory_budget(self) -> int:
        """记忆块配额（token）——消费方据此反推 memory.context 实参（G4）。"""
        return int(self.input_budget() * self.memory_ratio)

    def history_budget(self) -> int:
        """历史窗口配额（token）——本预算内从新到旧贪心收取。"""
        return int(self.input_budget() * self.history_ratio)

    def source_clip_budget(self) -> int:
        """MCP 源头护栏上限（G3 接线用；G0 先固化口径）。"""
        return self.source_clip_tokens

    # ---- 历史收窗（收窗机制已从 chat/compactor 移除调用；保留供装配/未来复用） ----

    def pick_history(
        self,
        messages: list[dict],
        budget: int | None = None,
        *,
        keep_first: bool = False,
    ) -> tuple[list[dict], int]:
        """token 口径历史收窗：从最新往回贪心收取，超出预算即止。

        刚性保障（可超预算，保对话不残）：
        - **最新一条**永远保留（当前话题不可丢）；
        - ``keep_first=True`` 时**首条 user 消息**额外保留（多步循环"首问"语义，
          与 react 既有 ``_trim`` 行为对齐）。

        返回 ``(按原顺序保留的消息, 丢弃条数)``。只裁喂给模型的窗口，
        不动调用方持有的消息全集。
        """
        if budget is None:
            budget = self.history_budget()
        n = len(messages)
        if n == 0:
            return [], 0
        toks = [estimate_tokens(m) for m in messages]
        keep: set[int] = set()
        used = 0
        # 刚性：最新一条
        keep.add(n - 1)
        used += toks[n - 1]
        # 贪心：从新到旧
        for i in range(n - 2, -1, -1):
            if used + toks[i] > budget:
                break
            keep.add(i)
            used += toks[i]
        # 刚性：首条 user（keep_first）
        if keep_first:
            fu = next((i for i, m in enumerate(messages)
                       if isinstance(m, dict) and m.get("role") == "user"), None)
            if fu is not None:
                keep.add(fu)
        dropped = n - len(keep)
        kept = [m for i, m in enumerate(messages) if i in keep]
        return kept, dropped

    # ---- 分块装配（G1） ----

    def _block_tokens(self, kind: str, payload: Any) -> int:
        """块内容 token 估算（text 串 / messages 列表）。"""
        if kind == "messages":
            return sum(estimate_tokens(m) for m in (payload or []))
        return estimate_tokens(payload or "")

    def _weight_of(self, b: BlockSpec) -> float:
        if b.weight > 0:
            return float(b.weight)
        return _DEFAULT_WEIGHTS.get(b.key, _DEFAULT_WEIGHT)

    @staticmethod
    def _present_nonempty(b: BlockSpec) -> bool:
        """注册制核心：`present=False` 或内容为空 → 不参与预算/装配。"""
        if not b.present:
            return False
        if b.kind == "messages":
            return bool(b.payload)
        return bool(str(b.payload or "").strip())

    @staticmethod
    def _coerce(b: BlockSpec | dict) -> BlockSpec:
        """兼容消费层传入 dict（chat 不 import 契约类型；解耦纪律）。"""
        if isinstance(b, BlockSpec):
            return b
        cov = b.get("coverage")
        return BlockSpec(
            key=str(b.get("key", "")),
            kind=str(b.get("kind", "text")),
            present=bool(b.get("present", False)),
            payload=b.get("payload", ""),
            weight=float(b.get("weight", 0.0) or 0.0),
            priority=int(b.get("priority", 100)),
            rigid=bool(b.get("rigid", False)),
            coverage=(int(cov[0]), int(cov[1])) if cov else None,
        )

    def _clip_hard(self, text: str, budget: int) -> str:
        """``clip_text`` 的省略标注自身有 token 开销，可能致结果略超预算 →
        按超出比例迭代收缩目标，直至实占 <= 预算（保证装配合约）。"""
        if budget <= 0:
            return ""
        out = text
        for _ in range(4):
            t = estimate_tokens(out)
            if t <= budget:
                break
            out, _ = clip_text(out, max(1, int(budget * budget / max(1, t))))
        return out

    def _trim_text(self, text: str, budget: int) -> tuple[str, bool]:
        if budget <= 0:
            return "", bool(text.strip())
        if estimate_tokens(text) <= budget:
            return text, False
        return self._clip_hard(text, budget), True

    def _trim_messages(self, messages: list[dict], budget: int) -> list[dict]:
        """token 口径从新到旧贪心（pick_history 同口径）；末条超预算则 clip，保证不超上限。"""
        if not messages or budget <= 0:
            return []
        toks = [estimate_tokens(m) for m in messages]
        keep: list[dict] = []
        used = 0
        for i in range(len(messages) - 1, -1, -1):
            if used + toks[i] <= budget:
                keep.append(messages[i])
                used += toks[i]
            elif not keep:               # 最新一条超预算 → clip 到预算，保对话不残
                m = dict(messages[i])
                m["content"] = self._clip_hard(str(m.get("content", "")), budget)
                keep.append(m)
                break
            else:
                break
        keep.reverse()
        return keep

    def assemble(self, blocks: list[BlockSpec]) -> Assembly:
        """分块装配：注册制过滤 → 刚性先扣 → 可裁回吐分配 → 块内裁剪 → 互斥输出。

        - **注册制**：只对 `present` 且非空的块分配预算；权重在"存在的块"间重归一。
        - **刚性超限**：按 priority 升序（越重要越先满足）分配，次要块降级并**双通道标注**
          （`AssemblyReport.rigid_overflow` + `system_text` 内显式标注段），绝不静默截断。
        - **剩余回吐**：可裁块 water-filling——未用满余量回吐公共池再分配（`redistributed` 记账）。
        - **互斥**：`messages` 块按 `coverage`（左开右闭）排序，重叠时高阶块胜出，
          绝不双份注入；输出前剥离内部 `seq`。
        """
        ib = self.input_budget()
        blocks = [self._coerce(b) for b in blocks]        # 兼容 dict 入参（消费层零 import）
        active = [b for b in blocks if self._present_nonempty(b)]
        active_keys = {b.key for b in active}

        # 1) 刚性块先扣（priority 升序 = 越重要越先满足）
        rigid = sorted([b for b in active if b.rigid], key=lambda b: b.priority)
        flex = [b for b in active if not b.rigid]
        rigid_alloc: dict[str, int] = {}
        rigid_req: dict[str, int] = {}
        overflow_keys: list[str] = []
        remaining = ib
        for b in rigid:
            need = self._block_tokens(b.kind, b.payload)
            rigid_req[b.key] = need
            give = min(need, max(0, remaining))
            rigid_alloc[b.key] = give
            remaining -= give
            if give < need:
                overflow_keys.append(b.key)

        # 2) 可裁块分配（water-filling：未用满余量回吐公共池再分配）
        pool = max(0, remaining)
        demand = {b.key: self._block_tokens(b.kind, b.payload) for b in flex}
        cap: dict[str, int] = {b.key: 0 for b in flex}
        work = [b for b in flex if demand[b.key] > 0]
        total_w0 = sum(self._weight_of(b) for b in work) or 1.0
        init_share = {b.key: pool * self._weight_of(b) / total_w0 for b in work}
        while work:
            total_w = sum(self._weight_of(b) for b in work) or 1.0
            share = {b.key: pool * self._weight_of(b) / total_w for b in work}
            fit = [b for b in work if demand[b.key] <= share[b.key]]
            if not fit:                       # 都超份额 → 按份额截断，收敛
                for b in work:
                    cap[b.key] = max(0, int(share[b.key]))
                break
            for b in fit:                     # 落定；余量自然回吐（pool 减少）
                cap[b.key] = demand[b.key]
                pool -= demand[b.key]
                work.remove(b)
            if pool <= 0:
                for b in work:
                    cap[b.key] = 0
                break
        redistributed = sum(max(0, int(init_share[b.key] - demand[b.key]))
                            for b in flex if cap[b.key] >= demand[b.key])

        # 3) 块内裁剪（text / messages）
        out_text: dict[str, str] = {}
        out_msgs: dict[str, list[dict]] = {}
        used: dict[str, int] = {}
        trimmed: dict[str, bool] = {}
        for b in active:
            alloc = rigid_alloc.get(b.key, 0) if b.rigid else cap.get(b.key, 0)
            if b.kind == "messages":
                msgs = self._trim_messages(list(b.payload), alloc)
                out_msgs[b.key] = msgs
                used[b.key] = self._block_tokens("messages", msgs)
                trimmed[b.key] = len(msgs) < len(b.payload)
            else:
                text, cut = self._trim_text(str(b.payload), alloc)
                out_text[b.key] = text
                used[b.key] = estimate_tokens(text)
                trimmed[b.key] = cut

        # 4) messages 互斥（左开右闭：相邻块边界相接；重叠则高阶块胜出，绝不双份注入）
        msg_blocks = sorted([b for b in active if b.kind == "messages"],
                            key=lambda b: (b.coverage[0] if b.coverage else 0))
        cover_hi = 0
        for b in msg_blocks:
            lo, hi = (b.coverage or (0, 0))
            if b.coverage is not None:
                kept: list[dict] = []
                for m in out_msgs[b.key]:
                    if "seq" not in m:        # 无 seq：信任调用方划分，不逐条过滤
                        kept.append(m)
                        continue
                    s = int(m["seq"])
                    # 左开右闭本块范围 (lo, hi] + 不与前块重叠（s > cover_hi）→ 越界/重叠即剔除
                    if s <= lo or s > hi or s <= cover_hi:
                        continue
                    kept.append(m)
                if len(kept) != len(out_msgs[b.key]):
                    out_msgs[b.key] = kept
                    used[b.key] = self._block_tokens("messages", kept)
                    trimmed[b.key] = True
            cover_hi = max(cover_hi, hi)

        # 5) 输出（text 块按 priority 升序 _SEP 连接；messages 按 coverage.lo 排序、剥离 seq）
        text_parts = [out_text[b.key] for b in sorted(
            [b for b in active if b.kind == "text" and out_text.get(b.key)],
            key=lambda b: b.priority)]
        system_text = _SEP.join(text_parts)

        prior: list[dict] = []
        for b in msg_blocks:
            for m in out_msgs.get(b.key, []):
                prior.append({k: v for k, v in m.items() if k != "seq"})

        # 6) 报告（刚性超限双通道之一：report）
        reports: list[BlockReport] = []
        for b in blocks:
            if b.key not in active_keys:
                reports.append(BlockReport(b.key, b.present, b.kind,
                                           reason="absent" if not b.present else "empty"))
                continue
            alloc = rigid_alloc.get(b.key, 0) if b.rigid else cap.get(b.key, 0)
            req = rigid_req.get(b.key, demand.get(b.key, 0))
            u = used.get(b.key, 0)
            reason = ("downgraded" if b.key in overflow_keys
                      else "trimmed" if trimmed.get(b.key) else "ok")
            reports.append(BlockReport(b.key, b.present, b.kind, requested=req,
                                       allocated=alloc, used=u,
                                       trimmed=trimmed.get(b.key, False), reason=reason))
        total_used = estimate_tokens(system_text) + self._block_tokens("messages", prior)
        rigid_overflow = [r for r in reports if r.reason == "downgraded"]
        report = AssemblyReport(blocks=reports, total_used=total_used, input_budget=ib,
                                rigid_overflow=rigid_overflow, redistributed=redistributed)

        # 7) 刚性超限双通道之二：文本通道（显式标注段，绝不静默截断）
        if rigid_overflow:
            marks = "、".join(f"{r.key} 缺 {r.requested - r.used} tok" for r in rigid_overflow)
            mark = f"⚠️ 上下文预算超限：以下刚性内容已降级 [{marks}]"
            system_text = mark if not system_text else mark + _SEP + system_text

        return Assembly(system_text=system_text, prior=prior, report=report)


__all__ = ["ContextGovernor"]
