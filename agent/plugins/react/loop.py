"""plugins.react · 主循环（Think → Act+Observe → 收敛）+ 三把闸。

循环形状取自 ``loong-agent``（PyPI 0.0.2）：每轮 Think；模型**不再返回工具调用**即为
最终答复；有则 Act+Observe，把结果回灌后进入下一轮。相比 ``LoongFlow`` 的
"final-answer-as-tool"，它与 function-calling 天然契合，也更贴本仓库的接缝模型。

补上 loong-agent 缺的三把闸：

===================  =========================================  ============================
闸                   位置                                        行为
===================  =========================================  ============================
``max_steps``        循环条件（配置，默认 8）                      触顶 → 统一收尾
``deadline_s``       每轮开头检查（配置，默认 120s）               触顶 → 统一收尾
token 预算           **不用写**：每步都走 ``ctx.llm``，           被拒（CapabilityNotImplemented）
                     ``agent/llm/pre-call`` 上 guard/budget 已生效  → 转"触顶"收尾（不崩）
===================  =========================================  ============================

收尾一律**诚实标注**：durable ``agent/react/exhausted`` + 文本如实说明（不静默失败、
不假装成功）。
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field

from agent.api import CapabilityNotImplemented, Events, StateKeys, durable, now_iso

logger = logging.getLogger("agent.react")

from .components import Actor, Finalizer, Observer, Reasoner
from .prompt import build_system


@dataclass(slots=True)
class ReactConfig:
    """配置（``config["agent"]["react"]``）。"""

    max_steps: int = 8                 # 循环上限（步数闸）
    deadline_s: float = 120.0          # 整任务时延上限（时延闸）
    tool_result_max_chars: int = 2000  # 单条工具结果截断（上下文治理）
    history_keep: int = 20             # 喂给模型的最近消息条数（历史裁剪）
    max_rejects: int = 3               # 同一工具被拒多少次即终止（防死循环）
    finalize_on_exceed: bool = True    # 触顶时再答一次（不给工具）


@dataclass(slots=True)
class ReactResult:
    """一次 run 的结果。``ok`` 只表示"是否给出了答复"；``exhausted`` 表示"是否被闸截停"。"""

    ok: bool
    text: str
    steps: int
    tokens_in: int = 0
    tokens_out: int = 0
    exhausted: bool = False
    reason: str | None = None
    tools: list[str] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)

    def as_evidence(self) -> dict:
        """机器证据（供 chat 转写 / 前端展示；纯数据）。"""
        return {"intent": "react", "ok": self.ok, "steps": self.steps,
                "exhausted": self.exhausted, "reason": self.reason,
                "tools": list(self.tools), "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "observations": list(self.observations)}


def visible_tools(ctx) -> list[dict]:
    """工具可见集（权限模式 **接口预留**）。

    本次**恒返回全量**（锁住"行为不变"）。将来 approval 插件落地后，``plan`` 模式在此
    裁为只读子集即可（``Tool.risk`` 字段已就位）：

        if ctx.state.get("permission_mode") == "plan":
            return [t for t in catalog if t.get("risk") == "read"]
    """
    return ctx.tasks.catalog()


def _trim(messages: list[dict], keep: int) -> list[dict]:
    """历史裁剪：保 system + 首个用户请求 + 最近 ``keep`` 条（多步循环的上下文治理）。

    只裁剪**喂给模型的窗口**，不丢循环自身的状态（消息全集由调用方持有）。
    """
    if keep <= 0 or len(messages) <= keep + 2:
        return messages
    first_user = next((i for i, m in enumerate(messages)
                       if m.get("role") == "user" and isinstance(m.get("content"), str)), None)
    cut = len(messages) - keep
    idx = {0, *range(cut, len(messages))}
    if first_user is not None:
        idx.add(first_user)
    return [m for i, m in enumerate(messages) if i in idx]


class ReactLoop:
    """ctx.react —— 多步工具循环（四组件可换；三把闸）。"""

    def __init__(self, reasoner: Reasoner, actor: Actor, observer: Observer,
                 finalizer: Finalizer, cfg: ReactConfig):
        self._reasoner = reasoner
        self._actor = actor
        self._observer = observer
        self._finalizer = finalizer
        self._cfg = cfg

    @property
    def config(self) -> ReactConfig:
        return self._cfg

    def visible_tools(self, ctx) -> list[dict]:
        """当前工具可见集（读 ``permission_mode``；本次恒全量 —— 见模块级 ``visible_tools``）。"""
        return visible_tools(ctx)

    async def run(self, ctx, prompt: str, *, session_id: str = "default",
                  prior: list[dict] | None = None) -> ReactResult:
        cfg = self._cfg
        await durable(ctx, Events.REACT_START, {
            "prompt": (prompt or "")[:500], "session_id": session_id,
            "max_steps": cfg.max_steps, "at": now_iso()})

        tools = visible_tools(ctx)
        messages: list[dict] = [{"role": "system", "content": build_system(tools)}]
        messages.extend(dict(m) for m in (prior or []))
        messages.append({"role": "user", "content": prompt})

        steps = 0
        tokens_in = tokens_out = 0
        rejected: dict[str, int] = defaultdict(int)
        tool_trace: list[str] = []
        observed: list[dict] = []
        started = time.monotonic()

        reason: str | None = None
        hard = False

        while steps < cfg.max_steps:
            if time.monotonic() - started > cfg.deadline_s:
                reason = f"超时（超过 {cfg.deadline_s:.0f}s）"
                break
            steps += 1
            await ctx.events.emit(Events.REACT_STEP_START,
                                  {"step": steps, "session_id": session_id})

            # ---- Think ----
            try:
                out = await self._reasoner.reason(ctx, _trim(messages, cfg.history_keep), tools)
            except CapabilityNotImplemented as exc:      # guard/预算拒绝 → 触顶收尾，不崩
                reason = f"模型调用被拒：{exc}"
                break
            except Exception as exc:                     # noqa: BLE001 —— 推理失败：诚实失败
                logger.exception("react 推理失败（step=%s，完整堆栈如下）", steps)
                reason = f"推理失败：{type(exc).__name__}: {exc}"
                hard = True
                break
            tokens_in += int(out.tokens_in or 0)
            tokens_out += int(out.tokens_out or 0)
            messages.append({"role": "assistant", "content": out.text,
                             "tool_calls": [c.as_dict() for c in out.tool_calls]})

            if not out.tool_calls:                       # ← 收敛：不再需要工具
                await durable(ctx, Events.REACT_FINAL, {
                    "steps": steps, "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "at": now_iso()})
                self._record(ctx, steps, tool_trace, exhausted=False)
                return ReactResult(ok=True, text=out.text or "（未给出回答）", steps=steps,
                                   tokens_in=tokens_in, tokens_out=tokens_out,
                                   tools=tool_trace, observations=observed)

            # ---- Act + Observe ----
            results = await self._actor.act(ctx, out.tool_calls)
            stop_reason: str | None = None
            for r in results:
                tool_trace.append(r.name)
                observed.append(r.as_dict())
                if r.denied:                             # 同一工具反复被拒 → 终止（防死循环）
                    rejected[r.name] += 1
                    if rejected[r.name] > cfg.max_rejects:
                        stop_reason = (f"工具 {r.name!r} 反复被拒"
                                       f"（超过 {cfg.max_rejects} 次），终止")
            messages.append(await self._observer.observe(ctx, results))
            await durable(ctx, Events.REACT_STEP, {
                "step": steps, "tools": [r.name for r in results],
                "is_error": any(r.is_error for r in results),
                "tokens_in": tokens_in, "tokens_out": tokens_out, "at": now_iso()})
            await ctx.events.emit(Events.REACT_STEP_END, {
                "step": steps, "tools": [r.name for r in results],
                "is_error": any(r.is_error for r in results)})
            if stop_reason:
                reason = stop_reason
                break
        else:
            reason = f"已达步数上限 max_steps={cfg.max_steps}"

        # ---- 触顶统一收尾：不崩、不静默，如实标注 ----
        reason = reason or f"已达步数上限 max_steps={cfg.max_steps}"
        await durable(ctx, Events.REACT_EXHAUSTED, {
            "steps": steps, "reason": reason, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "at": now_iso()})
        if cfg.finalize_on_exceed and not hard:
            try:
                text = await self._finalizer.finalize(ctx, messages, reason)
            except Exception:                            # noqa: BLE001 —— 收尾失败也要有话说
                text = f"（已达执行上限：{reason}）"
        else:
            text = f"（已达执行上限：{reason}）"
        self._record(ctx, steps, tool_trace, exhausted=True, reason=reason)
        return ReactResult(ok=not hard, text=text, steps=steps, tokens_in=tokens_in,
                           tokens_out=tokens_out, exhausted=True, reason=reason,
                           tools=tool_trace, observations=observed)

    # ---------- 诊断（status() 钩子取用） ----------

    def _record(self, ctx, steps: int, tools: list[str], *, exhausted: bool,
                reason: str | None = None) -> None:
        try:
            ctx.state[StateKeys.REACT_LAST] = {"steps": steps, "tools": list(tools[-10:]),
                                               "exhausted": exhausted, "reason": reason,
                                               "at": now_iso()}
        except Exception:                                # noqa: BLE001 —— 诊断不阻断主链路
            pass


__all__ = ["ReactConfig", "ReactResult", "ReactLoop", "visible_tools"]
