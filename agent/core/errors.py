"""agent.core.errors —— 诚实边界：统一错误族（slim Cordis）。

零业务词（test_agent_purity 守卫）。语义对齐引擎错误族，代码独立。
"""

from __future__ import annotations


class AgentError(Exception):
    """内核错误基类。"""


class ServiceNotFoundError(AgentError):
    """读取未提供的服务键；附可用键清单（诊断友好）。"""

    def __init__(self, key: str, ctx_name: str, *, action: str = "读取",
                 available: list[str] | None = None):
        self.key = key
        self.ctx_name = ctx_name
        self.available = available or []
        hint = f"；可用键: {sorted(self.available)}" if self.available else ""
        super().__init__(f"ctx[{ctx_name}] {action}: 服务 {key!r} 未提供{hint}")


class ServiceConflictError(AgentError):
    """D-3：同作用域重复注册同一服务键。"""

    def __init__(self, key: str, ctx_name: str):
        self.key = key
        super().__init__(f"ctx[{ctx_name}] 服务 {key!r} 已注册（D-3 键唯一，冲突）")


class ScopeClosedError(AgentError):
    """作用域已关闭仍执行变更操作（防僵尸引用/泄漏）。"""

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
    """对称卸载聚合错误（不阻断其余卸载）。"""

    def __init__(self, ctx_name: str, errors: list[BaseException]):
        self.ctx_name = ctx_name
        self.errors = errors
        super().__init__(
            f"ctx[{ctx_name}] teardown 聚合 {len(errors)} 个错误: "
            + "; ".join(str(e) for e in errors)
        )


class EventDispatchError(AgentError):
    """事件分发失败（监听器异常聚合/传播）。"""


class CapabilityNotImplemented(AgentError):
    """诚实边界：能力未实现/未配置/缺数据 → 该插槽不虚标能力。"""

    def __init__(self, capability: str, reason: str = ""):
        self.capability = capability
        suffix = f"（{reason}）" if reason else ""
        super().__init__(f"能力未就绪: {capability}{suffix}")


__all__ = [
    "AgentError",
    "CapabilityNotImplemented",
    "CyclicDependencyError",
    "EventDispatchError",
    "MissingDependencyError",
    "ScopeClosedError",
    "ServiceConflictError",
    "ServiceNotFoundError",
    "TeardownError",
]