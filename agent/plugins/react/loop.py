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

import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from agent.api import (CapabilityNotImplemented, Events, Services, StateKeys,
                      durable, estimate_request_cost, estimate_tokens, now_iso)

logger = logging.getLogger("agent.react")

from .components import Actor, Finalizer, Observer, Reasoner, ToolResult
from .prompt import BUDGET_TIGHT_SYSTEM, build_system


@dataclass(slots=True)
class ReactConfig:
    """配置（``config["agent"]["react"]``）。"""

    max_steps: int = 8                 # 循环上限（步数闸）
    deadline_s: float = 120.0          # 整任务时延上限（时延闸）
    # 注：单条工具结果上限**不在此处** —— 归源头护栏（S4：``context.source_clip_budget()``
    #     经 ``sys/tasks.invoke`` 在结果离开工具时净化，token 口径）。
    #     旧 ``tool_result_max_chars``（字符口径）已删除，避免与治理器口径打架。
    history_keep: int = 20             # 喂给模型的最近消息条数（**降级路径**裁剪口径）
    max_rejects: int = 3               # 同一工具被拒多少次即终止（防死循环）
    finalize_on_exceed: bool = True    # 触顶时再答一次（不给工具）
    # ---- S3 循环内上下文：**兜底默认**（治理器缺该方法时才用到） ----
    # 注：折叠**阈值**（单条上限 / 头尾保留量）归治理器（``clip_*``），本插件不持该策略；
    #     治理器无 ``clip_head_tail`` 接缝 → 不做折叠（与「无治理器退旧路径」一致）。
    react_keep_recent: int = 6         # 最近 N 条链原文保留，更老者才折叠
    # ---- D2：老 act 条目大参数折叠（token/值；<=0 关闭；需工作记忆在位才生效） ----
    act_fold_tokens: int = 256
    # ---- L1：懒加载（目录卡 + 元工具按需装载完整 schema；默认 off 灰度） ----
    lazy_tools: bool = False
    lazy_tools_max: int = 12


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
    loaded: set[str] = field(default_factory=set)   # L1：已加载完整定义的工具名


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
    summary: str = ""                     # C：收尾摘要（chat 并入 memory.remember 载荷）

    def as_evidence(self) -> dict:
        """机器证据（供 chat 转写 / 前端展示；纯数据）。"""
        return {"intent": "react", "ok": self.ok, "steps": self.steps,
                "exhausted": self.exhausted, "reason": self.reason,
                "tools": list(self.tools), "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "observations": list(self.observations),
                "summary": self.summary}


def _slim_description(desc: str, limit: int = 120) -> str:
    """D4：工具描述压成一行（首行，截 ``limit`` 字符）——纯投影，原目录不动。"""
    text = str(desc or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    return line[:limit]


def visible_tools(ctx) -> list[dict]:
    """工具可见集（权限模式裁剪；第 3 期启用预留，依据《审批插件》§6.1）。

    ``plan`` 模式 ⇒ 只保留 ``risk == "read"``（**软约束**：模型看不见写工具，
    防误导；幻觉调用仍由 approval 的 plan 硬兜底拒绝——软硬两层缺一不可）。
    判定只读 ``permission_mode`` 状态键（SSOT 前向契约），对 approval **零耦合**。
    其余模式恒全量（行为不变）。

    D4（描述瘦身）：返回前把 ``description`` 压成一行（首行 ≤120 字符）。这是
    **react 投影层**的改动——``ctx.tasks.catalog()`` 原始目录不动，approval /
    管理页等其它消费方拿到的仍是全文；系统提示词目录与 native tools 数组共用
    本投影，两处口径天然一致。
    """
    catalog = ctx.tasks.catalog()
    if str(ctx.state.get(StateKeys.PERMISSION_MODE, "") or "") == "plan":
        catalog = [t for t in catalog if t.get("risk") == "read"]
    return [{**t, "description": _slim_description(t.get("description"))}
            for t in catalog]


def _trim(messages: list[dict], keep: int) -> list[dict]:
    """历史裁剪：保 system + 首个用户请求 + 最近 ``keep`` 条（多步循环的上下文治理）。

    只裁剪**喂给模型的窗口**，不丢循环自身的状态（消息全集由调用方持有）。
    """
    if keep <= 0 or len(messages) <= keep + 2:
        return messages
    first_user = next((i for i, m in enumerate(messages)
                       if m.get("role") == "user" and isinstance(m.get("content"), str)), None)
    cut = len(messages) - keep
    # 400-fix A：切点不得落在 act 与其观察回灌之间——kept 后缀首条若是观察回灌，
    # 其前置 act（紧邻上一条）必须一并保留（回退直至切点停在配对边界之外）。
    while 0 < cut < len(messages) and _is_observe(messages[cut]):
        cut -= 1
    idx = {0, *range(cut, len(messages))}
    if first_user is not None:
        idx.add(first_user)
    return [m for i, m in enumerate(messages) if i in idx]


def _is_observe(m: dict) -> bool:
    """内部消息形状判据：观察回灌（content 为含 ``tool_results`` 的 dict）。"""
    c = m.get("content")
    return isinstance(c, dict) and c.get("tool_results") is not None


def _card(t: dict) -> dict:
    """L1：工具目录卡——名称 + 一行描述 + 参数名（无 JSON Schema），供懒加载模式暴露。"""
    return {"name": t.get("name"), "description": _slim_description(t.get("description")),
            "params": {}, "required": [], "risk": t.get("risk", "write"),
            "owner": t.get("owner", "")}


# L1：元工具完整定义（恒对模型可见；loop 内联处理，不进 ctx.tasks 注册表——
# 避免注册表污染与跨 run 状态，TaskContext.loaded 是唯一真相）
LAZY_META_TOOLS: list[dict] = [
    {"name": "tool_search", "description": "按关键词搜索可用工具（返回目录卡：名称/描述/参数名）",
     "params": {"keyword": {"type": "str"}}, "required": ["keyword"],
     "risk": "read", "owner": "plugins.react"},
    {"name": "tool_load", "description": "加载工具的完整参数定义（此后才能调用该工具；逗号分隔可多个）",
     "params": {"names": {"type": "str"}}, "required": ["names"],
     "risk": "read", "owner": "plugins.react"},
]


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
                  prior: list[dict] | None = None, resume: bool = False) -> ReactResult:
        cfg = self._cfg
        await durable(ctx, Events.REACT_START, {
            "prompt": (prompt or "")[:500], "session_id": session_id,
            "max_steps": cfg.max_steps, "at": now_iso()})

        tools = visible_tools(ctx)
        # L1：懒加载时系统提示只挂目录卡（完整定义按需经 native tools 分步装载）
        prompt_catalog = ([_card(t) for t in tools] if cfg.lazy_tools else tools)
        system_prompt = build_system(prompt_catalog, ctx.get(Services.SYSTEM_MEMORY) or "")

        # 工作记忆（第 3 层）：任务级进度缓存。接缝获取，缺失则跳过（不破解耦）。
        wm = ctx.get(Services.WORK_MEMORY)
        run_id = uuid.uuid4().hex
        task_id = f"{session_id}:{run_id}"
        # F2：账单归组键（尽力而为——budget 的 pre-call 读此键落 agent_budget_log.run_id）
        ctx.state[StateKeys.REACT_RUN_ID] = run_id
        if wm is not None:
            await wm.begin(task_id, {"session_id": session_id, "run_id": run_id,
                                     "max_steps": cfg.max_steps})

        # 任务级视图（S3）：prior = 入口已装配上下文（rigid，循环内不重裁）；
        # chain = **仅**循环产生的 Think/Act/Observe（草稿，循环结束即弃）。
        task = TaskContext(prior=[dict(m) for m in (prior or [])], task_id=task_id)
        # P2：「继续」续接——取回上轮链存档作初始 chain（再走 _prepare_chain 治理；
        # 无存档如实回退现状，不伪造）
        if resume and wm is not None:
            try:
                arc = await wm.get(f"react_chain:{session_id}")
                cps = json.loads((arc or {}).get("checkpoints") or "[]")
                data = next((c.get("data") or {} for c in reversed(cps)
                             if c.get("name") == "chain"), {})
                archived = [dict(m) for m in (data.get("chain") or [])
                            if isinstance(m, dict)]
                if archived:
                    task.chain.extend(archived)
                    await durable(ctx, Events.REACT_ARCHIVED, {
                        "session_id": session_id, "resumed": True,
                        "entries": len(archived), "at": now_iso()})
            except Exception as exc:                 # noqa: BLE001 —— 续接失败退全新 run
                logger.warning("react chain resume load failed: %s", exc)
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
        chain_act_folds = 0                    # D2：累计 act 参数折叠次数（可观测）
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
            # L1：懒加载时本步 native tools = 元工具 + 已加载完整定义 + 其余目录卡
            tools_step = (self._tools_for_step(tools, task) if cfg.lazy_tools else tools)
            window, win = self._window(ctx, task, system_prompt, prompt, messages, steps,
                                      tools=tools_step)
            if win:
                rt = win.get("react", {})
                chain_clips += int(rt.get("clips", 0))
                chain_act_folds += int(rt.get("act_folds", 0))
                chain_peak = max(chain_peak, int(rt.get("chain_tokens", 0)))
                rt["clips"], rt["chain_peak_tokens"] = chain_clips, chain_peak
                rt["act_folds"] = chain_act_folds

            # D1：预算预警旗标在位 → 本步窗口注入「尽快收束」提示（每步追加，幂等；
            # 只读状态键，对 budget 零直连——与 PERMISSION_MODE 同款前向契约）
            if ctx.state.get(StateKeys.BUDGET_WARNING):
                window.append({"role": "system", "content": BUDGET_TIGHT_SYSTEM})

            # ---- Think ----
            try:
                out = await self._reasoner.reason(ctx, window, tools_step)
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
            # B-fix：输出过思考的 assistant 消息回传时必须携带 reasoning_content（DeepSeek 校验）
            if getattr(out, "reasoning_content", ""):
                turn["reasoning_content"] = out.reasoning_content
            messages.append(turn)
            task.chain.append(turn)

            if not out.tool_calls:                       # ← 收敛：不再需要工具
                await durable(ctx, Events.REACT_FINAL, {
                    "steps": steps, "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "at": now_iso()})
                self._record(ctx, steps, tool_trace, exhausted=False)
                await self._archive_chain(ctx, wm, session_id, task, steps,
                                          tokens_in, tokens_out)
                if wm is not None:
                    await wm.finish(task_id)
                ctx.state[StateKeys.REACT_RUN_ID] = ""
                return ReactResult(ok=True, text=out.text or "（未给出回答）", steps=steps,
                                   tokens_in=tokens_in, tokens_out=tokens_out,
                                   tools=tool_trace, observations=observed)

            # ---- Act + Observe ----
            # L1：懒加载模式下先拦截元工具与未加载调用（loop 内联处理，不进注册表）
            meta_results: list[ToolResult] = []
            real_calls = out.tool_calls
            if cfg.lazy_tools:
                meta_results, real_calls = self._partition_meta(task, out.tool_calls, tools)
            results = list(meta_results)
            if real_calls:
                results.extend(await self._actor.act(ctx, real_calls))
            stop_reason: str | None = None
            for r in results:
                tool_trace.append(r.name)
                observed.append(r.as_dict())
                if r.denied:
                    # 连拒闸（防死循环）——第 3 期语义细化：``denied_by == "user"``
                    # 是「用户本轮主动拒」（拒绝理由已回灌，模型应读理由换方案），
                    # **不计连拒**；步数/时延闸仍兜底。策略类拒绝（guard/approval
                    # 名单/超时）重试无益，照旧计数。
                    if str(getattr(r, "denied_by", "") or "") == "user":
                        pass
                    else:
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
                # D2：act 完整参数随步存档——后续老 act 折叠的「按引用找回」依据
                await wm.checkpoint(task_id, f"step_{steps}",
                                    {"tool_trace": list(tool_trace[-10:]),
                                     "last_action": last, "note": "step completed",
                                     "act_calls": [c.as_dict() for c in out.tool_calls]})
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
        # C（方案）：收尾摘要——过程知识不落库，run 结束即弃；至少把「做了什么/
        # 败在哪/花多少」交还给用户与长期记忆，避免当晚「第 10-12 章蒸发」式烂尾。
        summary = self._closing_summary(steps, tool_trace, observed, tokens_in, tokens_out)
        if cfg.finalize_on_exceed and not hard:
            try:
                # 摘要以系统消息随行，finalizer 与其兜底文本均可取用
                text = await self._finalizer.finalize(
                    ctx, list(window or messages) + [{"role": "system", "content": summary}],
                    reason)
            except Exception:                            # noqa: BLE001 —— 收尾失败也要有话说
                text = f"（已达执行上限：{reason}）\n{summary}"
        else:
            text = f"（已达执行上限：{reason}）\n{summary}"
        self._record(ctx, steps, tool_trace, exhausted=True, reason=reason)
        await self._archive_chain(ctx, wm, session_id, task, steps, tokens_in, tokens_out)
        if wm is not None:
            await wm.finish(task_id)
        ctx.state[StateKeys.REACT_RUN_ID] = ""
        return ReactResult(ok=not hard, text=text, steps=steps, tokens_in=tokens_in,
                           tokens_out=tokens_out, exhausted=True, reason=reason,
                           tools=tool_trace, observations=observed, summary=summary)

    # ---------- 循环内上下文（S3：块内准备 → 治理器装配） ----------

    def _prepare_chain(self, gov, chain: list[dict], keep_recent: int, *,
                       wm=None, task_id: str = "",
                       act_fold: int = 0) -> tuple[list[dict], int, int]:
        """**块内准备：只折叠、不丢弃**（与治理器 `_trim_messages` 的丢弃职责不重叠）。

        - 最近 ``keep_recent`` 条**原样保留**；
        - 更老者中 ``type == "observe"`` 且**未 pinned** 的条目 → 逐条首尾折叠（就地生效，幂等）；
        - D2：更老者中 ``type == "act"`` 的条目 → **大参数值折叠**（单值 token 超
          ``act_fold`` 的字符串参数替换为引用标记；``path``/``mode`` 等小参数原样保留）。
          **无损承诺**：完整原文在 act 发生时已存工作记忆 checkpoint（见 ``run``），
          故仅在 ``wm`` 在位且 ``act_fold > 0`` 时启用——不可找回就不折，绝不静默丢真；
        - ``think`` / 其余 **一律保留**——关键信息通常在 Think 里已转录；
        - **不丢任何条目**：超预算后的丢弃由治理器统一执行（避免双重截断）。

        返回 ``(本步待装配的 chain, observe 折叠数, act 参数折叠数)``。
        """
        clip_fn = getattr(gov, "clip_head_tail", None)
        if not callable(clip_fn):
            return list(chain), 0, 0
        cut = max(0, len(chain) - max(0, int(keep_recent)))
        newly = 0
        act_folds = 0
        for i, m in enumerate(chain):
            if i >= cut or m.get("pinned"):
                continue
            mtype = m.get("type")
            if mtype == "observe":
                content = m.get("content")
                folded, changed = self._fold_content(content, clip_fn)
                if changed:
                    m["content"] = folded
                    newly += 1
            elif mtype == "act" and wm is not None and act_fold > 0:
                act_folds += self._fold_act(m, act_fold, task_id)
        return list(chain), newly, act_folds

    @staticmethod
    def _fold_act(m: dict, act_fold: int, task_id: str) -> int:
        """D2：折叠一条 act 条目里的**大字符串参数值**（就地，幂等）。

        只折叠超阈值的字符串值（章节全文等）；``path``/``mode`` 等结构参数保留，
        模型仍能理解「这一步对哪个文件做了什么」。折叠标记引用工作记忆 checkpoint
        键（由条目 id ``step{N}_act`` 反解出 ``step_{N}``——与 run() 的存档键一致）。
        返回本次新折叠的参数个数。
        """
        calls = m.get("tool_calls")
        if not isinstance(calls, list):
            return 0
        sid = str(m.get("id") or "")
        step_key = f"step_{sid[4:].split('_', 1)[0]}" if sid.startswith("step") else sid
        folded = 0
        for tc in calls:
            if not isinstance(tc, dict):
                continue
            args = tc.get("arguments")
            if not isinstance(args, dict):
                continue
            new_args: dict = {}
            changed = False
            for k, v in args.items():
                if isinstance(v, str):
                    est = estimate_tokens(v)
                    if est > act_fold and not v.startswith("«[已折叠"):
                        new_args[k] = (f"«[已折叠·约{est} tokens] 完整原文见工作记忆 "
                                       f"checkpoint {task_id}·{step_key}»")
                        changed = True
                        continue
                new_args[k] = v
            if changed:
                tc["arguments"] = new_args
                folded += 1
        return folded

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
                messages: list[dict], step: int,
                tools: list[dict] | None = None) -> tuple[list[dict], dict]:
        """构造喂给模型的窗口（G1 装配；无治理器 → 退回旧 ``_trim`` 条数裁剪，行为不变）。

        块列表（注册制）：system / prior（rigid）/ 本轮 prompt（rigid）/ chain（可裁）。
        prior 与 prompt 为**刚性块**——循环内不参与常规裁剪（自身超预算时治理器仍会
        按刚性降级规则诚实标注，不静默）。
        """
        gov = ctx.get(Services.CONTEXT) if ctx.has(Services.CONTEXT) else None
        fn = getattr(gov, "assemble", None) if gov is not None else None
        if not callable(fn):
            return _trim(messages, self._cfg.history_keep), {}
        # D2：工作记忆接缝（act 原文存档的找回面；缺失则 act 折叠自动关闭——无损承诺）
        wm = ctx.get(Services.WORK_MEMORY) if ctx.has(Services.WORK_MEMORY) else None
        keep_recent = _gov_int(gov, "react_keep_recent", self._cfg.react_keep_recent)
        prepared, newly, act_folds = self._prepare_chain(
            gov, task.chain, keep_recent, wm=wm, task_id=task.task_id,
            act_fold=self._cfg.act_fold_tokens)
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
            # S3（彻底版·统一账本）：native tools 数组是请求附加项、不在 messages 预算内，
            # 经 assemble(reserve_tokens=) 先扣其占用，使窗口总占用（messages + native tools）
            # 对齐真实 prompt_tokens，根治「窗口预算不认 tools」的结构性隐患。
            # F1（彻底作用）：预算整体除以校准因子——真实计费高于估算时窗口装得更少，
            # 使真实请求体积贴近窗口预算（因子读 api.cost_factor 状态键，治理器自身无 ctx）。
            from agent.api import cost_factor as _cf
            factor = _cf(ctx)
            try:
                asm = fn(blocks, reserve_tokens=estimate_request_cost(messages=[], tools=tools),
                         cost_factor=factor)
            except TypeError:                # 注入实现不认 cost_factor → 退回原签名（不劣化）
                asm = fn(blocks, reserve_tokens=estimate_request_cost(messages=[], tools=tools))
        except Exception as exc:                     # noqa: BLE001 —— 装配失败退降级，绝不裸崩
            logger.warning("react 窗口装配失败，退回条数裁剪：%s", exc)
            return _trim(messages, self._cfg.history_keep), {}
        window = [{"role": "system", "content": asm.system_text}] + list(asm.prior)
        return window, self._win_stats(asm, prepared, newly, step, keep_recent,
                                       act_folds=act_folds)

    @staticmethod
    def _win_stats(asm, prepared: list[dict], newly_clipped: int, step: int,
                   keep_recent: int, *, act_folds: int = 0) -> dict:
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
                "act_folds": int(act_folds),             # D2：act 大参数折叠数（由 run() 累计）
                "pinned": sum(1 for m in prepared if m.get("pinned")),
                "pinned_downgraded": downgraded,
                "keep_recent": int(keep_recent),
                "budget": chain_alloc,
            },
        }

    async def _emit_window(self, ctx, stats: dict) -> None:
        """发 ``agent/context/governed``（``stage="react"``）遥测；失败绝不阻断循环。

        遥测落库（一期）：此前仅 live emit、不落 agent_events——当晚诊断只能靠
        tokens_in 曲线反推裁剪决策。改为 durable（可跨重启回放）+ live 双通道。
        """
        if not stats:
            return
        try:
            await durable(ctx, Events.CONTEXT_GOVERNED,
                          {"stage": "react", "at": now_iso(), **stats})
        except Exception as exc:                     # noqa: BLE001 —— 遥测失败不阻断
            logger.warning("react context governed durable failed: %s", exc)
        try:
            await ctx.events.emit(Events.CONTEXT_GOVERNED, {
                "stage": "react", "at": now_iso(), **stats})
        except Exception as exc:                     # noqa: BLE001 —— 遥测失败不阻断
            logger.warning("react context governed emit failed: %s", exc)

    @staticmethod
    def _closing_summary(steps: int, tool_trace: list[str], observed: list[dict],
                         tokens_in: int, tokens_out: int) -> str:
        """C：组装收尾摘要（诚实交代：做了什么 / 败在哪 / 花多少）。"""
        s = (f"[收尾摘要] 本次运行已执行 {steps} 步；工具调用 {len(tool_trace)} 次"
             f"（{', '.join(tool_trace[-8:]) or '无'}）；"
             f"tokens 累计 in={tokens_in}/out={tokens_out}。")
        last = next((o for o in reversed(observed) if o.get("is_error")), None)
        if last:
            s += (f" 最后一次失败：{last.get('name')}："
                  f"{str(last.get('content') or '')[:200]}")
        else:
            s += " 无失败记录，但未形成最终答复。"
        return s

    # ---------- L1：懒加载（目录卡 / 元工具拦截 / 按需装载） ----------

    @staticmethod
    def _tools_for_step(tools: list[dict], task: TaskContext) -> list[dict]:
        """本步 native tools：元工具 + 已加载完整定义 + 其余目录卡（体积随 loaded 增长）。"""
        out: list[dict] = [dict(m) for m in LAZY_META_TOOLS]
        for t in tools:
            out.append(t if t.get("name") in task.loaded else _card(t))
        return out

    def _partition_meta(self, task: TaskContext, calls, tools: list[dict]
                        ) -> tuple[list[ToolResult], list]:
        """拆分本轮调用：元工具（内联处理）+ 未加载（错误观察可自纠）+ 真实调用。"""
        meta: list[ToolResult] = []
        real = []
        catalog = {t.get("name"): t for t in tools}
        for c in calls:
            if c.name == "tool_search":
                meta.append(self._tool_search(c, tools))
            elif c.name == "tool_load":
                meta.append(self._tool_load(task, c, catalog))
            elif c.name not in task.loaded:
                meta.append(ToolResult(
                    c.name,
                    f"工具 {c.name!r} 尚未加载完整定义（懒加载模式）。"
                    f"请先调用 tool_load(names=...) 加载后再调用，"
                    f"或用 tool_search(keyword=...) 查看可用工具目录。",
                    is_error=True, tool_call_id=c.id))
            else:
                real.append(c)
        return meta, real

    @staticmethod
    def _tool_search(c, tools: list[dict]) -> ToolResult:
        """tool_search：按关键词过滤目录，返回目录卡文本（摘要，非 schema）。"""
        kw = str((c.arguments or {}).get("keyword") or "").strip().lower()
        hits = [t for t in tools
                if not kw or kw in str(t.get("name") or "").lower()
                or kw in str(t.get("description") or "").lower()]
        lines = [f"- {t.get('name')}（{t.get('risk', 'write')}）："
                 f"{_slim_description(t.get('description'))}；参数 "
                 f"{','.join((t.get('params') or {}).keys()) or '-'}" for t in hits[:20]]
        body = "\n".join(lines) if lines else "（无匹配工具）"
        return ToolResult("tool_search", f"可用工具 {len(hits)} 个：\n{body}",
                          tool_call_id=c.id)

    def _tool_load(self, task: TaskContext, c, catalog: dict) -> ToolResult:
        """tool_load：把工具完整定义加入本 run 的 loaded 集（上限 lazy_tools_max，诚实拒绝）。"""
        raw = (c.arguments or {}).get("names") or ""
        names = ([str(x).strip() for x in raw.split(",")]
                 if isinstance(raw, str) else [str(x) for x in raw])
        names = [n for n in names if n]
        unknown = [n for n in names if n not in catalog]
        wanted = [n for n in names if n in catalog]
        cap = max(0, int(self._cfg.lazy_tools_max))
        room = cap - len(task.loaded)
        loaded, deferred = wanted[:max(0, room)], wanted[max(0, room):]
        task.loaded.update(loaded)
        parts = [f"已加载 {len(loaded)} 个：{', '.join(loaded) or '无'}"]
        if deferred:
            parts.append(f"超出加载上限（{cap}）暂缓：{', '.join(deferred)}"
                         f"——请先 tool_load 卸载优先级低者或分批调用")
        if unknown:
            parts.append(f"未知工具名忽略：{', '.join(unknown)}")
        return ToolResult("tool_load", "；".join(parts), tool_call_id=c.id)

    # ---------- P1：链存档（「继续」低成本续接的依据） ----------

    @staticmethod
    async def _archive_chain(ctx, wm, session_id: str, task: TaskContext,
                             steps: int, tokens_in: int, tokens_out: int) -> None:
        """run 收尾把已治理折叠链存工作记忆（键 react_chain:{session}，不 finish、靠 TTL）。

        与一期 progress 摘要互补：摘要是给长期记忆的**结论**，链存档是给工作记忆的
        **过程**。存档本身已过 `_prepare_chain` 折叠治理（天然有界）。
        """
        if wm is None or not task.chain:
            return
        try:
            arch_id = f"react_chain:{session_id}"
            await wm.begin(arch_id, {"session_id": session_id, "run_id": "chain",
                                     "max_steps": 0})
            await wm.checkpoint(arch_id, "chain", {
                "chain": [dict(m) for m in task.chain], "steps": steps,
                "tokens_in": tokens_in, "tokens_out": tokens_out, "at": now_iso()})
            await durable(ctx, Events.REACT_ARCHIVED, {
                "session_id": session_id, "entries": len(task.chain), "steps": steps,
                "at": now_iso()})
        except Exception as exc:                 # noqa: BLE001 —— 存档失败不阻断收尾
            logger.warning("react chain archive failed: %s", exc)

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
