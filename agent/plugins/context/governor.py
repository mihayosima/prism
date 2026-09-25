"""agent.plugins.context.governor —— 上下文治理器（G0 骨架）。

预算模型：输入窗口总预算（``max_context_tokens - reserve_output_tokens``）按块分配——
  记忆块 ``memory_ratio`` · 历史窗口 ``history_ratio`` · 其余归 system/工具块/本轮消息。
裁剪一律按 token（``core.tokens.estimate_tokens``），不再按条数。

G0 只提供预算查询 + 历史收窗（``pick_history``）；分块装配（G1）、
会话压缩摘要（G1）、MCP 源头护栏（G3）按阶段接线，消费面全部软依赖
``ctx.has(Services.CONTEXT)``——插件停用即退回各消费方现状，不劣化不裸崩。
"""

from __future__ import annotations

from typing import Any

from agent.core.tokens import estimate_tokens


class ContextGovernor:
    """上下文治理器：预算模型 + token 化收窗（接缝默认实现，可注入替换）。"""

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

    # ---- 历史收窗 ----

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


__all__ = ["ContextGovernor"]
