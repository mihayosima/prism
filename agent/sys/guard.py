"""agent.sys.guard —— 对等执行契约 / 权限网关（硬规则层）。

- 矩阵为配置数据（``config["agent"]["permissions"]``），**默认 open**（不先限制能力）。
- 挂在 capability 事件上裁决（waterfall）：``agent/llm/pre-call``、``agent/tasks/pre-execute``。
  同一 seam 多裁决插件：**谁先裁决取决于注册顺序，不取决于插件自我声明**——
  ``core/events.py`` 的 ``prepend`` 是 ``insert(0, …)``，所以**后注册者排前面**。
  实际装配序：``profiles.py:137`` 先装 ``SYS_PLUGINS``（含本插件）→ ``:145`` 再装目录插件
  （``plugins.sandbox`` / ``plugins.approval``）⇒ **实际顺序 sandbox → guard → approval**
  （approval 是 append，排最后）。改插件装配顺序会改变裁决优先级，勿依赖"我写了 prepend 所以我先"。
- 允许 → ``next()`` 放行；拒绝 → 不带 next() 短路，返回 ``{"denied": True, reason}``。
- 每次裁决落 durable ``agent/permission/decision``（审计=事件查询，不另建表）。

策略唯一性
----------------------------------------
本插件是**策略（谁能调什么）的唯一持有者**。目录插件 ``plugins.sandbox`` 历史上也带过一份
allow/deny/risk 名单（已认定为功能重叠，见
``docs/sandbox/沙箱与权限管理器重叠核定-2026-10-03.md``）：
两条名单并存会互相打洞、无单一真相。⇒ 模式匹配（尾部 ``*``）与域限定（``targets``）已收归本插件。
``plugins.approval`` 不参与策略判定——它是**唯一真问人**的通道（provider + 待答表 + 弹窗），
其 ``ask`` 是真审批；本插件的 ``risk.*="ask"`` 是**无 approval 时的 fail-safe 兜底**（按拒绝处理）。

唯一覆盖的两面（勿误判为可弃件）
----------------------------------------
1. ``LLM_PRE_CALL`` —— **模型调用**裁决，本插件是唯一挂载者（sandbox 完全不挂该 seam）。
2. **非 ``mcp.*`` 的内置工具** —— sandbox 默认 ``targets`` 只管 MCP 域。
"""

from __future__ import annotations

from agent.api import (SESSION_CFG_VIEW, Events, Services, current_session_key,
                       durable, inc_counter, now_iso, plugin)

# 未命中任何名单时的兜底裁决 = 放行（与实施前行为一致：默认 open，不先限制能力）。
DEFAULT_POLICY: dict = {
    "default": "open",
    "llm": "open",
    "task_allow": [],
    "task_deny": [],
    "risk": {},
    "targets": ["*"],          # 域限定：默认全部（等价于实施前"管辖全部"的行为）
}


def match(pattern: str, target: str) -> bool:
    """工具名模式匹配：仅支持**尾部** ``*``（``mcp.*`` / ``*``），其余按精确名。

    自 ``plugins/sandbox`` 原样迁入（2026-10-03 收口：策略唯一性）。
    ⚠️ **搬迁时不得改动本函数任何一行** —— 沙箱的 16 个用例里有 8 条是它的真值表，
    两侧语义必须逐位一致。``.workbuddy/verify_guard_match_equiv.py`` 穷举比对两实现。

    精确名是 ``*`` 的特例（``pattern == target``），故此函数对既有精确名配置**完全向后兼容**。
    """
    p = str(pattern or "").strip()
    if not p:
        return False
    if p == "*":
        return True
    if p.endswith("*"):
        return target.startswith(p[:-1])
    return p == target


def in_scope(policy: dict, target: str) -> bool:
    """该工具是否在本插件管辖范围内（不在 ⇒ 完全不过问）。"""
    return any(match(p, target) for p in (policy.get("targets") or []))


def _matrix(ctx) -> dict:
    """三层合并：插件默认 ← 容器级 ``permissions`` ← 会话级 ``plugins.guard``。

    会话级走 ``SESSION_CFG_VIEW`` 接缝，**它是可调用对象** ``(section, sk) -> dict``：
    调用它就已拿到**解包后**的插件节（``ConfigStore.view`` 内部已做过 ``section_of``）
    ⇒ **不要再套一次 ``section_of``**。写成字典用法会抛 ``AttributeError``，
    且因本函数在每个任务调用前都跑，等于**整个插件把工具调用全打断**
    （沙箱已踩过这个坑，见其模块 docstring 与 ``test_view_seam_must_be_called_not_indexed``）。
    """
    merged: dict = {**DEFAULT_POLICY}
    container = ((ctx.config or {}).get("agent") or {}).get("permissions") or {}
    merged.update({k: v for k, v in container.items() if v is not None})
    sk = current_session_key(ctx)
    if not sk:
        return merged
    try:
        view = ctx.get(SESSION_CFG_VIEW)
        sess = view("guard", sk) if callable(view) else {}
    except Exception:                       # noqa: BLE001 —— 视图不可用即回落容器级
        return merged
    merged.update({k: v for k, v in (sess or {}).items() if v is not None})
    return merged


def _hit(policy: dict, key: str, target: str) -> str | None:
    """名单命中判定：返回命中的模式，或 ``None``。

    尾部 ``*`` 通配由 :func:`match` 提供（精确名是其特例 ⇒ 向后兼容）。
    """
    for pat in (policy.get(key) or []):
        if match(pat, target):
            return str(pat)
    return None


# ``permissions.llm`` 的**唯一合法取值**（2026-10-03 收紧）。
# ⚠️ 这是一个**高危开关**：``deny`` 会拒掉**所有**模型调用（记忆提取 / 解说 / 反思都在内），
# 不是一个可以随手关的"模型权限"。所以取值域必须显式：
#   - 此前实现是 ``"allow" if m.get("llm","open") == "open" else "deny"``，
#     即**任何非 open 的字符串**（``"true"`` / ``"1"`` / ``"是"`` / ``"deny"`` / 乱写）
#     都会**静默锁死全部模型调用**，且 ``config_schema`` 的 ``str`` 类型拦不住（任何字符串都合法）。
#   - 收紧后：只认 ``open`` / ``deny``；**未知值回落 open 并计数**（fail-open，
#     与本模块"配置写错不该把能力全锁死"的既有方向一致 —— 沙箱历史 ``decide()`` 同款）。
_LLM_MODES = ("open", "deny")


def _llm_decision(matrix: dict) -> str:
    """``LLM_PRE_CALL`` 裁决：显式枚举 + 未知值回落。

    ★ 未知值回落 ``open``（放行）而非 ``deny``：这是**故意**的选择。
    "配置写错" 与 "明确要求禁用" 是两件事，把前者当后者处理会让一次手滑变成服务不可用；
    而放行的代价是最多多了几次本就要付费的模型调用，可恢复且可见。
    未知值会打 ``agent_guard_llm_mode_invalid_total`` 计数（可观测，不是静默）。
    """
    raw = str(matrix.get("llm", "open") or "open").strip().lower()
    if raw in _LLM_MODES:
        return "allow" if raw == "open" else "deny"
    inc_counter("agent_guard_llm_mode_invalid_total")
    return "allow"


def _decide(ctx, kind: str, *, target: str | None = None, caller: str = "unknown",
            risk: str = "") -> str:

    """裁决：allow / deny / ask。

    task 裁决顺序（S5，2026-10-03 补 ``targets`` 域限定并把名单判定换成模式匹配）：
    **域外直接放行** → deny 名单 → 显式 risk 策略（deny/ask/allow 优先）→
    default open 放行 → 收窄模式走 allow 名单。``risk`` 取值：
    "allow"（该风险级恒放行）/"deny"（恒拒）/"ask"（留给 approval 插件，
    无本插件时按拒绝处理）/"open"（或未配置 → 走原有名单逻辑，默认行为零回归）。

    ⚠️ ``ask`` 的诚实说明：waterfall 里"要问人"必须由**下游** approval 裁决，
    而本插件是 prepend ⇒ 真 ask 需把裁决权交给 approval。``ask`` 与沙箱历史上的假 ask
    （"拒绝并在理由里点名"）语义不同，**不可互相替代**。
    """
    m = _matrix(ctx)
    default = m.get("default", "open")
    if kind == "llm":
        return _llm_decision(m)
    if kind == "task":
        if not in_scope(m, target or ""):
            return "allow"                      # 域外不过问（不变成"什么都拦一遍"）
        if _hit(m, "task_deny", target) is not None:
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
        return "allow" if _hit(m, "task_allow", target) is not None else "deny"
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

    async def status(self, ctx) -> dict:
        """运行态摘要（``Container.plugin_statuses`` 消费）⇒ 插件页展示真实策略。

        ★ 与 ``plugins.sandbox`` 的分工：本条是**策略**摘要（谁能调什么），
        沙箱的 ``status`` 应只报**隔离档位与强度**。两者不重叠、不互相代答。
        """
        m = _matrix(ctx)
        return {
            "default": m.get("default", "open"),
            "llm": m.get("llm", "open"),
            "targets": list(m.get("targets") or []),
            "task_allow": list(m.get("task_allow") or []),
            "task_deny": list(m.get("task_deny") or []),
            "risk": dict(m.get("risk") or {}),
            "session": current_session_key(ctx) or "container",
        }


__all__ = ["Guard", "match", "in_scope", "DEFAULT_POLICY"]
