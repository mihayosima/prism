"""插件包：react —— ReAct 多步工具循环。

管理：本包位于 ``agent/plugins/``，经「插件」页可停用 / 启用 / 卸载。
语义上是系统组件；放进 plugins 目录只是采用
「目录插件」这一生命周期形态 —— 来源决定能否启停卸，layer 只是标签，二者正交。
已启用；停用后 ``ctx.has("react")`` 为 False，
``chat`` 自动回退到原有的单步「规划 → 执行 → 解说」路径。

设计要点
--------
- 循环：每轮 Think；模型不再返回工具调用即收敛；否则 Act+Observe 把结果回灌。
- 三把闸：``max_steps`` / ``deadline_s`` / token 预算。
- 失败即观察：工具异常/参数错/被拒都转成观察回灌，让模型自纠；同一工具连拒超
 ``max_rejects`` 才终止。
- 四组件可换：``ctx.provide("react.reasoner"|"react.actor"|"react.observer"|
 "react.finalizer", ...)`` 任一存在即用注入实现，缺省用内置。
- 零特权：模型一律走 ``ctx.llm``，工具一律走
 ``ctx.tasks.invoke``。不新建特权通道。
- 无自有表：``tables = ()`` ⇒ 卸载零残留。
- 依赖方向单向：``chat --软--> react --软 (读 permission_mode)--> approval``；
 approval 对 react 零依赖。
- 循环内上下文：Think/Act/Observe 是任务级草稿，
 不进 ``agent_messages``／工作记忆／Redis；窗口经 ``plugins.context`` 装配。职责不重叠：本插件只做「块内准备·逐条折叠」，
 治理器只做「预算裁剪·整块丢弃 + 末条保底」。治理器缺失 → 退回 ``_trim`` 条数裁剪。

文件：``prompt.py``· ``components.py``·
``loop.py``。
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
    """ctx.react —— ReAct 多步工具循环。

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
            # P1-1：无进展检测阈值 —— 配置旋钮，设极大即关闭。
            no_progress_window=int(cfg.get("no_progress_window", 5)),
            no_progress_repeat=int(cfg.get("no_progress_repeat", 3)),
            finalize_on_exceed=bool(cfg.get("finalize_on_exceed", True)),
            # S3 兜底默认
            react_keep_recent=int(cfg.get("react_keep_recent", 6)),
            # 老 act 条目单参数值折叠阈值
            act_fold_tokens=int(cfg.get("act_fold_tokens", 256)),
            # L1：懒加载
            lazy_tools=bool(cfg.get("lazy_tools", False)),
            lazy_tools_max=int(cfg.get("lazy_tools_max", 12)),
            # 续接前缀对齐
            alignment_enabled=bool(cfg.get("alignment_enabled", True)),
        )
        # 四组件 = 可选注入的接缝服务
        reasoner = ctx.get("react.reasoner") or DefaultReasoner()
        actor = ctx.get("react.actor") or DefaultActor(caller="plugins.react")
        observer = ctx.get("react.observer") or DefaultObserver()
        finalizer = ctx.get("react.finalizer") or DefaultFinalizer()
        ctx.provide(Services.REACT, ReactLoop(reasoner, actor, observer, finalizer, rcfg))

        # 子 Agent 接缝：受限循环工厂。签名引擎无关，返回对象须具
        # ``run(ctx, prompt, identity) -> ReactResult``。未来子引擎换 LangGraph 只改本闭包体。
        def _make_loop(tools_override=None, cfg_overrides=None, components_override=None,
                       governance_override=None, depth=0):
            return ReactLoop(reasoner, actor, observer, finalizer, rcfg,
                             tools_override=tools_override, cfg_overrides=cfg_overrides,
                             components_override=components_override,
                             governance_override=governance_override, depth=depth)
        ctx.provide(Services.REACT_FACTORY, _make_loop)

    async def status(self, ctx) -> dict:
        """插件页状态摘要。"""
        cfg = ((ctx.config or {}).get("agent") or {}).get("react") or {}
        last = ctx.state.get(StateKeys.REACT_LAST) or {"runs": 0}
        return {"max_steps": int(cfg.get("max_steps", 8)),
                # R1 补：档位按会话分桶 ⇒ 经内核助手取本会话的值
                "permission_mode": current_permission_mode(ctx),
                **last}
