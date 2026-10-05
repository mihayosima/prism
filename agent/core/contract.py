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

    语义契约（D1 固化，由 tests/test_llm_contract.py 锁定，无运行期开销）：
    - **结果与 stream 配置无关**：同一 (messages, schema, tools) 在 ``stream=True/False``
      下必须返回等价结构——记忆提取（schema 调用走流式配置）曾因流式分支不累积
      content 而全量失败（aa39fb4 修复），本条防复发；
    - ``schema is not None`` → 返回 dict 必须是已解析的 JSON 对象（provider 内部完成
      ``_parse_json`` 语义），调用方无需二次解析；
    - ``tools is not None`` 且模型发起调用 → 返回含 ``tool_calls`` 列表（分片 delta
      必须归并完整：name/arguments 跨 chunk 拼接）；
    - 失败一律 **raise**（由 LlmService 统一发 ``agent/llm/call-failed`` 触发预算退款），
      不得返回 ``{"error": ...}`` 之类的伪成功。
    """

    interface_version = (1, 0)

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None,
                       tools: list[dict] | None = None) -> dict: ...


class ReactResultLike(Protocol):
    """chat 消费 ReAct 运行结果的最小面（D2 固化，运行期零开销）。

    ``sys/chat.py`` 委托 ``ctx.react.run(...)`` 后只消费以下成员——react 侧
    ``ReactResult`` dataclass 改字段名/删成员时，结构守卫测试（tests/test_memory_seams）
    即刻报红，不再等 chat 运行期才断。
    """

    ok: bool
    text: str
    reason: str | None
    tokens_in: int
    tokens_out: int

    def as_evidence(self) -> dict: ...


__all__ = ["LlmProvider", "ReactResultLike"]
