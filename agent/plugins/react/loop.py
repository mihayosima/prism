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

S3（循环内上下文治理）
----------------------
循环内 Think/Act/Observe 是**任务级草稿**（``TaskContext.chain``）——**不写入**
``agent_messages``、不进工作记忆、不写 Redis；循环结束只把终局文本交 chat 落库。

窗口构造拆成两层，**职责不重叠**（详见 docs/governance/S3-循环内上下文-设计方案.md §3.6）：

========================  ==============================================  ==============
层                        做什么                                          丢条目？
``_prepare_chain``（本插件）  块内准备：老观察**逐条首尾折叠**（只折叠）          否
``_trim_messages``（治理器）   预算裁剪：**整块丢弃** + 末条保底折叠              是
========================  ==============================================  ==============

- 预算口径 ``react_budget = input_budget() - prior_tokens``（**可由负**：prior 超预算时
  由 chat 调度层的预算闸先压缩一次再重建，见 ``sys/chat._ensure_react_headroom``）；
- ``prior`` 以 **rigid** 入块（不参与常规裁剪）；chain 为可裁块；
- 条目元数据：``type``（``think``/``act``/``observe``）是**本插件私有语义**；
  ``pinned`` 是**通用元数据**，交给治理器识别（块内先占额度）；
- 治理器缺失 / 无 ``assemble`` 接缝 → 退回旧 ``_trim`` 条数裁剪，行为不变（不劣化）。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from agent.api import (CapabilityNotImplemented, Events, Services, StateKeys,
                     durable, now_iso)

logger = logging.getLogger("agent.react")

from .components import Actor, Finalizer, Observer, Reasoner
from .prompt import build_system


@dataclass(slots=True)
class ReactConfig:
    """配置（``config["agent"]["react"]``）。"""

    max_steps: int = 8                 # 循环上限（步数闸）
    deadline_s: float = 120.0          # 整任务时延上限（时延闸）
    tool_result_max_chars: int = 2000  # 单条工具结果截断（上下文治理）
    history_keep: int = 20             # 喂给模型的最近消息条数（**降级路径**裁剪口径）
    max_rejects: int = 3               # 同一工具被拒多少次即终止（防死循环）
    finalize_on_exceed: bool = True    # 触顶时再答一次（不给工具）
    # ---- S3 循环内上下文：**兜底默认**（治理器缺该方法时才用到） ----
    # 注：折叠**阈值**（单条上限 / 头尾保留量）归治理器（``clip_*``），本插件不持该策略；
    #     治理器无 ``clip_head_tail`` 接缝 → 不做折叠（与「无治理器退旧路径」一致）。
    react_keep_recent: int = 6         # 最近 N 条链原文保留，更老者才折叠


@dataclass(slots=True)
class TaskContext:
    """循环内任务视图（**不持久化**；任务结束即弃）。

    职责边界（S3 钉死，防双份真相）：
    - ``prior``：入口已装配的会话上下文——**rigid**，循环内不参与常规裁剪；
    - ``chain``：**仅**循环产生的 Think/Act/Observe——任务级草稿，**不进** ``agent_messages``、
      不进工作记忆、不写 Redis；
    - ``task_id``：工作记忆键的**引用**（非副本）——进度态 SSOT 仍在
      ``plugins.work_memory``，此处**不自持进度字段**。

    条目元数据归属：
    - ``type``（``think``/``act``/``observe``）= **本插件私有语义**，供 ``_prepare_chain`` 筛选；
    - ``pinned`` = **通用元数据**，交给治理器识别（块内先占额度），本插件只负责打标。
    """

    prior: list[dict] = field(default_factory=list)
    chain: list[dict] = field(default_factory=list)
    task_id: str = ""
    step: int = 0


def _gov_int(gov, name: str, default: int) -> int:
    """从治理器取整型配置（缺方法/异常 → 回落本插件兜底默认，不劣化）。"""
    fn = getattr(gov, name, None)
    if not callable(fn):
        return int(default)
    try:
        return int(fn())
    except Exception:      # noqa: BLE001 —— 预算查询异常不阻断循环
        return int(default)


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
        system_prompt = build_system(tools, ctx.get(Services.SYSTEM_MEMORY) or "")

        # 工作记忆（第 3 层）：任务级进度缓存。接缝获取，缺失则跳过（不破解耦）。
        wm = ctx.get(Services.WORK_MEMORY)
        run_id = uuid.uuid4().hex
        task_id = f"{session_id}:{run_id}"
        if wm is not None:
            await wm.begin(task_id, {"session_id": session_id, "run_id": run_id,
                                     "max_steps": cfg.max_steps})

        # 任务级视图（S3）：prior = 入口已装配上下文（rigid，循环内不重裁）；
        # chain = **仅**循环产生的 Think/Act/Observe（草稿，循环结束即弃）。
        task = TaskContext(prior=[dict(m) for m in (prior or [])], task_id=task_id)
        # 消息全集：供 finalize 与「无治理器」降级裁剪使用；**绝不写入 agent_messages**。
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(task.prior)
        messages.append({"role": "user", "content": prompt})
        window: list[dict] = messages          # 最近一次喂给模型的窗口（治理后）

        steps = 0
        tokens_in = tokens_out = 0
        rejected: dict[str, int] = defaultdict(int)
        tool_trace: list[str] = []
        observed: list[dict] = []
        chain_peak = 0                         # 循环内 chain 实占峰值（可观测）
        chain_clips = 0                        # 累计逐条折叠次数（可观测）
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

            # ---- 窗口构造（S3：块内准备 → 治理器装配；无治理器退降级） ----
            window, win = self._window(ctx, task, system_prompt, prompt, messages, steps)
            if win:
                rt = win.get("react", {})
                chain_clips += int(rt.get("clips", 0))
                chain_peak = max(chain_peak, int(rt.get("chain_tokens", 0)))
                rt["clips"], rt["chain_peak_tokens"] = chain_clips, chain_peak

            # ---- Think ----
            try:
                out = await self._reasoner.reason(ctx, window, tools)
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
            turn = {"role": "assistant", "content": out.text,
                    "tool_calls": [c.as_dict() for c in out.tool_calls],
                    "type": "act" if out.tool_calls else "think",
                    "id": f"step{steps}_{'act' if out.tool_calls else 'think'}"}
            messages.append(turn)
            task.chain.append(turn)

            if not out.tool_calls:                       # ← 收敛：不再需要工具
                await durable(ctx, Events.REACT_FINAL, {
                    "steps": steps, "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "at": now_iso()})
                self._record(ctx, steps, tool_trace, exhausted=False)
                if wm is not None:
                    await wm.finish(task_id)
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
            obs = dict(await self._observer.observe(ctx, results))
            obs.setdefault("type", "observe")
            obs.setdefault("id", f"step{steps}_observe")
            obs.setdefault("pinned", False)          # 通用元数据：默认 False（可由 Observer 接缝注入）
            messages.append(obs)
            task.chain.append(obs)
            await durable(ctx, Events.REACT_STEP, {
                "step": steps, "tools": [r.name for r in results],
                "is_error": any(r.is_error for r in results),
                "tokens_in": tokens_in, "tokens_out": tokens_out, "at": now_iso()})
            await ctx.events.emit(Events.REACT_STEP_END, {
                "step": steps, "tools": [r.name for r in results],
                "is_error": any(r.is_error for r in results)})
            await self._emit_window(ctx, win)
            # 工作记忆：每步进度更新 + 里程碑检查点（tool_trace 仅工具名、已限 10，体积小）
            if wm is not None:
                last = tool_trace[-1] if tool_trace else ""
                await wm.update(task_id, step=steps, tools=list(tool_trace), last_action=last)
                await wm.checkpoint(task_id, f"step_{steps}",
                                    {"tool_trace": list(tool_trace[-10:]),
                                     "last_action": last, "note": "step completed"})
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
                text = await self._finalizer.finalize(ctx, window or messages, reason)
            except Exception:                            # noqa: BLE001 —— 收尾失败也要有话说
                text = f"（已达执行上限：{reason}）"
        else:
            text = f"（已达执行上限：{reason}）"
        self._record(ctx, steps, tool_trace, exhausted=True, reason=reason)
        if wm is not None:
            await wm.finish(task_id)
        return ReactResult(ok=not hard, text=text, steps=steps, tokens_in=tokens_in,
                           tokens_out=tokens_out, exhausted=True, reason=reason,
                           tools=tool_trace, observations=observed)

    # ---------- 循环内上下文（S3：块内准备 → 治理器装配） ----------

    def _prepare_chain(self, gov, chain: list[dict], keep_recent: int) -> tuple[list[dict], int]:
        """**块内准备：只折叠、不丢弃**（与治理器 `_trim_messages` 的丢弃职责不重叠）。

        - 最近 ``keep_recent`` 条**原样保留**；
        - 更老者中 ``type == "observe"`` 且**未 pinned** 的条目 → 逐条首尾折叠（就地生效，幂等）；
        - ``think`` / ``act`` **一律保留**——关键信息通常在 Think 里已转录；
        - **不丢任何条目**：超预算后的丢弃由治理器统一执行（避免双重截断）。

        返回 ``(本步待装配的 chain, 本步新发生折叠的条目数)``。
        """
        clip_fn = getattr(gov, "clip_head_tail", None)
        if not callable(clip_fn):
            return list(chain), 0
        cut = max(0, len(chain) - max(0, int(keep_recent)))
        newly = 0
        for i, m in enumerate(chain):
            if i >= cut or m.get("type") != "observe" or m.get("pinned"):
                continue
            content = m.get("content")
            folded, changed = self._fold_content(content, clip_fn)
            if changed:
                m["content"] = folded
                newly += 1
        return list(chain), newly

    @staticmethod
    def _fold_content(content, clip_fn) -> tuple[Any, bool]:
        """对条目内容做首尾折叠（**保结构**）：观察内容是 ``{"tool_results": [...]}`` 时，
        逐条折叠其 ``content`` 字段——**不把整个 dict 序列化成字符串**（否则 role=tool
        配对会被破坏）。纯字符串内容直接折叠。"""
        if isinstance(content, str):
            return clip_fn(content)
        if isinstance(content, dict):
            trs = content.get("tool_results")
            if isinstance(trs, list):
                changed = False
                new: list[Any] = []
                for tr in trs:
                    if isinstance(tr, dict) and isinstance(tr.get("content"), str):
                        text, cut = clip_fn(tr["content"])
                        if cut:
                            tr = {**tr, "content": text}
                            changed = True
                    new.append(tr)
                if changed:
                    return {**content, "tool_results": new}, True
        return content, False

    def _window(self, ctx, task: TaskContext, system_prompt: str, prompt: str,
                messages: list[dict], step: int) -> tuple[list[dict], dict]:
        """构造喂给模型的窗口（G1 装配；无治理器 → 退回旧 ``_trim`` 条数裁剪，行为不变）。

        块列表（注册制）：system / prior（rigid）/ 本轮 prompt（rigid）/ chain（可裁）。
        prior 与 prompt 为**刚性块**——循环内不参与常规裁剪（自身超预算时治理器仍会
        按刚性降级规则诚实标注，不静默）。
        """
        gov = ctx.get(Services.CONTEXT) if ctx.has(Services.CONTEXT) else None
        fn = getattr(gov, "assemble", None) if gov is not None else None
        if not callable(fn):
            return _trim(messages, self._cfg.history_keep), {}
        keep_recent = _gov_int(gov, "react_keep_recent", self._cfg.react_keep_recent)
        prepared, newly = self._prepare_chain(gov, task.chain, keep_recent)
        blocks = [
            {"key": "react_system", "kind": "text", "present": True,
             "payload": system_prompt, "rigid": True, "priority": -10},
            {"key": "react_prior", "kind": "messages", "present": bool(task.prior),
             "payload": task.prior, "rigid": True, "priority": 0, "coverage": (0, 1)},
            {"key": "react_prompt", "kind": "messages", "present": True,
             "payload": [{"role": "user", "content": prompt}], "rigid": True,
             "priority": 5, "coverage": (1, 2)},
            {"key": "react_chain", "kind": "messages", "present": bool(prepared),
             "payload": prepared, "weight": 1.0, "priority": 10, "coverage": (2, 3)},
        ]
        try:
            asm = fn(blocks)
        except Exception as exc:                     # noqa: BLE001 —— 装配失败退降级，绝不裸崩
            logger.warning("react 窗口装配失败，退回条数裁剪：%s", exc)
            return _trim(messages, self._cfg.history_keep), {}
        window = [{"role": "system", "content": asm.system_text}] + list(asm.prior)
        return window, self._win_stats(asm, prepared, newly, step, keep_recent)

    @staticmethod
    def _win_stats(asm, prepared: list[dict], newly_clipped: int, step: int,
                   keep_recent: int) -> dict:
        """装配统计（遥测用；字段名与 S3 设计 §3.6.6 一致）。"""
        rep = getattr(asm, "report", None)
        blocks: list[dict] = []
        chain_tokens = chain_alloc = downgraded = 0
        for r in getattr(rep, "blocks", []):
            blocks.append({"key": r.key, "present": r.present, "reason": r.reason,
                           "requested": r.requested, "allocated": r.allocated, "used": r.used})
            if r.key == "react_chain":
                chain_tokens = int(r.used)
                chain_alloc = int(r.allocated)
                downgraded = len(getattr(r, "pinned_downgraded", []) or [])
        return {
            "step": step,
            "input_budget": int(getattr(rep, "input_budget", 0)),
            "total_used": int(getattr(rep, "total_used", 0)),
            "blocks": blocks,
            "rigid_overflow": [r.key for r in getattr(rep, "rigid_overflow", [])],
            "react": {
                "chain_tokens": chain_tokens,
                "chain_peak_tokens": chain_tokens,       # 由 run() 累计为任务内峰值
                "clips": newly_clipped,                  # 由 run() 累计
                "pinned": sum(1 for m in prepared if m.get("pinned")),
                "pinned_downgraded": downgraded,
                "keep_recent": int(keep_recent),
                "budget": chain_alloc,
            },
        }

    async def _emit_window(self, ctx, stats: dict) -> None:
        """发 ``agent/context/governed``（``stage="react"``）遥测；失败绝不阻断循环。"""
        if not stats:
            return
        try:
            await ctx.events.emit(Events.CONTEXT_GOVERNED, {
                "stage": "react", "at": now_iso(), **stats})
        except Exception as exc:                     # noqa: BLE001 —— 遥测失败不阻断
            logger.warning("react context governed emit failed: %s", exc)

    # ---------- 诊断（status() 钩子取用） ----------

    def _record(self, ctx, steps: int, tools: list[str], *, exhausted: bool,
                reason: str | None = None) -> None:
        try:
            ctx.state[StateKeys.REACT_LAST] = {"steps": steps, "tools": list(tools[-10:]),
                                               "exhausted": exhausted, "reason": reason,
                                               "at": now_iso()}
        except Exception:                                # noqa: BLE001 —— 诊断不阻断主链路
            pass


__all__ = ["ReactConfig", "ReactResult", "ReactLoop", "TaskContext", "visible_tools"]
