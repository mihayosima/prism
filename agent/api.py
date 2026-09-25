"""agent.api —— 内核公共命名空间（唯一出口）。

agent 本体只准 ``from agent.api import …``；禁止 ``from agent.core import …`` 或具体实现导入。
包边界即依赖边界。零业务词（test_agent_purity 守卫）。
"""

from agent import __agent_milestone__, __version__
from agent.core.audit import durable, now_iso
from agent.core.container import Container, Plugin, plugin
from agent.core.context import CURRENT_PLUGIN, Context, State, current_plugin
from agent.core.contract import LlmProvider, ReactResultLike
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
from agent.core.mcp_client import McpStdioClient
from agent.core.services_catalog import ALL as SERVICE_KEYS, Services
from agent.core.state_keys import StateKeys
from agent.core.tokens import clip_json, clip_text, estimate_tokens

# ---- 提示词标记 SSOT（D4）-------------------------------------------------
# 离线 FixtureLlm 据系统提示中的标记路由确定性响应；生产侧（chat/memory/react）
# 与夹具侧（fixtures/llm.py）一律引用本处常量，禁止第三处书写 `<!--...-->` 字面量。
MARK_CHAT_PLAN = "<!--chat:plan-->"
MARK_CHAT_ANSWER = "<!--chat:answer-->"
MARK_MEMORY_EXTRACT = "<!--memory:extract-->"
MARK_REACT_TURN = "<!--react:turn-->"
MARK_REACT_FINAL = "<!--react:final-->"

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
    "current_plugin",
    "CURRENT_PLUGIN",
    # services
    "Services",
    "SERVICE_KEYS",
    # ctx.state 共享键（跨插件契约）
    "StateKeys",
    # 审计公共面（D4 转正；实现位于 core/audit）
    "durable",
    "now_iso",
    # MCP stdio 客户端（实现位于 core/mcp_client；业务插件一律经本出口）
    "McpStdioClient",
    # token 原语（实现位于 core/tokens；插件/系统层一律经本出口，不得直连 core）
    "estimate_tokens",
    "clip_text",
    "clip_json",
    # 提示词标记 SSOT（D4：离线 fixture 据此路由；`<!--` 字面量只许出现在本文件）
    "MARK_CHAT_PLAN",
    "MARK_CHAT_ANSWER",
    "MARK_MEMORY_EXTRACT",
    "MARK_REACT_TURN",
    "MARK_REACT_FINAL",
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
    "ReactResultLike",
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