"""plugins.react · 四组件接缝（Reasoner / Actor / Observer / Finalizer）。

四者都是 **Protocol**，且都是「可换的接缝服务」：容器里若存在对应键
（``ctx.provide("react.reasoner", ...)``）就用注入实现，否则退回本文件的默认实现
—— 与 ``ctx.llm`` 的 provider 选择同构。

好处：将来 ``plugins.approval`` 只需替换 ``Actor``（在 ``act`` 里插入"问人"），
**不必改循环本身**；并行工具编排同理只换 ``Actor``。

零特权纪律：默认实现里，模型调用一律走 ``ctx.llm``，工具调用一律走 ``ctx.tasks.invoke``
⇒ guard 裁决 / schema 校验 / owner 审计 / 预算记账全部自动继承。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from agent.api import AgentError, CapabilityNotImplemented

from .prompt import FINAL_SYSTEM, truncate


@dataclass(slots=True)
class ToolCall:
    """模型请求的一次工具调用。"""

    name: str
    arguments: dict = field(default_factory=dict)
    id: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "ToolCall":
        args = d.get("arguments")
        return cls(name=str(d.get("name") or ""),
                   arguments=args if isinstance(args, dict) else {},
                   id=str(d.get("id") or ""))

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class ToolResult:
    """一次工具调用的观察结果（**永不抛出**，一律回灌给模型自纠）。"""

    name: str
    content: str
    is_error: bool = False
    denied: bool = False          # 被 guard/预算拒绝（与"工具本身出错"区分，用于防死循环）
    tool_call_id: str = ""

    def as_dict(self) -> dict:
        return {"tool_call_id": self.tool_call_id, "name": self.name,
                "content": self.content, "is_error": self.is_error, "denied": self.denied}


@dataclass(slots=True)
class ReasonOutput:
    """一轮 Think 的产出：最终答复文本 或 工具调用列表（二者其一非空）。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0


class Reasoner(Protocol):
    async def reason(self, ctx, messages: list[dict], tools: list[dict]) -> ReasonOutput: ...


class Actor(Protocol):
    async def act(self, ctx, calls: list[ToolCall]) -> list[ToolResult]: ...


class Observer(Protocol):
    async def observe(self, ctx, results: list[ToolResult]) -> dict: ...


class Finalizer(Protocol):
    async def finalize(self, ctx, messages: list[dict], reason: str) -> str: ...


# ---------------------------------------------------------------------------
# 默认实现（开箱可用；不支持 native 工具调用的 provider 会诚实报缺）
# ---------------------------------------------------------------------------


class DefaultReasoner:
    """默认推理：一步 ``ctx.llm.complete(..., tools=...)`` → 工具调用 或 最终答复。"""

    def __init__(self, *, caller: str = "plugins.react"):
        self._caller = caller

    async def reason(self, ctx, messages, tools) -> ReasonOutput:
        lr = await ctx.llm.complete(ctx, messages, tools=tools, caller=self._caller)
        data = lr.data if isinstance(lr.data, dict) else {}
        calls = [ToolCall.from_dict(c) for c in (data.get("tool_calls") or [])
                 if isinstance(c, dict)]
        return ReasonOutput(text=str(data.get("text") or ""), tool_calls=calls,
                            tokens_in=int(lr.tokens_in or 0),
                            tokens_out=int(lr.tokens_out or 0))


class DefaultActor:
    """默认执行：逐个经 ``ctx.tasks.invoke``；异常一律转观察，不中断循环。

    - ``CapabilityNotImplemented``（guard/预算拒绝）→ ``denied=True`` 的 error 观察；
    - ``AgentError``（参数错 / 工具不存在）→ error 观察；
    - 其它异常（工具内部 bug）→ error 观察（不让一次工具崩溃掀翻整轮）。
    """

    def __init__(self, *, max_chars: int = 2000, caller: str = "plugins.react"):
        self._max_chars = max_chars
        self._caller = caller

    async def act(self, ctx, calls) -> list[ToolResult]:
        return [await self._one(ctx, c) for c in calls]

    async def _one(self, ctx, call: ToolCall) -> ToolResult:
        try:
            result = await ctx.tasks.invoke(ctx, call.name, caller=self._caller,
                                            **dict(call.arguments or {}))
        except CapabilityNotImplemented as exc:
            return ToolResult(call.name, f"被拒绝：{exc}", is_error=True, denied=True,
                              tool_call_id=call.id)
        except AgentError as exc:
            return ToolResult(call.name, f"出错：{exc}", is_error=True, tool_call_id=call.id)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(call.name, f"异常：{type(exc).__name__}: {exc}", is_error=True,
                              tool_call_id=call.id)
        return ToolResult(call.name, truncate(result, self._max_chars), tool_call_id=call.id)


class DefaultObserver:
    """默认观察：把结果整理成一条回灌消息（纯 dict，可 JSON 化 —— 与事件面同纪律）。"""

    async def observe(self, ctx, results) -> dict:
        return {"role": "user", "content": {"tool_results": [r.as_dict() for r in results]}}


class DefaultFinalizer:
    """默认收尾：再问一次模型（不给工具）；失败则退回**证据兜底文本**（诚实，不假成功）。"""

    def __init__(self, *, caller: str = "plugins.react"):
        self._caller = caller

    async def finalize(self, ctx, messages, reason: str) -> str:
        probe = list(messages) + [{"role": "system", "content": FINAL_SYSTEM}]
        try:
            lr = await ctx.llm.complete(ctx, probe, caller=self._caller)
            text = str(((lr.data or {}) if isinstance(lr.data, dict) else {}).get("text") or "")
            if text.strip():
                return text.strip()
        except Exception:  # noqa: BLE001 —— 收尾失败不崩：退回兜底
            pass
        rounds = sum(1 for m in messages
                     if m.get("role") == "assistant" and m.get("tool_calls"))
        return (f"（已达执行上限：{reason}；已执行 {rounds} 轮工具调用，"
                f"未能形成最终答复）")


__all__ = ["ToolCall", "ToolResult", "ReasonOutput",
           "Reasoner", "Actor", "Observer", "Finalizer",
           "DefaultReasoner", "DefaultActor", "DefaultObserver", "DefaultFinalizer"]
