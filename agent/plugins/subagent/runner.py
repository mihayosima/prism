"""子 Agent 运行器（plugins.subagent）。

与 react 主循环**解耦**：本模块**绝不 import react.loop 的实现类**，只经服务契约
``Services.REACT_FACTORY`` 取得工厂、构造受限子循环，并经 ``ctx.tasks`` / ``ctx.llm`` /
``ctx.events``（均走 ``ctx.<key>`` ⇔ ``ctx.get``）协作。任何引擎替换（如 LangGraph）
只改 react 插件内的工厂闭包，本模块零改动。

关键不变量（设计稿 v0.3 §6.1 / §6.2）：
- **深度护栏**：用 ContextVar 跟踪当前委派深度，默认最多 1 层（主循环 0 → 子 1），
  子不可再委派。ContextVar 按 asyncio task 隔离，父/子各读各的，无需共享 state。
- **共享 state 隔离**：子循环收尾会把容器级共享的 ``ctx.state[REACT_RUN_ID]`` 清空，
  故 handler 在派生前 **save**、await 结束后 **restore** 父的账单键（同时 restore SESSION_KEY 兜底）。
- **子循环独立 task**：必须 ``asyncio.create_task`` 运行子循环——否则子与主 handler 共
  同一 task，子 ``set_current_run_id`` 会污染父的请求作用域 ContextVar。
- **结构化返回**：``{status, answer, evidence, request_id}``；子运行期异常被捕后转为结构化
  ``error`` 结果（不裸 ``str(exc)``），交父循环作为观察自纠，不掀翻父 run。
"""

from __future__ import annotations

import asyncio
import contextvars
import logging

from agent.api import (
    AgentError, DEFAULT_SESSION, DEFAULT_TENANT, DEFAULT_USER, Events, Services,
    StateKeys, current_run_id, current_session_key, durable, make_identity, now_iso,
)

from .roles import combine_tools, get_role

logger = logging.getLogger("agent.subagent")

# 子循环护栏（MVP 默认；阶段 3 可经配置放宽）
SUBAGENT_MAX_STEPS = 6           # 子循环步数上限（比父的 8 更紧）
SUBAGENT_DEADLINE_S = 60.0       # 子循环整体时延上限（秒）；子循环自身的 deadline 闸据此收尾

# 当前委派深度（ContextVar，按 task 隔离）。主循环上下文为 0；每深入一层 +1。
_SUBAGENT_DEPTH = contextvars.ContextVar("subagent_depth", default=0)


async def _child_wrapper(loop, ctx, prompt: str, identity, depth: int):
    """在独立 task 中跑子循环，并把委派深度 +1（仅在该 task 内生效，不污染父）。"""
    token = _SUBAGENT_DEPTH.set(depth)
    try:
        return await loop.run(ctx, prompt, identity=identity)
    finally:
        _SUBAGENT_DEPTH.reset(token)


async def run_subagent(ctx, *, task: str, role: str, param_tools: list | None,
                       max_depth: int) -> dict:
    """委派一个受限子循环并回收结构化结果。

    参数
    ----
    task        委派给子的独立任务描述。
    role        角色名（default/explorer/worker 或阶段 3 的 YAML 角色）。
    param_tools 调用方在角色白名单基础上进一步收窄的工具名列表（交集语义）。
    max_depth   最大委派深度（含本层）；达到即拒绝继续委派。

    返回
    ----
    ``{"status", "answer", "evidence", "request_id"}``
    status ∈ {ok, exhausted, error}。
    """
    # ---- 深度护栏（基于 ContextVar，绝不动共享 state） ----
    depth = _SUBAGENT_DEPTH.get()
    if depth >= max_depth:
        raise AgentError(
            f"已达子 Agent 最大递归深度（{max_depth}），禁止继续委派；"
            f"如需更深层次，请在阶段 3 显式放宽 max_depth 并评估风险")

    role_obj = get_role(role)
    role_name = role_obj.name

    # ---- 工具三层交集的下两层：role.tools ∩ 请求 tools（父权限裁剪由工厂再做） ----
    try:
        tools_override = combine_tools(role_obj.tools, param_tools)
    except ValueError as exc:
        raise AgentError(str(exc))

    # ---- 软依赖引擎：REACT_FACTORY 缺席 ⇒ 诚实报错（优雅降级） ----
    factory = ctx.get(Services.REACT_FACTORY)
    if factory is None:
        raise AgentError("子 Agent 引擎（react.factory）未就绪：react 插件未启用或被停用")

    child_loop = factory(
        tools_override=tools_override,
        cfg_overrides={"max_steps": SUBAGENT_MAX_STEPS,
                       "deadline_s": SUBAGENT_DEADLINE_S},
        depth=depth + 1,
    )

    # ---- 子身份：同父的 user/session，tenant 取服务端默认；run_id 由子循环自生成 ----
    sk = current_session_key(ctx) or ""
    user, _, session = sk.partition(":")
    child_identity = make_identity(
        tenant=DEFAULT_TENANT,
        user=(user or None) or DEFAULT_USER,
        session=(session or None) or DEFAULT_SESSION,
    )

    # 子任务提示 = 角色引导（如有）+ 任务本体（设计稿 §3.1a：shared_brief 由提示承载）
    prompt = (f"{role_obj.system_note}\n\n任务：{task}" if role_obj.system_note
              else f"任务：{task}")

    parent_rid = current_run_id(ctx)                 # 父 run_id（父 task 的 ContextVar，隔离安全）
    saved_rid = ctx.state.get(StateKeys.REACT_RUN_ID)
    # ★ 读会话键必须经原语（会话键守卫：禁止 ctx.state 直读，防 R1 悄悄退化）
    saved_skey = current_session_key(ctx)

    child_task = asyncio.create_task(
        _child_wrapper(child_loop, ctx, prompt, child_identity, depth + 1))
    result = None
    exc_info: BaseException | None = None
    try:
        result = await child_task
    except BaseException as exc:                      # 子运行期异常：结构化，不裸抛
        exc_info = exc
    finally:
        # ★ 还原父的共享账单键（子收尾会把它清空；ContextVar 已隔离无需动）
        ctx.state[StateKeys.REACT_RUN_ID] = saved_rid
        ctx.state[StateKeys.SESSION_KEY] = saved_skey

    if exc_info is not None:
        await durable(ctx, Events.SUBAGENT_SPAWN, {
            "parent_run_id": parent_rid,
            "parent_session_key": current_session_key(ctx),
            "child_run_id": "", "child_session_key": child_identity.session_key,
            "role": role_name, "depth": depth + 1,
            "task": (task or "")[:500], "status": "error",
            "error": type(exc_info).__name__, "at": now_iso(),
        })
        return {"status": "error", "answer": None,
                "evidence": {"error": type(exc_info).__name__,
                             "detail": str(exc_info)[:500]},
                "request_id": ""}

    # 子正常收尾（ok/exhausted 都算「跑完了」；hard 失败 ⇒ ok=False 已落在 exc 分支）
    status = ("ok" if (getattr(result, "ok", False)
                      and not getattr(result, "exhausted", False))
              else ("exhausted" if getattr(result, "exhausted", False) else "error"))
    await durable(ctx, Events.SUBAGENT_SPAWN, {
        "parent_run_id": parent_rid,
        "parent_session_key": current_session_key(ctx),
        "child_run_id": getattr(result, "run_id", "") or "",
        "child_session_key": child_identity.session_key,
        "role": role_name, "depth": depth + 1,
        "task": (task or "")[:500], "status": status, "at": now_iso(),
    })
    return {"status": status,
            "answer": getattr(result, "text", None),
            "evidence": (getattr(result, "as_evidence", lambda: {})()
                         if result is not None else {}),
            "request_id": getattr(result, "run_id", "") or ""}
