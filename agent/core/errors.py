"""内核抛出的错误族定义。"""

from __future__ import annotations


class AgentError(Exception):
    """内核错误基类。"""


class ServiceNotFoundError(AgentError):
    """读取未提供的服务键；附可用键清单。"""

    def __init__(self, key: str, ctx_name: str, *, action: str = "读取",
                 available: list[str] | None = None):
        self.key = key
        self.ctx_name = ctx_name
        self.available = available or []
        hint = f"；可用键: {sorted(self.available)}" if self.available else ""
        super().__init__(f"ctx[{ctx_name}] {action}: 服务 {key!r} 未提供{hint}")


class ServiceConflictError(AgentError):
    """同作用域重复注册同一服务键时抛出。"""

    def __init__(self, key: str, ctx_name: str):
        self.key = key
        super().__init__(f"ctx[{ctx_name}] 服务 {key!r} 已注册（D-3 键唯一，冲突）")


class ScopeClosedError(AgentError):
    """作用域已关闭仍执行变更操作。"""

    def __init__(self, ctx_name: str):
        super().__init__(f"ctx[{ctx_name}] 已关闭，拒绝变更操作")


class MissingDependencyError(AgentError):
    """装配：inject 键无提供方，给出缺失清单。"""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("容器装配缺失依赖: " + "; ".join(missing))


class CyclicDependencyError(AgentError):
    """装配：inject 依赖成环，给出可读环路径。"""

    def __init__(self, cycle: list[str]):
        self.cycle = cycle
        super().__init__("容器装配依赖成环: " + " -> ".join(cycle))


class TeardownError(AgentError):
    """对称卸载聚合错误。"""

    def __init__(self, ctx_name: str, errors: list[BaseException]):
        self.ctx_name = ctx_name
        self.errors = errors
        super().__init__(
            f"ctx[{ctx_name}] teardown 聚合 {len(errors)} 个错误: "
            + "; ".join(str(e) for e in errors)
        )


class EventDispatchError(AgentError):
    """事件分发失败。"""


class CapabilityNotImplemented(AgentError):
    """能力未实现 / 未配置 / 缺数据时抛出。``denied_by`` 标记拒绝来源：``""`` 缺省、``"approval"`` 审批判定、``"user"`` 用户主动拒绝。"""

    def __init__(self, capability: str, reason: str = "", *, denied_by: str = ""):
        self.capability = capability
        self.denied_by = str(denied_by or "")
        suffix = f"（{reason}）" if reason else ""
        super().__init__(f"能力未就绪: {capability}{suffix}")


class ConfigError(AgentError):
    """配置 schema 校验失败。"""


class EventWiringError(AgentError):
    """capability 接缝接线校验失败。"""


class LlmUnavailableError(AgentError):
    """模型调用重试耗尽后仍不可用时抛出。"""


class EngineUnavailable(AgentError):
    """本地能力引擎不可用时抛出。"""


__all__ = [
    "AgentError",
    "CapabilityNotImplemented",
    "ConfigError",
    "CyclicDependencyError",
    "EngineUnavailable",
    "EventDispatchError",
    "EventWiringError",
    "LlmUnavailableError",
    "MissingDependencyError",
    "ScopeClosedError",
    "ServiceConflictError",
    "ServiceNotFoundError",
    "TeardownError",
]
