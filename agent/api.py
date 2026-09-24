"""agent.api —— 内核公共命名空间（唯一出口）。

agent 本体只准 ``from agent.api import …``；禁止 ``from agent.core import …`` 或具体实现导入。
包边界即依赖边界。零业务词（test_agent_purity 守卫）。
"""

from agent import __agent_milestone__, __version__
from agent.core.container import Container, Plugin, plugin
from agent.core.context import Context, State
from agent.core.contract import LlmProvider
from agent.core.errors import (
    AgentError,
    CapabilityNotImplemented,
    ConfigError,
    CyclicDependencyError,
    EventDispatchError,
    EventWiringError,
    MissingDependencyError,
    ScopeClosedError,
    ServiceConflictError,
    ServiceNotFoundError,
    TeardownError,
)
from agent.core.events import EventBus
from agent.core.events_catalog import Events, all_events, EVENT_MODE, REQUIRED_SUBSCRIBER_EVENTS
from agent.core.services_catalog import ALL as SERVICE_KEYS, Services

__all__ = [
    "__version__",
    "__agent_milestone__",
    # container
    "Container",
    "Plugin",
    "plugin",
    # context
    "Context",
    "State",
    # services
    "Services",
    "SERVICE_KEYS",
    # events
    "EventBus",
    "Events",
    "all_events",
    "EVENT_MODE",
    "REQUIRED_SUBSCRIBER_EVENTS",
    "EventDispatchError",
    "EventWiringError",
    # 能力接缝契约（业务经此导入，不 import 实现）
    "LlmProvider",
    # errors
    "AgentError",
    "CapabilityNotImplemented",
    "ConfigError",
    "CyclicDependencyError",
    "EventWiringError",
    "MissingDependencyError",
    "ScopeClosedError",
    "ServiceConflictError",
    "ServiceNotFoundError",
    "TeardownError",
]