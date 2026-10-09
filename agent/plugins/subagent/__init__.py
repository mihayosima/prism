"""插件包：subagent —— 子 Agent 委派（MVP；设计稿 v0.3 §3 / §5）。

职责（最小可用）：
- 注册 ``agent.delegate`` 工具：父循环委派一个**受限、独立上下文**的子循环完成任务，
  返回结构化结果 ``{status, answer, evidence, request_id}``。
- 经 ``REACT_FACTORY`` 服务契约构造子循环（**绝不 import react.loop 实现**）；工具面经
  「三层交集」收窄（visible_tools ∩ role.tools ∩ 请求 tools）；深度经 ContextVar 护栏
  （默认最多 1 层）。
- 子循环在独立 asyncio task 运行 ⇒ ContextVar 天然隔离；共享 ``ctx.state[REACT_RUN_ID]``
  在收尾被清空 ⇒ handler 负责 save/restore 父的账单键（见 runner.py）。
- ``REACT_FACTORY`` 缺席（react 被停用）⇒ 不注册工具（优雅降级）。
"""

from __future__ import annotations

import logging

from agent.api import AgentError, Events, Services, plugin

from .roles import get_role, list_roles
from .runner import run_subagent

logger = logging.getLogger("agent.subagent")

MAX_SUBAGENT_DEPTH = 1     # MVP：主循环(0) → 子(1)；子不可再委派（阶段 3 可放宽）


@plugin
class SubAgent:
    """ctx.subagent —— 子 Agent 委派（目录插件；opt-in）。"""

    name = "plugins.subagent"
    version = "0.1.0"
    layer = "business"
    provides: list[str] = []                  # 不对外暴露服务；能力经 agent.delegate 工具提供
    inject = [Services.TASKS, Services.EVENTS, Services.LLM]
    tables = ()                               # 审计走现成 agent_events，不自建表 ⇒ 卸载零残留
    description = "子 Agent 委派：受限子循环独立完成任务，返回结构化结果"

    async def apply(self, ctx) -> None:
        self._ctx = ctx
        # REACT_FACTORY 缺席（react 停用）⇒ 不注册工具（优雅降级）。
        # react 经目录插件加载且字典序先于本插件装配 ⇒ apply 时必然已就绪；
        # 此处仍做软校验，避免任何装配顺序变化导致静默注册一个跑不起来的工具。
        if not ctx.has(Services.REACT_FACTORY):
            logger.warning("REACT_FACTORY 未就绪，agent.delegate 不注册（子 Agent 不可用）")
            return
        ctx.tasks.register(
            ctx, "agent.delegate",
            schema={
                "task": {"type": "str", "required": True,
                         "description": "委派给子 Agent 的独立任务描述（目标明确、可独立验收）"},
                "role": {"type": "str", "required": False, "default": "default",
                         "description": ("子 Agent 角色（内置 default/explorer/worker；"
                                         "阶段 3 经 agent.react.subagents YAML 扩展）；"
                                         "决定提示风格与工具白名单下界")},
                "tools": {"type": "array", "required": False, "default": [],
                          "description": ("在角色白名单基础上**进一步收窄**的工具名列表；"
                                          "与角色白名单取交集（只能更窄，不能更宽）")},
            },
            handler=self._delegate,
            owner="plugins.subagent",
            description=(
                "委派一个子 Agent 在**受限、独立**的上下文中完成任务并返回结构化结果"
                "{status, answer, evidence, request_id}。"
                "适用：需要隔离上下文的专注子任务、可独立验收的子问题、前期调研/审查。"
                "不适用：需要直接读写父会话即时状态、或在 plan 模式下执行写操作——"
                "子 Agent 在独立上下文中运行，不共享父循环的对话历史与草稿；"
                "实际写操作仍受 guard/approval 制约。"),
            risk="read",      # plan 模式仍可见（只读语义；写操作仍受 guard/approval 硬兜底）
        )

    async def _delegate(self, *, task: str, role: str = "default",
                       tools: list | None = None) -> dict:
        ctx = self._ctx
        return await run_subagent(
            ctx, task=task, role=role, param_tools=tools or [],
            max_depth=MAX_SUBAGENT_DEPTH)

    # ---- 状态钩子（插件页摘要，如实上报） ----

    async def status(self, ctx) -> dict:
        enabled = ctx.has(Services.REACT_FACTORY)
        return {"delegate_enabled": enabled,
                "max_depth": MAX_SUBAGENT_DEPTH,
                "roles": list_roles() if enabled else []}


__all__ = ["SubAgent", "MAX_SUBAGENT_DEPTH"]
