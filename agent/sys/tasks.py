"""agent.sys.tasks —— 任务注册表（run(ctx,**params) 统一入口 + owner）。

- ``register(ctx, name, *, schema, handler, owner, risk)`` 注册即逆（teardown 对称移除）；
  ``risk`` 是工具风险元数据（read/write/danger，默认 write），随 ``catalog()`` 输出。
- ``invoke`` 先过 ``agent/tasks/pre-execute``（capability，guard 挂这）→ schema 校验 → 执行
  → ``agent/tasks/post-execute``（emit）→ 返回。
- ``resolve(name)`` 返回 ``lambda **kw`` 可调用入口（面向 loop/桥，与引擎 TargetRegistry 同约定）。
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from typing import Any, Callable, Mapping

from agent.api import AgentError, CapabilityNotImplemented, Events, Services, plugin

logger = logging.getLogger("agent.tasks")

# 工具风险分级（**元数据**，供 approval/权限模式消费；本次只落字段，无判定行为）
#   read   只读（查询/读取，可自动放行）
#   write  有副作用（默认；保守取值 —— 未声明即按"会改东西"对待）
#   danger 高危（执行命令/改系统，应默认需人确认）
RISKS = ("read", "write", "danger")


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


# ---- 轻量 schema 校验（{参数名: {type, required, default}}） ----

_TYPE_CHECKERS = {
    "str": (lambda v: isinstance(v, str), "{v!r} 不是 str"),
    "int": (lambda v: isinstance(v, int) and not isinstance(v, bool), "{v!r} 不是 int"),
    "float": (lambda v: isinstance(v, (int, float)) and not isinstance(v, bool), "{v!r} 不是 float"),
    "bool": (lambda v: isinstance(v, bool), "{v!r} 不是 bool"),
    "object": (lambda v: True, ""),
    "array": (lambda v: isinstance(v, list), "{v!r} 不是 list"),
}


def validate_kwargs(schema: Mapping[str, dict] | None, kwargs: dict[str, Any]) -> dict[str, Any]:
    from agent.api import AgentError

    schema = schema or {}
    unknown = set(kwargs) - set(schema)
    if unknown:
        raise AgentError(f"未知参数 {sorted(unknown)}（schema 允许: {sorted(schema)}）")
    clean: dict[str, Any] = {}
    for key, spec in schema.items():
        if key in kwargs:
            value = kwargs[key]
            ok, msg = _TYPE_CHECKERS.get(spec.get("type", "object"), _TYPE_CHECKERS["object"])
            if not ok(value):
                raise AgentError(f"{key}: {msg.format(v=value)}")
            clean[key] = value
        elif spec.get("required", False):
            raise AgentError(f"缺少必填参数 {key!r}")
        elif "default" in spec:
            clean[key] = spec["default"]
    return clean


class Task:
    __slots__ = ("name", "schema", "handler", "owner", "description", "risk")

    def __init__(self, name: str, schema: Mapping[str, dict] | None, handler: Callable,
                 *, owner: str | None = None, description: str = "",
                 risk: str = "write"):
        self.name = name
        self.schema = schema
        self.handler = handler
        self.owner = owner
        self.description = description
        self.risk = risk

    async def invoke(self, kwargs: dict[str, Any]) -> Any:
        clean = validate_kwargs(self.schema, kwargs)
        result = self.handler(**clean)
        if inspect.isawaitable(result):
            return await result
        return result


class TaskRegistry:
    """ctx.tasks —— 任务注册表。名称唯一（D-3）。注册即逆。"""

    def __init__(self):
        self._tasks: dict[str, Task] = {}

    def register(self, ctx, name: str, *, schema: Mapping[str, dict] | None,
                 handler: Callable, owner: str | None = None,
                 description: str = "", risk: str = "write") -> Callable[[], bool]:
        if name in self._tasks:
            raise AgentError(f"任务重复注册（名称唯一，D-3）: {name}")
        if risk not in RISKS:
            raise AgentError(f"risk 须 ∈ {RISKS}，得到 {risk!r}")
        owner = owner if owner is not None else ctx.get("_current_plugin")
        self._tasks[name] = Task(name, schema, handler, owner=owner,
                                 description=description, risk=risk)

        def dispose() -> bool:
            if name in self._tasks:
                del self._tasks[name]
                return True
            return False

        ctx.effect(dispose)  # P7：注册即逆
        return dispose

    def has(self, name: str) -> bool:
        return name in self._tasks

    async def invoke(self, ctx, name: str, *, caller: str | None = None, **kwargs: Any) -> Any:
        cmd = self._tasks.get(name)
        if cmd is None:
            raise AgentError(f"任务不存在: {name!r}")
        caller = caller or ctx.get("_current_plugin") or "unknown"
        decision = await ctx.events.waterfall(
            Events.TASKS_PRE_EXECUTE, {"task": name, "kwargs": kwargs, "caller": caller}
        )
        if isinstance(decision, dict) and decision.get("denied"):
            raise CapabilityNotImplemented(
                f"agent.tasks.{name}", decision.get("reason", "被 guard/约束否决")
            )
        result = await cmd.invoke(kwargs)
        await ctx.events.emit(Events.TASKS_POST_EXECUTE, {"task": name, "caller": caller})
        return result

    def resolve(self, ctx, name: str) -> Callable[..., Any] | None:
        """降级为可调用入口（**kwargs → coroutine）。无则 None（目标不存在）。"""
        if name not in self._tasks:
            return None
        return lambda **kw: self.invoke(ctx, name, **kw)

    def names(self) -> list[str]:
        return list(self._tasks)

    def owned_by(self, owner: str) -> list[str]:
        return [t.name for t in self._tasks.values() if t.owner == owner]

    def catalog(self, include: set[str] | None = None) -> list[dict]:
        """工具目录（供 tool-call 编排：把注册任务暴露成可调用的工具描述）。"""
        out: list[dict] = []
        for t in self._tasks.values():
            if include is not None and t.name not in include:
                continue
            out.append({
                "name": t.name,
                "description": t.description or t.name,
                "params": {k: {k2: v2 for k2, v2 in v.items() if k2 != "required"}
                           for k, v in (t.schema or {}).items()},
                "required": [k for k, v in (t.schema or {}).items() if v.get("required")],
                "owner": t.owner,
                "risk": t.risk,
            })
        return out


@plugin
class TasksPlugin:
    """ctx.tasks —— 任务注册表 + 内置 agent.ping。"""

    name = "agent.tasks"
    layer = "system"
    provides = [Services.TASKS]
    inject = [Services.EVENTS]

    async def apply(self, ctx) -> None:
        registry = TaskRegistry()
        ctx.provide(Services.TASKS, registry)

        async def _ping(action: str = "ping") -> dict:
            return {"ok": True, "action": action, "now": _now_iso(), "service": "agent.tasks"}

        registry.register(ctx, "agent.ping",
                          schema={"action": {"type": "str", "required": False, "default": "ping"}},
                          handler=_ping, owner=None)


__all__ = ["TaskRegistry", "TasksPlugin", "validate_kwargs", "RISKS"]