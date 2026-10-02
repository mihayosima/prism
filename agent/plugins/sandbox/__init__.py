"""插件包：sandbox —— 外部工具（默认 MCP）的**准入网关 + 审计**（路线图 #7 的「沙箱」一半）。

为什么需要它（核实过的缺口，不是推测）
----------------------------------------
- guard 的权限矩阵只做**精确名匹配**（``agent/sys/guard.py``：``target in task_deny`` /
  ``target in task_allow``），而 MCP 的工具名是**运行时发现**的（``mcp.<bridge>.<tool>``，
  随桥升级新增）⇒ 配置里既写不出 ``mcp.*`` 这种模式，也挡不住事后新增的工具。
  这是"权限机制明明在、却管不到外部工具"的根因。
- 另有一个**已核实的事实**决定了本插件的形态边界：``Task.risk`` 默认 ``"write"``，而两个
  MCP 插件注册时都**没传 risk** ⇒ **全部 MCP 工具都是 write 档**。所以按风险级分级对
  MCP 只能整体生效，**要按工具细分只能靠名称模式**（本插件的主手段）。

**沙箱在这里的定位**：不是进程隔离（那与"操作本机的助手"这一定位冲突，且平台相关），
而是**准入 + 可审计**——谁、在哪个会话、调用了哪个外部工具，逐次留痕、按模式可控。
本模块**不承诺**任何进程级隔离；真要进程隔离应另立议题（见路线图 #7 的另半）。

与既有权限体系的关系（**不重复造轮子**）
----------------------------------------
- 挂**同一个** waterfall：``agent/tasks/pre-execute``，``prepend=True`` ⇒ **最先裁决**
  （``core/events.py`` 的 ``prepend`` 是 ``insert(0, …)``，与注册顺序无关）。理由有二：
  ① 沙箱是"能不能进"的前置门；② 载荷带 ``kwargs`` 且 waterfall 支持**参数改写**
  （``tasks.invoke`` 比较 ``decision["kwargs"] != kwargs``）⇒ 将来做路径重定向必须
  在 guard/approval 看到参数**之前**完成。
- 放行 ⇒ ``next()``；拒绝 ⇒ 不带 ``next`` 短路，``tasks.invoke`` 转成
  ``CapabilityNotImplemented(denied_by="sandbox")``（与 guard 同款）。
- 每次裁决落 ``agent/sandbox/decision``（durable 直写，**刻意另立**事件——用户裁定）⇒
  「权限」视图的"最近裁决"保持只装 guard 的判据（权限矩阵），沙箱轨迹改由「事件」视图的
  ``agent/sandbox`` 过滤项查看（两者判据不同：名称模式+会话 vs 权限矩阵，混在一起就分不清
  是谁拒的）。#5 之后该账本带 ``request_id``/``session`` ⇒ 可按一次请求把外部工具轨迹串起来查。
- 已知代价：沙箱拒绝时 guard 的审计行不会产生（链被短路）——**沙箱自己落审计**，不丢。

配置
----------------------------------------
容器级 ``config["agent"]["sandbox"]``（已在 ``core/config_schema.py`` 登记**逐键严格**
schema，否则写了配置就装配失败）；会话级覆盖走既有 ``plugins.sandbox`` 节
（``SESSION_CFG_VIEW`` + ``section_of``，与 G2 同构 ⇒ 天然按会话隔离）：

    targets   受本插件管辖的**工具名模式**（默认 ``["mcp.*"]`` ⇒ 默认只管 MCP）
    default   未命中任何名单时的裁决：allow（默认，保持现状）/ ask / deny
    allow     白名单模式（先判 deny，故白名单**不能**穿透黑名单）
    deny      黑名单模式
    risk      按风险级覆盖：``{"write": "ask", "danger": "deny"}``（**仅配置文件**，
              不进设置页——见上文 F9：对 MCP 全是 write 档，放上去是个无效旋钮）

模式语法：仅支持**尾部** ``*``（``mcp.*`` / ``*``），其余按精确名。刻意**不用正则**：
这份配置是运维手写的，语义越简单越不会错配。

设置页
----------------------------------------
``targets`` / ``default`` / ``allow`` / ``deny`` 四项已接入设置页（容器级与会话级均可写，
受 schema 校验 ⇒ 键名拼错会被拒绝而非静默失效）。

``ask`` 的诚实说明：waterfall 里"要问人"必须由**下游** approval 裁决，而 guard 的 prepend
已先短路，因此本插件的 ``ask`` 与 guard 同款——**拒绝并在理由里点名**（"要放行请加入
allow 白名单，或改由审批插件裁决"）。真 ask 需把该裁决权交给 approval，属后续项。
"""

from __future__ import annotations

from agent.api import (SESSION_CFG_VIEW, Events, Services, current_session_key,
                       durable, inc_counter, now_iso, plugin)

# 未命中名单时的兜底裁决 = 放行（与实施前行为一致：启用本插件只增加可观测性）。
DEFAULT_POLICY: dict = {
    "targets": ["mcp.*"],
    "default": "allow",
    "allow": [],
    "deny": [],
    "risk": {},
}

# 插件自管默认（与 core/config_schema.py 登记的键集一致）。
DEFAULT_CONFIG: dict = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                       for k, v in DEFAULT_POLICY.items()}

_DECISIONS = ("allow", "ask", "deny")


def match(pattern: str, target: str) -> bool:
    """工具名模式匹配：仅支持**尾部** ``*``（``mcp.*`` / ``*``），其余按精确名。

    刻意不用正则：这份配置是运维手写的，语义越简单越不会错配。
    """
    p = str(pattern or "").strip()
    if not p:
        return False
    if p == "*":
        return True
    if p.endswith("*"):
        return target.startswith(p[:-1])
    return p == target


def decide(policy: dict, *, target: str, risk: str = "") -> tuple[str, str]:
    """裁决（纯函数，便于穷举单测）：``(裁决, 命中理由)``。

    顺序固定为 **deny 名单 → allow 名单 → risk 覆盖 → default**：显式黑名单是"硬"的，
    白名单**不能**穿透它（与 guard 的"deny 名单优先"同向，避免两份配置互相打洞）。
    非法裁决值一律回落 ``allow``（fail-open：配置写错不该把工具全锁死）。
    """
    for pat in (policy.get("deny") or []):
        if match(pat, target):
            return "deny", f"命中拒绝名单 {str(pat)!r}"
    for pat in (policy.get("allow") or []):
        if match(pat, target):
            return "allow", f"命中放行名单 {str(pat)!r}"
    rv = str((policy.get("risk") or {}).get(risk) or "").strip()
    if rv in _DECISIONS:
        return rv, f"风险级 {risk!r} 覆盖为 {rv!r}"
    dv = str(policy.get("default") or "allow").strip()
    return (dv if dv in _DECISIONS else "allow"), "默认裁决"


def in_scope(policy: dict, target: str) -> bool:
    """该工具是否在本插件管辖范围内（不在 ⇒ 完全不过问，避免变成"第二个 guard"）。"""
    return any(match(p, target) for p in (policy.get("targets") or []))


def _config(ctx) -> dict:
    """生效配置 = 插件默认 ← 容器级 ← 会话级（后者优先）。

    ⚠️ ``SESSION_CFG_VIEW`` 接缝是**可调用对象** ``(section, sk) -> dict``，调用它就已
    拿到**解包后**的插件节（``ConfigStore.view`` 内部已做过 ``section_of``）⇒ **不要再套
    一次 ``section_of``**。写成字典用法会抛 ``AttributeError``，且因为本函数在每个任务
    调用前都跑，等于**整个插件把工具调用全打断**。此处与 ``sys/budget.py`` 的读法同构。
    """
    merged: dict = {**DEFAULT_CONFIG}
    container = ((ctx.config or {}).get("agent") or {}).get("sandbox") or {}
    merged.update({k: v for k, v in container.items() if v is not None})
    sk = current_session_key(ctx)
    if not sk:
        return merged
    try:
        view = ctx.get(SESSION_CFG_VIEW)
        sess = view("sandbox", sk) if callable(view) else {}
    except Exception:                       # noqa: BLE001 —— 视图不可用即回落容器级
        return merged
    merged.update({k: v for k, v in (sess or {}).items() if v is not None})
    return merged


@plugin
class Sandbox:
    """外部工具准入网关（``agent/tasks/pre-execute`` 最先裁决 + 逐次审计）。"""

    name = "plugins.sandbox"
    layer = "system"
    provides = []
    inject = [Services.EVENTS, Services.DB]

    async def apply(self, ctx) -> None:
        async def _pre_task(payload: dict, next) -> dict:
            target = str(payload.get("task") or "")
            policy = _config(ctx)
            if not in_scope(policy, target):
                return await next()          # 不在作用域：完全不过问
            risk = str(payload.get("risk") or "")
            decision, why = decide(policy, target=target, risk=risk)
            await durable(ctx, Events.SANDBOX_DECISION, {
                "who": str(payload.get("caller") or "unknown"), "level": "task",
                "target": target, "risk": risk, "decision": decision,
                "by": "sandbox", "session": current_session_key(ctx),
                "reason": why, "at": now_iso(),
            })
            inc_counter("agent_sandbox_decisions_total")
            if decision == "allow":
                inc_counter("agent_sandbox_allowed_total")
                return await next()
            inc_counter("agent_sandbox_denied_total")
            hint = ("（要放行请加入 allow 白名单，或改由审批插件裁决）"
                    if decision == "ask" else "")
            return {"denied": True, "denied_by": "sandbox",
                    "reason": f"sandbox: {target} {why} —— 未授权{hint}"}

        ctx.on(Events.TASKS_PRE_EXECUTE, _pre_task, prepend=True)

    def policy(self, ctx) -> dict:
        """当前生效配置副本（诊断用，与 guard 的 ``policy()`` 同形状）。"""
        return _config(ctx)


__all__ = ["Sandbox", "decide", "match", "in_scope", "DEFAULT_CONFIG", "DEFAULT_POLICY"]
