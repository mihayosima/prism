"""agent.sys.guard —— 对等执行契约 / 权限网关（T0-T4 分级，MVP 只落机制，粒度后细）。

- 矩阵为配置数据（``config["agent"]["permissions"]``），**默认 open**（不先限制能力）。
- 挂在 capability 事件上裁决（waterfall）：``agent/llm/pre-call``、``agent/tasks/pre-execute``。
  同一 seam 多裁决插件：guard 用 prepend 先裁决，budget 后裁决（权限先、成本后）。
- 允许 → ``next()`` 放行；拒绝 → 不带 next() 短路，返回 ``{"denied": True, reason}``。
- 每次裁决落 durable ``agent/permission/decision``（审计=事件查询，不另建表）。
"""

from __future__ import annotations

from agent.api import Events, Services, durable, now_iso, plugin


def _matrix(ctx) -> dict:
    return ((ctx.config or {}).get("agent") or {}).get("permissions") or {"default": "open"}


def _decide(ctx, kind: str, *, target: str | None = None, caller: str = "unknown",
            risk: str = "") -> str:
    """裁决：allow / deny / ask。

    task 裁决顺序（S5）：deny 名单 → 显式 risk 策略（deny/ask/allow 优先）→
    default open 放行 → 收窄模式走 allow 名单。``risk`` 取值：
    "allow"（该风险级恒放行）/"deny"（恒拒）/"ask"（留给 approval 插件，
    MVP 按拒绝处理）/"open"（或未配置 → 走原有名单逻辑，默认行为零回归）。
    """
    m = _matrix(ctx)
    default = m.get("default", "open")
    if kind == "llm":
        return "allow" if m.get("llm", "open") == "open" else "deny"
    if kind == "task":
        deny = m.get("task_deny") or []
        if target in deny:
            return "deny"
        risk_policy = (m.get("risk") or {}).get(risk)
        if risk_policy == "deny":
            return "deny"
        if risk_policy == "ask":
            return "ask"
        if risk_policy == "allow":
            return "allow"
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
    provides = [Services.GUARD]
    inject = [Services.EVENTS, Services.DB]

    async def apply(self, ctx) -> None:
        async def _pre_llm(payload: dict, next) -> dict:
            caller = payload.get("caller", "unknown")
            decision = _decide(ctx, "llm", caller=caller)
            await durable(ctx, Events.PERMISSION_DECISION, {
                "who": caller, "level": "llm", "target": "agent.llm.complete",
                "decision": decision, "at": now_iso(),
            })
            if decision == "allow":
                return await next()
            return {"denied": True, "reason": f"guard: 模型调用未授权（caller={caller!r}）"}

        async def _pre_task(payload: dict, next) -> dict:
            task = payload.get("task", "")
            caller = payload.get("caller", "unknown")
            risk = payload.get("risk", "")
            decision = _decide(ctx, "task", target=task, caller=caller, risk=risk)
            await durable(ctx, Events.PERMISSION_DECISION, {
                "who": caller, "level": "task", "target": task, "risk": risk,
                "decision": decision, "at": now_iso(),
            })
            if decision == "allow":
                return await next()
            if decision == "ask":
                return {"denied": True,
                        "reason": f"guard: 任务 {task!r} 风险级 {risk!r} 需人确认（ask）；"
                                  f"approval 插件未接入前按拒绝处理"}
            return {"denied": True, "reason": f"guard: 任务 {task!r} 未授权（caller={caller!r}）"}

        ctx.on(Events.LLM_PRE_CALL, _pre_llm, prepend=True)
        ctx.on(Events.TASKS_PRE_EXECUTE, _pre_task, prepend=True)
        ctx.provide(Services.GUARD, self)

    def policy(self, ctx) -> dict:
        """当前矩阵副本（诊断用）。"""
        return dict(_matrix(ctx))


__all__ = ["Guard"]