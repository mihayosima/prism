"""agent.plugins.context.protocols —— 治理器接缝契约（G0 固化，G1 扩面）。

``ctx.get("context.governor")`` 注入的实现须满足本协议；契约留在插件内
（与 memory 三组件同构），Driver/消费方只依赖协议面，不依赖默认实现。

G1 新增**分块装配契约**（纯数据，chat 构造、governor 消费）：
``BlockSpec`` / ``BlockReport`` / ``AssemblyReport`` / ``Assembly``。
消费方不触插件内部实现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class BlockSpec:
    """一个可装配块（注册制：块自声明存在性，治理器动态组装）。

    ``coverage`` 语义（**一律「左开右闭」`(lo, hi]`**，全链统一）：
    - `messages` 块专用；**含 `hi`、不含 `lo`**；
    - 现行实例：`summary=(0, covers]`、`history=(covers, N]`，`covers` 取自
      `covers_up_to_seq`（SSOT）；任意两块区间两两不相交；
    - 该边界由 **chat 读取后作入参传入**（非 governor 反向查询），避免
      governor→chat/short_term 循环依赖。
    """

    key: str
    kind: str                                   # "text" | "messages"
    present: bool
    payload: str | list[dict] = ""
    weight: float = 0.0                         # 可裁块预算权重；<=0 → 用治理器按 key 的默认
    priority: int = 100                         # 越小越先保留（装配顺序 / 超限裁剪逆序）
    rigid: bool = False                         # True = 刚性（超预算也不裁，先占预算）
    coverage: tuple[int, int] | None = None     # messages 专用（左开右闭）


@dataclass(frozen=True)
class BlockReport:
    """单块装配结果（可观测）。"""

    key: str
    present: bool
    kind: str
    requested: int = 0                          # 块自报需求 token
    allocated: int = 0                          # 分配上限 token
    used: int = 0                               # 实占 token
    trimmed: bool = False                       # 是否被裁剪
    reason: str = ""                            # absent / empty / ok / trimmed / downgraded


@dataclass(frozen=True)
class AssemblyReport:
    """装配总报告（结构化 dataclass，非裸 dict）。"""

    blocks: list[BlockReport] = field(default_factory=list)
    total_used: int = 0
    input_budget: int = 0
    rigid_overflow: list[BlockReport] = field(default_factory=list)  # 刚性超限被降级者
    redistributed: int = 0                      # 回吐再分配的总 token


@dataclass(frozen=True)
class Assembly:
    """装配产物：system 文本 + prior 消息 + 报告。"""

    system_text: str
    prior: list[dict]
    report: AssemblyReport


class GovernorLike(Protocol):
    """上下文治理器契约：预算查询 + 历史收窗 + 分块装配（G1）。"""

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

    def assemble(self, blocks: list[BlockSpec]) -> Assembly: ...


__all__ = [
    "BlockSpec",
    "BlockReport",
    "AssemblyReport",
    "Assembly",
    "GovernorLike",
]
