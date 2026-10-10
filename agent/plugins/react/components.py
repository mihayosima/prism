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

from .prompt import FINAL_SYSTEM, as_text


@dataclass(slots=True)
class ToolCall:
    """模型请求的一次工具调用。"""

    name: str
    arguments: dict = field(default_factory=dict)
    id: str = ""
    truncated: bool = False        # E：参数 JSON 被 max_tokens 腰斩（残缺），不可直接执行
    arguments_raw: str = ""        # E：残缺参数原始片段（供错误回灌自纠）

    @classmethod
    def from_dict(cls, d: dict) -> "ToolCall":
        args = d.get("arguments")
        return cls(name=str(d.get("name") or ""),
                   arguments=args if isinstance(args, dict) else {},
                   id=str(d.get("id") or ""),
                   truncated=bool(d.get("truncated")),
                   arguments_raw=str(d.get("arguments_raw") or ""))

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "arguments": self.arguments}


@dataclass(slots=True)
class ToolResult:
    """一次工具调用的观察结果（**永不抛出**，一律回灌给模型自纠）。"""

    name: str
    content: str
    is_error: bool = False
    denied: bool = False          # 被 guard/预算拒绝（与"工具本身出错"区分，用于防死循环）
    denied_by: str = ""           # 拒绝来源（第 3 期）："user"=用户本轮主动拒（不计连拒）
    tool_call_id: str = ""

    def as_dict(self) -> dict:
        return {"tool_call_id": self.tool_call_id, "name": self.name,
                "content": self.content, "is_error": self.is_error, "denied": self.denied,
                "denied_by": self.denied_by}


@dataclass(slots=True)
class ReasonOutput:
    """一轮 Think 的产出：最终答复文本 或 工具调用列表（二者其一非空）。

    ``reasoning_content``（B-fix）：thinking 模式的思考链原样随行——回灌历史时
    DeepSeek 要求「输出过思考的 assistant 消息回传必须携带」，故须采集→存档→wire 回传。
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning_content: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    truncated: bool = False      # 下游截断信号（finish_reason=length）：reasoning 吃满 output 额度、正文被挤空


class Reasoner(Protocol):
    async def reason(self, ctx, messages: list[dict], tools: list[dict]) -> ReasonOutput: ...


class Actor(Protocol):
    async def act(self, ctx, calls: list[ToolCall]) -> list[ToolResult]: ...


class Observer(Protocol):
    """观察接缝：把本轮工具结果整理成一条回灌消息。

    返回的 dict 可携带以下**可选**元数据（循环会自动补齐缺省值）：

    - ``pinned: bool``（默认 ``False``）——**通用元数据**：交给上下文治理器识别，
      标 ``True`` 的条目在块内**先占额度**（不被折叠/丢弃）；实现方按业务判定标注意要观察即可；
    - ``type`` / ``id`` 由循环统一补齐（``act``/``observe`` 等属**本插件私有语义**，
      治理器不感知）。
    """

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

    async def reason(self, ctx, messages, tools, *,
                     max_tokens: int | None = None,
                     system_note: str | None = None) -> ReasonOutput:
        msgs = list(messages)
        if system_note:
            msgs = [{"role": "system", "content": system_note}] + msgs
        lr = await ctx.llm.complete(ctx, msgs, tools=tools, caller=self._caller,
                                    max_tokens=max_tokens)
        data = lr.data if isinstance(lr.data, dict) else {}
        calls = [ToolCall.from_dict(c) for c in (data.get("tool_calls") or [])
                 if isinstance(c, dict)]
        return ReasonOutput(text=str(data.get("text") or ""), tool_calls=calls,
                            reasoning_content=str(data.get("reasoning_content") or ""),
                            tokens_in=int(lr.tokens_in or 0),
                            tokens_out=int(lr.tokens_out or 0),
                            truncated=bool(data.get("truncated")))


class DefaultActor:
    """默认执行：逐个经 ``ctx.tasks.invoke``；异常一律转观察，不中断循环。

    - ``CapabilityNotImplemented``（guard/预算拒绝）→ ``denied=True`` 的 error 观察；
    - ``AgentError``（参数错 / 工具不存在）→ error 观察；
    - 其它异常（工具内部 bug）→ error 观察（不让一次工具崩溃掀翻整轮）。

    **结果体积上限归源头护栏**（S4）：工具结果在 ``ctx.tasks.invoke`` 内即被
    ``guard_source`` 按 ``context.source_clip_budget()`` 裁到 token 上限，
    故本类**不再做字符截断**（旧 ``max_chars=2000`` 字符口径已删——字符与 token
    在 CJK/ASCII 下差 3 倍，且与治理器口径互相打架）。此处只做 ``as_text`` 转文本。
    """

    def __init__(self, *, caller: str = "plugins.react"):
        self._caller = caller

    async def act(self, ctx, calls, *, timeout_s: float | None = None) -> list[ToolResult]:
        """``timeout_s``（H3）：本次 act 的**剩余预算**，逐个下压给 ``tasks.invoke``。

        没有它时，卡死的工具会让 ``await`` 永不返回，而 run 的 deadline 只在
        **循环顶**检查 ⇒ 永远到不了那个检查点 ⇒ 整个 run 永久挂住。
        ``None`` = 不限时（默认，既有行为不变）。
        """
        return [await self._one(ctx, c, timeout_s) for c in calls]

    async def _one(self, ctx, call: ToolCall,
                   timeout_s: float | None = None) -> ToolResult:
        if getattr(call, "truncated", False):
            # E（方案）：生成侧截断的调用**不执行**——半截 JSON 已被 _wire_call 拒析，
            # 强行 invoke 只会得到「缺少必填参数」的误导报错。此处如实回灌自纠指引。
            raw = (getattr(call, "arguments_raw", "") or "")[:300]
            return ToolResult(
                call.name,
                "模型输出被截断（finish_reason=length，触及 max_tokens 生成上限），"
                "工具参数 JSON 不完整，本次调用未执行。请减小单次输出规模："
                "把大内容分片（如每章拆 2-3 段、多次追加写入）后重试。"
                f"残缺参数片段：{raw}",
                is_error=True, tool_call_id=call.id)
        try:
            result = await ctx.tasks.invoke(ctx, call.name, caller=self._caller,
                                            timeout_s=timeout_s,
                                            **dict(call.arguments or {}))
        except CapabilityNotImplemented as exc:
            return ToolResult(call.name, f"被拒绝：{exc}", is_error=True, denied=True,
                              denied_by=str(getattr(exc, "denied_by", "") or ""),
                              tool_call_id=call.id)
        except AgentError as exc:
            return ToolResult(call.name, f"出错：{exc}", is_error=True, tool_call_id=call.id)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(call.name, f"异常：{type(exc).__name__}: {exc}", is_error=True,
                              tool_call_id=call.id)
        return ToolResult(call.name, as_text(result), tool_call_id=call.id)


class DefaultObserver:
    """默认观察：把结果整理成一条回灌消息（纯 dict，可 JSON 化 —— 与事件面同纪律）。

    ``pinned`` 默认 ``False``（**不自动判定**——原方案未定义判定规则，属伪需求；
    需要时由替换实现按业务标注，见 ``Observer`` 协议）。
    """

    async def observe(self, ctx, results) -> dict:
        return {"role": "user", "content": {"tool_results": [r.as_dict() for r in results]},
                "pinned": False}


class DefaultFinalizer:
    """默认收尾：再问一次模型（不给工具）；失败则退回**证据兜底文本**（诚实，不假成功）。"""

    def __init__(self, *, caller: str = "plugins.react"):
        self._caller = caller

    async def finalize(self, ctx, messages, reason: str) -> str:
        # finalize=True（方案A）：budget 据此允许本调用动用收尾预留额度——
        # 正常步骤已在 max-reserve 处被拒，收尾不加此标记则同样被拒（死结）。
        probe = list(messages) + [{"role": "system", "content": FINAL_SYSTEM}]
        try:
            lr = await ctx.llm.complete(ctx, probe, caller=self._caller, finalize=True)
            text = str(((lr.data or {}) if isinstance(lr.data, dict) else {}).get("text") or "")
            if text.strip():
                return text.strip()
        except Exception:  # noqa: BLE001 —— 收尾失败不崩：退回兜底
            pass
        rounds = sum(1 for m in messages
                     if m.get("role") == "assistant" and m.get("tool_calls"))
        # C：兜底文本带上收尾摘要（loop 以 [收尾摘要] 系统消息注入 messages）
        summary = next((str(m.get("content") or "") for m in messages
                        if isinstance(m, dict) and m.get("role") == "system"
                        and str(m.get("content", "")).startswith("[收尾摘要]")), "")
        base = (f"（已达执行上限：{reason}；已执行 {rounds} 轮工具调用，"
                f"未能形成最终答复）")
        return f"{base}\n{summary}" if summary else base


__all__ = ["ToolCall", "ToolResult", "ReasonOutput",
           "Reasoner", "Actor", "Observer", "Finalizer",
           "DefaultReasoner", "DefaultActor", "DefaultObserver", "DefaultFinalizer"]
