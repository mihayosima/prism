"""agent.sys.guard —— 对等执行契约 / 权限网关（T0-T4 分级，MVP 只落机制，粒度后细）。

- 矩阵为配置数据（``config["agent"]["permissions"]``），**默认 open**（不先限制能力）。
- 挂在 capability 事件上裁决（waterfall）：``agent/llm/pre-call``、``agent/tasks/pre-execute``。
  同一 seam 多裁决插件：guard 用 prepend 先裁决，budget 后裁决（权限先、成本后）。
- 允许 → ``next()`` 放行；拒绝 → 不带 next() 短路，返回 ``{"denied": True, reason}``。
- 每次裁决落 durable ``agent/permission/decision``（审计=事件查询，不另建表）。
"""

from __future__ import annotations

from agent.api import Events, plugin
from agent.sys.audit import _now_iso, durable


def _matrix(ctx) -> dict:
    return ((ctx.config or {}).get("agent") or {}).get("permissions") or {"default": "open"}


def _decide(ctx, kind: str, *, target: str | None = None, caller: str = "unknown") -> str:
    m = _matrix(ctx)
    default = m.get("default", "open")
    if kind == "llm":
        return "allow" if m.get("llm", "open") == "open" else "deny"
    if kind == "task":
        deny = m.get("task_deny") or []
        if target in deny:
            return "deny"
        if default == "open":
            return "allow"
        allow = m.get("task_allow") or []
        return "allow" if ("*" in allow or target in allow) else "deny"
    return "allow"


@plugin
class Guard:
    """ctx.guard —— 权限网关（capability 事件裁决 + durable 审计）。"""

    name = "agent.guard"
    layer = "system"
    provides = ["guard"]
    inject = ["events", "db"]

    async def apply(self, ctx) -> None:
        async def _pre_llm(payload: dict, next) -> dict:
            caller = payload.get("caller", "unknown")
            decision = _decide(ctx, "llm", caller=caller)
            await durable(ctx, Events.PERMISSION_DECISION, {
                "who": caller, "level": "llm", "target": "agent.llm.complete",
                "decision": decision, "at": _now_iso(),
            })
            if decision == "allow":
                return await next()
            return {"denied": True, "reason": f"guard: 模型调用未授权（caller={caller!r}）"}

        async def _pre_task(payload: dict, next) -> dict:
            task = payload.get("task", "")
            caller = payload.get("caller", "unknown")
            decision = _decide(ctx, "task", target=task, caller=caller)
            await durable(ctx, Events.PERMISSION_DECISION, {
                "who": caller, "level": "task", "target": task,
                "decision": decision, "at": _now_iso(),
            })
            if decision == "allow":
                return await next()
            return {"denied": True, "reason": f"guard: 任务 {task!r} 未授权（caller={caller!r}）"}

        ctx.on(Events.LLM_PRE_CALL, _pre_llm, prepend=True)
        ctx.on(Events.TASKS_PRE_EXECUTE, _pre_task, prepend=True)
        ctx.provide("guard", self)

    def policy(self, ctx) -> dict:
        """当前矩阵副本（诊断用）。"""
        return dict(_matrix(ctx))


__all__ = ["Guard"]