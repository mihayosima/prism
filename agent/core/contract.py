"""能力接缝契约：模型接缝 LlmProvider 与 ReAct 结果最小消费面 ReactResultLike。"""

from __future__ import annotations

from typing import Any, Protocol


class LlmProvider(Protocol):
    """模型接缝：complete 返回结构化 dict，失败一律 raise；tools 为 native 工具调用的加法扩展。"""

    interface_version = (1, 0)

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None,
                       tools: list[dict] | None = None) -> dict: ...


class ReactResultLike(Protocol):
    """chat 消费 ReAct 运行结果的最小成员面。"""

    ok: bool
    text: str
    reason: str | None
    tokens_in: int
    tokens_out: int

    def as_evidence(self) -> dict: ...


__all__ = ["LlmProvider", "ReactResultLike"]
