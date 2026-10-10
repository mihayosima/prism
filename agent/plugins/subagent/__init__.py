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

from .roles import list_roles, load_role_files, roles_dir
from .runner import (MAX_DELEGATIONS_PER_RUN, MAX_PENDING_PER_RUN,
                     MAX_RESEND_PER_TASK, MAX_SUBAGENT_BATCH, MAX_SUBAGENT_THREADS,
                     _catalog, _data_dir, await_subagent, collect_pending,
                     cancel_all, pending_handles, run_subagent,
                     run_subagents_parallel, start_subagent)

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
        # 阶段 3：异步委派有在飞任务 ⇒ 卸载时必须回收（否则后台子循环脱离容器继续跑）
        ctx.effect(self._dispose)
        ctx.tasks.register(
            ctx, "agent.delegate",
            schema={
                "task": {"type": "str", "required": False, "default": "",
                         "description": ("委派给子 Agent 的独立任务描述（目标明确、可独立验收）。"
                                         "与 tasks 二选一；两个都给时以 tasks 为准")},
                "tasks": {"type": "array", "required": False, "default": [],
                          "description": (f"**并行**委派多个子任务（阶段 2）：数组，每项是一条独立任务描述。"
                                          f"最多 {MAX_SUBAGENT_BATCH} 条；同一父 run 下同时在飞的"
                                          f"子 Agent 不超过 {MAX_SUBAGENT_THREADS} 个（其余排队）。"
                                          "单个子任务失败不影响其他子，结果按原顺序返回")},
                "role": {"type": "str", "required": False, "default": "default",
                         "description": ("子 Agent 角色（内置 default/explorer/worker；"
                                         "阶段 3 经 agent.react.subagents YAML 扩展）；"
                                         "决定提示风格与工具白名单下界")},
                "tools": {"type": "array", "required": False, "default": [],
                          "description": ("在角色白名单基础上**进一步收窄**的工具名列表；"
                                          "与角色白名单取交集（只能更窄，不能更宽）")},
                "wait": {"type": "bool", "required": False, "default": True,
                         "description": ("True（默认）= 同步等待子 Agent 完成并取回结果；"
                                         "False = **异步委派**：立即返回一个 handle，"
                                         "子 Agent 在后台继续跑，你可先做别的事，"
                                         "之后用 handle 取回结果（取回是一次 await，"
                                         "不是轮询）")},
                "handle": {"type": "str", "required": False, "default": "",
                           "description": ("取回异步委派的结果：传 start 时返回的 handle；"
                                           "传 '*' 或 'all' 表示**一次性取回本轮所有**"
                                           "在飞的异步委派。给了 handle 则忽略 task/tasks")},
                "wait_timeout_s": {"type": "float", "required": False, "default": 0,
                                   "description": ("取回异步结果时最多等待的秒数（0 = 用子"
                                                   "循环自身的时限）。超时不会取消子 Agent，"
                                                   "只是这次没取回，可稍后再取")},
            },
            handler=self._delegate,
            owner="plugins.subagent",
            # ★ 描述即「何时委派」的判据载体（2026-10-10 基线驱动）：实测真模型
            #   委派触发率仅 2.7%（298 run 仅 8 次委派），根因不是能力缺失而是
            #   **模型无法自我判断该不该委派**——旧描述只给抽象标准（"需要隔离上下文"），
            #   没有可操作的门��。此处改为「三问命中任一 → 委派；三条皆否 → 自己做」+ 反例。
            description=(
                # ★ 首行是**唯一一定被模型看到**的部分，且被硬截在 120 字符
                #   （实测：原始 346 字 → 模型只看到前 120 字）。故三条判据
                #   必须**全部压进首行 120 字内**，否则第③条会被砍掉（已实测验证）。
                "委派判据（命中任一即委派，否则自己做）：①要翻很多文件/多轮检索、"
                "但结论只需一句；②有2个以上互不依赖的子任务；③子任务有明确交付物、"
                "不依赖你已写的中间状态。\n"
                "【不要委派，自己做】简单查询/单点修改/需参考本对话历史才能判断的事；"
                "步骤强依赖（前者输出是后者输入）；写操作类任务（子 Agent 不共享父会话草稿）。\n"
                "【成本】一次委派≈一份独立推理开销，简单任务委派是净亏损；拿不准先自己做。\n"
                "返回 {status, answer, evidence}。子 Agent 是叶子节点不能再委派；"
                "被反复打回重派达上限会被拒绝（防死循环），届时接受结果或换思路。"
            ),
            risk="read",      # plan 模式仍可见（只读语义；写操作仍受 guard/approval 硬兜底）
        )

    async def _dispose(self) -> bool:
        """回收在飞的异步委派（插件卸载对称；否则后台子循环脱离容器继续跑）。"""
        try:
            await cancel_all("teardown")
        except Exception as exc:                      # 卸载路径不因清理失败而中断
            logger.warning("异步委派回收失败：%s", exc)
        return True

    async def _delegate(self, *, task: str = "", role: str = "default",
                        tools: list | None = None, tasks: list | None = None,
                        wait: bool = True, handle: str = "",
                        wait_timeout_s: float = 0) -> dict:
        ctx = self._ctx
        timeout_s = float(wait_timeout_s or 0)
        handle = str(handle or "").strip()

        # ---- 阶段 3：取回异步结果（handle 优先；'*'/'all' = 一次性 join 全部） ----
        if handle:
            if handle in ("*", "all"):
                return await collect_pending(ctx, timeout_s)
            return await await_subagent(ctx, handle, timeout_s)

        batch = [t for t in (tasks or []) if str(t or "").strip()]
        if batch:                                     # 并行路径（阶段 2）
            if wait is False:                         # 阶段 3：并行 + 异步 ⇒ 逐个 spawn
                spawned = [await start_subagent(
                    ctx, task=t, role=role, param_tools=tools or [],
                    max_depth=MAX_SUBAGENT_DEPTH) for t in batch]
                handles = [s.get("handle") or "" for s in spawned
                           if s.get("status") == "pending"]
                rejected = [s for s in spawned if s.get("status") == "rejected"]
                return {"status": "pending" if handles else "rejected",
                        "count": len(spawned), "handles": handles,
                        "rejected": rejected, "results": spawned}
            return await run_subagents_parallel(
                ctx, tasks=batch, role=role, param_tools=tools or [],
                max_depth=MAX_SUBAGENT_DEPTH)
        if not str(task or "").strip():
            raise AgentError("agent.delegate 需要 task 或 tasks（两者皆空，无法委派）")
        if wait is False:                             # 阶段 3：异步单发
            return await start_subagent(
                ctx, task=task, role=role, param_tools=tools or [],
                max_depth=MAX_SUBAGENT_DEPTH)
        return await run_subagent(
            ctx, task=task, role=role, param_tools=tools or [],
            max_depth=MAX_SUBAGENT_DEPTH)

    # ---- 状态钩子（插件页摘要，如实上报） ----

    async def status(self, ctx) -> dict:
        enabled = ctx.has(Services.REACT_FACTORY)
        extra = (load_role_files(roles_dir(_data_dir(ctx)), _catalog(ctx))
                 if enabled else {})
        return {"delegate_enabled": enabled,
                "max_depth": MAX_SUBAGENT_DEPTH,
                "max_threads": MAX_SUBAGENT_THREADS,
                "max_batch": MAX_SUBAGENT_BATCH,
                "max_delegations_per_run": MAX_DELEGATIONS_PER_RUN,
                "max_resend_per_task": MAX_RESEND_PER_TASK,
                "pending": pending_handles(ctx) if enabled else [],
                "max_pending_per_run": MAX_PENDING_PER_RUN,
                "roles_dir": str(roles_dir(_data_dir(ctx))),
                "roles": list_roles(extra) if enabled else [],
                "file_roles": sorted(extra) if enabled else []}


__all__ = ["SubAgent", "MAX_SUBAGENT_DEPTH"]
