"""agent.core.contract —— 能力接缝契约（零业务词，test_agent_purity 守卫）。

- ``LlmProvider``: 模型接缝，方法与业务语义解耦（structured result 由调用方定）。
"""

from __future__ import annotations

from typing import Any, Protocol


class LlmProvider(Protocol):
    """模型接缝：complete → 结构化 dict（schema 约束输出）。重试/预算在调用方 seam。

    ``tools`` 是**加法扩展**（native 工具调用）：传 None 即旧行为（现有调用方不传，不变）；
    支持时返回 ``{"text": str, "tool_calls": [{"id","name","arguments"}]}``；
    不支持 native 工具调用的 provider 应诚实拒绝（CapabilityNotImplemented），不得用文本约定假装。
    """

    interface_version = (1, 0)

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None,
                       tools: list[dict] | None = None) -> dict: ...


__all__ = ["LlmProvider"]