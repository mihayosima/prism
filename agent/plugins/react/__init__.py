"""插件包：react —— ReAct 多步工具循环（Think → Act+Observe → 收敛）。

管理：本包位于 ``agent/plugins/``，经「插件」页可停用 / 启用 / 卸载（引擎式 desired/actual/delta）。
语义上是**系统组件**（``layer=system``，与 guard/llm 同级）；放进 plugins 目录只是采用
「目录插件」这一生命周期形态 —— **来源决定能否启停卸，layer 只是标签，二者正交**（AC-8）。
已启用（依据 ``data/agent_plugin_state.json``；**代码级默认是 opt-in** —— 该文件缺失时
``PluginManager`` 返回空 enabled 列表，全部目录插件停用）；停用后 ``ctx.has("react")`` 为 False，
``chat`` 自动回退到原有的单步「规划 → 执行 → 解说」路径（chat 契约一行不改）。

设计要点
--------
- **循环**：每轮 Think；模型**不再返回工具调用**即收敛；否则 Act+Observe 把结果回灌。
- **三把闸**：``max_steps`` / ``deadline_s`` / token 预算（预算天然生效——每步都走 ``ctx.llm``）。
- **失败即观察**：工具异常/参数错/被拒都转成观察回灌，让模型自纠；同一工具连拒超
  ``max_rejects`` 才终止。
- **四组件可换**：``ctx.provide("react.reasoner"|"react.actor"|"react.observer"|
  "react.finalizer", ...)`` 任一存在即用注入实现，缺省用内置（见 ``components.py``）。
- **零特权**：模型一律走 ``ctx.llm``（guard/budget/审计照生效），工具一律走
  ``ctx.tasks.invoke``（guard/schema/owner 审计照生效）。**不新建特权通道。**
- **无自有表**：``tables = ()`` ⇒ 卸载零残留（轨迹落 ``agent_events``，转写落 ``agent_messages``）。
- **依赖方向单向**：``chat --软--> react --软(读 permission_mode)--> approval``；
  approval 对 react **零依赖**（见《ReAct插件-设计方案.md》§14）。
- **循环内上下文（S3）**：Think/Act/Observe 是任务级草稿（``TaskContext.chain``），
  **不进** ``agent_messages``／工作记忆／Redis；窗口经 ``plugins.context`` 装配
  （``react --软--> context``，单向）。职责不重叠：本插件只做「块内准备·逐条折叠」，
  治理器只做「预算裁剪·整块丢弃 + 末条保底」。治理器缺失 → 退回 ``_trim`` 条数裁剪。

文件：``prompt.py``（提示词/工具目录）· ``components.py``（四组件接缝）·
``loop.py``（主循环 + 三把闸 + 循环内窗口）。
"""

from __future__ import annotations

from agent.api import Services, StateKeys, current_permission_mode, plugin

from .components import (Actor, DefaultActor, DefaultFinalizer, DefaultObserver,
                         DefaultReasoner, Finalizer, Observer, Reasoner, ReasonOutput,
                         ToolCall, ToolResult)
from .loop import ReactConfig, ReactLoop, ReactResult, TaskContext, visible_tools

__all__ = ["React", "ReactLoop", "ReactResult", "ReactConfig", "TaskContext", "visible_tools",
           "Reasoner", "Actor", "Observer", "Finalizer", "ReasonOutput",
           "ToolCall", "ToolResult", "DefaultReasoner", "DefaultActor",
           "DefaultObserver", "DefaultFinalizer"]


@plugin
class React:
    """ctx.react —— ReAct 多步工具循环（系统组件 · 目录插件形态 · 期望态为启用）。

    停用后不装配 → ``ctx.has("react")`` 为 False，``chat`` 自动回退单步路径。
    """

    name = "plugins.react"
    version = "0.1.0"
    layer = "system"
    provides = [Services.REACT, Services.REACT_FACTORY]
    inject = [Services.LLM, Services.TASKS, Services.EVENTS, Services.DB]
    tables = ()
    description = "ReAct 多步工具循环（Think→Act+Observe→收敛；max_steps/时延/预算三把闸）"

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("react") or {}
        rcfg = ReactConfig(
            max_steps=int(cfg.get("max_steps", 8)),
            deadline_s=float(cfg.get("deadline_s", 120.0)),
            history_keep=int(cfg.get("history_keep", 20)),
            max_rejects=int(cfg.get("max_rejects", 3)),
            finalize_on_exceed=bool(cfg.get("finalize_on_exceed", True)),
            # S3 兜底默认（折叠阈值归治理器 clip_*；此处仅最近保留条数）
            react_keep_recent=int(cfg.get("react_keep_recent", 6)),
            # D2：老 act 条目单参数值折叠阈值（token；<=0 关闭。需工作记忆在位才折叠，
            # 保证「原文存 checkpoint 可找回」的无损承诺）
            act_fold_tokens=int(cfg.get("act_fold_tokens", 256)),
            # L1：懒加载（默认 off 灰度；开关已入设置页）
            lazy_tools=bool(cfg.get("lazy_tools", False)),
            lazy_tools_max=int(cfg.get("lazy_tools_max", 12)),
            # G2.0（P0/F-1）：续接前缀对齐（默认 on；关闭即一键回退旧行为）
            alignment_enabled=bool(cfg.get("alignment_enabled", True)),
        )
        # 四组件 = 可选注入的接缝服务（与 ctx.llm 的 provider 选择同构）
        reasoner = ctx.get("react.reasoner") or DefaultReasoner()
        actor = ctx.get("react.actor") or DefaultActor(caller="plugins.react")
        observer = ctx.get("react.observer") or DefaultObserver()
        finalizer = ctx.get("react.finalizer") or DefaultFinalizer()
        ctx.provide(Services.REACT, ReactLoop(reasoner, actor, observer, finalizer, rcfg))

        # 子 Agent 接缝（v0.3 §3.2）：受限循环工厂。签名引擎无关，返回对象须具
        # ``run(ctx, prompt, identity) -> ReactResult``。未来子引擎换 LangGraph 只改本闭包体。
        def _make_loop(tools_override=None, cfg_overrides=None, components_override=None,
                       governance_override=None, depth=0):
            return ReactLoop(reasoner, actor, observer, finalizer, rcfg,
                             tools_override=tools_override, cfg_overrides=cfg_overrides,
                             components_override=components_override,
                             governance_override=governance_override, depth=depth)
        ctx.provide(Services.REACT_FACTORY, _make_loop)

    async def status(self, ctx) -> dict:
        """插件页状态摘要（未跑过则如实报 ``runs: 0``，不给笼统假状态）。"""
        cfg = ((ctx.config or {}).get("agent") or {}).get("react") or {}
        last = ctx.state.get(StateKeys.REACT_LAST) or {"runs": 0}
        return {"max_steps": int(cfg.get("max_steps", 8)),
                # R1 补：档位按会话分桶 ⇒ 经内核助手取**本会话**的值（此前读容器级单值）
                "permission_mode": current_permission_mode(ctx),
                **last}
