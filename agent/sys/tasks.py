"""agent.sys.tasks —— 任务注册表。

- ``register(ctx, name, *, schema, handler, owner, risk)`` 注册即逆；
 ``risk`` 是工具风险元数据，随 ``catalog()`` 输出。
- ``invoke`` 先过 ``agent/tasks/pre-execute``→ schema 校验 → 执行
 → S4 源头护栏
 → ``agent/tasks/post-execute``→ 返回。
- ``resolve(name)`` 返回 ``lambda **kw`` 可调用入口。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Callable, Mapping

from agent.api import (
    AgentError,
    CapabilityNotImplemented,
    Events,
    Services,
    current_plugin,
    durable,
    guard_source,
    inc_counter,
    now_iso,
    plugin,
)

logger = logging.getLogger("agent.tasks")

# : 落 durable 时必须截断/脱敏的键名——工具参数里可能有文件正文、
# : 密钥、用户隐私；``agent_events`` 是可长期查询的审计表，不能裸存。
# : 命中即把该键的值替换为占位符。
_REDACT_KEYS: tuple[str, ...] = (
    "content", "text", "body", "data", "payload", "input", "output",
    "secret", "token", "key", "password", "passwd", "credential",
    "api_key", "apikey", "auth", "cookie", "session_key",
)

# : 单个键的保留长度：防大对象把审计表撑爆。
_REDACT_MAX = 200


def _redact_kwargs(kwargs: Mapping[str, Any] | None) -> dict:
    """工具入参脱敏：敏感键只留存在性，长值截断。

     审计要回答"调了什么工具、参数形状如何"，不需要参数内容。
     绝不落原始正文/密钥——``agent_events`` 会被前端与 SQL 长期读取。
     """
    out: dict = {}
    for k, v in (kwargs or {}).items():
        if any(s in str(k).lower() for s in _REDACT_KEYS):
            out[k] = f"<redacted:{type(v).__name__}>"
            continue
        if isinstance(v, str) and len(v) > _REDACT_MAX:
            out[k] = v[:_REDACT_MAX] + f"…<+{len(v) - _REDACT_MAX}字>"
        elif isinstance(v, str):
            out[k] = v            # 短字符串原样保留
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        else:
            # 复杂结构只记形状，不展开内容
            out[k] = f"<{type(v).__name__}:{len(v) if hasattr(v, '__len__') else '?'}>"
    return out


# ---- 源头护栏--------------------------------------------------------
# 巨量工具 / MCP 输出在离开工具时即按 token 口径裁到上限，堵住"注入入口未净化"。
# 额度唯一来源是 `plugins.context.source_clip_budget()`。
def _guard_result(ctx, result: Any) -> tuple[Any, tuple[bool, int, int | None]]:
    """把一次工具结果按 context 给的额度净化。

         - 软依赖：无 ``context`` 插件 / 无 ``source_clip_budget`` → 不裁；
         - 整函数体 self-try：任何异常都不外抛，返回原值 —— 护栏不得成为新的失败点；
         - 返回 ``(净化后的结果, (clipped, omitted_tokens, limit_tokens))``。
         """
    try:
        gov = ctx.get(Services.CONTEXT)
        budget = getattr(gov, "source_clip_budget", None)
        if not callable(budget):
            return result, (False, 0, None)
        limit = int(budget())
        guarded, omitted = guard_source(result, limit)
        return guarded, (omitted > 0, omitted, limit)
    except Exception:  # noqa: BLE001 —— 护栏失效不得掀翻工具调用
        logger.warning("source guard 失效，回退原值（本次工具结果未净化）", exc_info=True)
        return result, (False, 0, None)

# 工具风险分级，元数据，供 approval/权限模式消费；本次只落字段，无判定行为
#   read   只读
#   write  有副作用，默认；保守取值 —— 未声明即按"会改东西"对待
#   danger 高危，执行命令/改系统，应默认需人确认
RISKS = ("read", "write", "danger")


# ---- 轻量 schema 校验 ----

_TYPE_CHECKERS = {
    "str": (lambda v: isinstance(v, str), "{v!r} 不是 str"),
    "int": (lambda v: isinstance(v, int) and not isinstance(v, bool), "{v!r} 不是 int"),
    "float": (lambda v: isinstance(v, (int, float)) and not isinstance(v, bool), "{v!r} 不是 float"),
    "bool": (lambda v: isinstance(v, bool), "{v!r} 不是 bool"),
    "object": (lambda v: True, ""),
    "array": (lambda v: isinstance(v, list), "{v!r} 不是 list"),
}


def validate_kwargs(schema: Mapping[str, dict] | None, kwargs: dict[str, Any], *,
                    tool_name: str = "") -> dict[str, Any]:
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
            # B：报错附必填/可省清单，模型下一轮即可自纠——
            # 当晚「缺少必填参数 'mode'」孤句让模型无从改起。
            required = [k for k, sp in schema.items() if sp.get("required")]
            optional = [k for k in schema if k not in required]
            hint = ""
            if tool_name:
                hint = f"（{tool_name} 必填：{', '.join(required) or '-'}"
                if optional:
                    hint += f"；可省：{', '.join(optional)}"
                hint += "）"
            raise AgentError(f"缺少必填参数 {key!r}{hint}")
        elif "default" in spec:
            clean[key] = spec["default"]
    return clean


class Task:
    __slots__ = ("name", "schema", "handler", "owner", "description", "risk", "timeout_s")

    def __init__(self, name: str, schema: Mapping[str, dict] | None, handler: Callable,
                 *, owner: str | None = None, description: str = "",
                 risk: str = "write", timeout_s: float | None = None):
        self.name = name
        self.schema = schema
        self.handler = handler
        self.owner = owner
        self.description = description
        self.risk = risk
        # : 注册级超时。``None`` = 不限。
        # : H3：react 会用「剩余 deadline」覆盖它，故默认不限不影响长任务。
        self.timeout_s = timeout_s

    async def invoke(self, kwargs: dict[str, Any]) -> Any:
        clean = validate_kwargs(self.schema, kwargs, tool_name=self.name)
        result = self.handler(**clean)
        if inspect.isawaitable(result):
            return await result
        return result


class TaskTimeout(AgentError):
    """工具执行超时。

         独立异常类型⇒ 调用方可只对超时做特殊处置，而不会把"超时"与"参数非法/任务不存在"混为一谈。
         """


class TaskRegistry:
    """ctx.tasks —— 任务注册表。名称唯一。注册即逆。"""

    def __init__(self):
        self._tasks: dict[str, Task] = {}

    def register(self, ctx, name: str, *, schema: Mapping[str, dict] | None,
                 handler: Callable, owner: str | None = None,
                 description: str = "", risk: str = "write",
                 timeout_s: float | None = None) -> Callable[[], bool]:
        if name in self._tasks:
            raise AgentError(f"任务重复注册（名称唯一，D-3）: {name}")
        if risk not in RISKS:
            raise AgentError(f"risk 须 ∈ {RISKS}，得到 {risk!r}")
        owner = owner if owner is not None else current_plugin()
        self._tasks[name] = Task(name, schema, handler, owner=owner,
                                 description=description, risk=risk,
                                 timeout_s=timeout_s)

        def dispose() -> bool:
            if name in self._tasks:
                del self._tasks[name]
                return True
            return False

        ctx.effect(dispose)  # P7：注册即逆
        return dispose

    def has(self, name: str) -> bool:
        return name in self._tasks

    async def invoke(self, ctx, name: str, *, caller: str | None = None,
                     timeout_s: float | None = None, **kwargs: Any) -> Any:
        cmd = self._tasks.get(name)
        if cmd is None:
            raise AgentError(f"任务不存在: {name!r}")
        caller = caller or current_plugin() or "unknown"
        # 落 durable：与 post-execute 成对，供"请求了多少次 vs 真正
        # 执行了多少次"对比，是审批与护栏
        # 的事后审计依据。waterfall 的闸门语义不受影响。
        await durable(ctx, Events.TASKS_PRE_EXECUTE, {
            "task": name, "caller": caller, "kwargs": _redact_kwargs(kwargs),
            "risk": cmd.risk,
        })
        decision = await ctx.events.waterfall(
            Events.TASKS_PRE_EXECUTE,
            {"task": name, "kwargs": kwargs, "caller": caller,
             "risk": cmd.risk},   # S5：风险元数据随行，供 guard 消费
        )
        if isinstance(decision, dict) and decision.get("denied"):
            raise CapabilityNotImplemented(
                f"agent.tasks.{name}", decision.get("reason", "被 guard/约束否决"),
                denied_by=str(decision.get("denied_by") or ""))
        # 第 3 期：updated_input —— 决策回读改写后的 kwargs。waterfall 链底默认原样返回 payload⇒ 以「值不等」判定被改写；改写者须替换该 dict
        effective = kwargs
        if isinstance(decision, dict) and isinstance(decision.get("kwargs"), dict) \
                and decision["kwargs"] != kwargs:
            effective = decision["kwargs"]
        started = time.monotonic()
        ok, error = True, None
        clipped, omitted, limit = False, 0, None
        # H3：单次执行必须有上限。此前 `await cmd.invoke(...)` 裸 await，
        # 一个卡死的非 MCP 工具会让 run 永久挂住 —— 而 deadline 只在循环顶检查，
        # 永远到不了那个检查点。超时来源优先级：调用方显式 > 注册时声明 > 不限。
        budget = timeout_s if timeout_s is not None else cmd.timeout_s
        try:
            if budget and budget > 0:
                result = await asyncio.wait_for(cmd.invoke(effective), timeout=budget)
            else:
                result = await cmd.invoke(effective)
            # S4 源头护栏：在 emit 之前净化，使事件能带上裁剪遥测
            result, (clipped, omitted, limit) = _guard_result(ctx, result)
        except asyncio.TimeoutError as exc:
            ok, error = False, f"工具执行超时（>{budget:.1f}s）"
            inc_counter("agent_tasks_timeout_total")
            logger.warning("工具 %s 执行超时（>%ss），已中断", name, budget)
            raise TaskTimeout(f"agent.tasks.{name}", error) from exc
        except Exception as exc:
            ok, error = False, str(exc)     # S7：失败也发 post 事件
            raise
        finally:
            span_ms = int((time.monotonic() - started) * 1000)
            payload = {
                "task": name, "caller": caller,
                "ok": ok, "span_ms": span_ms,
                # error 也可能是长文本/含敏感片段 ⇒ 截断
                "error": (str(error)[:_REDACT_MAX] if error else None),
                "clipped": clipped, "omitted_tokens": omitted, "limit_tokens": limit,
                "timed_out": bool(ok is False and error and "超时" in error),
            }
            await ctx.events.emit(Events.TASKS_POST_EXECUTE, payload)
            # 同事件再落 durable：emit 是 live 遥测、不落库，
            # 导致「每任务工具调用数 / 一轮多调用分布 / 工具耗时」等度量在
            # agent_events 里取不到。
            # 经 core.audit.durable 直写——它是 agent_events 的唯一写入点
            # 故自动带上 request_id/run_id/session 关联列，
            # 可与 react/llm 事件按同一 id 串起来。
            # 载荷用同一 dict⇒ 不泄密、不膨胀。
            # durable 内部自带 db 为 None 的保护与写失败兜底，绝不影响主链路；
            # 不额外给 TasksPlugin inject DB。
            await durable(ctx, Events.TASKS_POST_EXECUTE, payload)
        return result

    def resolve(self, ctx, name: str) -> Callable[..., Any] | None:
        """降级为可调用入口。无则 None。"""
        if name not in self._tasks:
            return None
        return lambda **kw: self.invoke(ctx, name, **kw)

    def names(self) -> list[str]:
        return list(self._tasks)

    def owned_by(self, owner: str) -> list[str]:
        return [t.name for t in self._tasks.values() if t.owner == owner]

    def catalog(self, include: set[str] | None = None) -> list[dict]:
        """工具目录，供 tool-call 编排：把注册任务暴露成可调用的工具描述。"""
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
            return {"ok": True, "action": action, "now": now_iso(), "service": "agent.tasks"}

        registry.register(ctx, "agent.ping",
                          schema={"action": {"type": "str", "required": False, "default": "ping"}},
                          handler=_ping, owner=None)


__all__ = ["TaskRegistry", "TasksPlugin", "validate_kwargs", "RISKS", "TaskTimeout"]
