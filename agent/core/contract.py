"""agent.core.contract —— 能力接缝契约（零业务词，test_agent_purity 守卫）。

- ``LlmProvider``: 模型接缝，方法与业务语义解耦（structured result 由调用方定）。
"""

from __future__ import annotations

from typing import Any, Protocol


class LlmProvider(Protocol):
    """模型接缝：complete → 结构化 dict（schema 约束输出）。重试/预算在调用方 seam。"""

    interface_version = (1, 0)

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None) -> dict: ...


__all__ = ["LlmProvider"]