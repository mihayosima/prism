"""agent.plugins.context.protocols —— 治理器接缝契约（G0 固化，D2 同款）。

``ctx.get("context.governor")`` 注入的实现须满足本协议；契约留在插件内
（与 memory 三组件同构），Driver/消费方只依赖协议面，不依赖默认实现。
"""

from __future__ import annotations

from typing import Protocol


class GovernorLike(Protocol):
    """上下文治理器契约：预算查询 + 历史收窗（G0 面；随 G1–G5 接线扩面）。"""

    def input_budget(self) -> int: ...

    def memory_budget(self) -> int: ...

    def history_budget(self) -> int: ...

    def source_clip_budget(self) -> int: ...

    def pick_history(
        self,
        messages: list[dict],
        budget: int | None = None,
        *,
        keep_first: bool = False,
    ) -> tuple[list[dict], int]: ...


__all__ = ["GovernorLike"]
