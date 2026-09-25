"""插件包：react —— ReAct 多步工具循环（Think → Act+Observe → 收敛）。

管理：本包位于 ``agent/plugins/``，经「插件」页可停用 / 启用 / 卸载（引擎式 desired/actual/delta）。
语义上是**系统组件**（``layer=system``，与 guard/llm 同级）；放进 plugins 目录只是采用
「目录插件」这一生命周期形态 —— **来源决定能否启停卸，layer 只是标签，二者正交**（AC-8）。
默认启用（见 ``data/agent_plugin_state.json``）；停用后 ``ctx.has("react")`` 为 False，
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

文件：``prompt.py``（提示词/工具目录/截断）· ``components.py``（四组件接缝）·
``loop.py``（主循环 + 三把闸）。
"""

from __future__ import annotations

from agent.api import Services, StateKeys, plugin

from .components import (Actor, DefaultActor, DefaultFinalizer, DefaultObserver,
                         DefaultReasoner, Finalizer, Observer, Reasoner, ReasonOutput,
                         ToolCall, ToolResult)
from .loop import ReactConfig, ReactLoop, ReactResult, visible_tools

__all__ = ["React", "ReactLoop", "ReactResult", "ReactConfig", "visible_tools",
           "Reasoner", "Actor", "Observer", "Finalizer", "ReasonOutput",
           "ToolCall", "ToolResult", "DefaultReasoner", "DefaultActor",
           "DefaultObserver", "DefaultFinalizer"]


@plugin
class React:
    """ctx.react —— ReAct 多步工具循环（系统组件 · 目录插件形态 · 默认启用）。

    停用后不装配 → ``ctx.has("react")`` 为 False，``chat`` 自动回退单步路径。
    """

    name = "plugins.react"
    version = "0.1.0"
    layer = "system"
    provides = [Services.REACT]
    inject = [Services.LLM, Services.TASKS, Services.EVENTS, Services.DB]
    tables = ()
    description = "ReAct 多步工具循环（Think→Act+Observe→收敛；max_steps/时延/预算三把闸）"

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("react") or {}
        rcfg = ReactConfig(
            max_steps=int(cfg.get("max_steps", 8)),
            deadline_s=float(cfg.get("deadline_s", 120.0)),
            tool_result_max_chars=int(cfg.get("tool_result_max_chars", 2000)),
            history_keep=int(cfg.get("history_keep", 20)),
            max_rejects=int(cfg.get("max_rejects", 3)),
            finalize_on_exceed=bool(cfg.get("finalize_on_exceed", True)),
        )
        # 四组件 = 可选注入的接缝服务（与 ctx.llm 的 provider 选择同构）
        reasoner = ctx.get("react.reasoner") or DefaultReasoner()
        actor = ctx.get("react.actor") or DefaultActor(
            max_chars=rcfg.tool_result_max_chars)
        observer = ctx.get("react.observer") or DefaultObserver()
        finalizer = ctx.get("react.finalizer") or DefaultFinalizer()
        ctx.provide(Services.REACT, ReactLoop(reasoner, actor, observer, finalizer, rcfg))

    async def status(self, ctx) -> dict:
        """插件页状态摘要（未跑过则如实报 ``runs: 0``，不给笼统假状态）。"""
        cfg = ((ctx.config or {}).get("agent") or {}).get("react") or {}
        last = ctx.state.get(StateKeys.REACT_LAST) or {"runs": 0}
        return {"max_steps": int(cfg.get("max_steps", 8)),
                "permission_mode": ctx.state.get(StateKeys.PERMISSION_MODE, "default"),
                **last}
