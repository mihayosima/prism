"""内核公共命名空间，各层一律经此导入内核符号，不直接 import 具体实现。

包边界即依赖边界。
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
    EngineUnavailable,
    EventDispatchError,
    EventWiringError,
    LlmUnavailableError,
    MissingDependencyError,
    ScopeClosedError,
    ServiceConflictError,
    ServiceNotFoundError,
    TeardownError,
)
from agent.core.events import EventBus
from agent.core.events_catalog import Events, all_events, EVENT_MODE, REQUIRED_SUBSCRIBER_EVENTS
from agent.core.mcp_client import McpStdioClient, build_task_schema
from agent.core.config_schema import DEFAULT_DB_PATH, coerce_config, validate_config
from agent.core.obs import (bind_request_id, configure_logging, counters_snapshot,
                            current_request_id, ensure_request_id, inc_counter,
                            new_request_id, trace_fields)
from agent.core.request_cost import (estimate_request_cost, wire_messages,
                                     wire_tools)
# 沙箱 confinement 后端，供 plugins.sandbox 驱动隔离档位。
from agent.core.sandbox_backend import (BACKENDS, BRIDGE_PORTABILITY, STRENGTHS,
                                        BackendUnavailable, ConfinementBackend,
                                        ContainerBackend, NullBackend, SpawnOptions,
                                        Wsl2Backend, apply_wrap, call_spawn_provider,
                                        describe_backend, is_portable, runnable_in,
                                        select_backend, to_wsl_path, unportable_bridges)
from agent.core.scope_config import (DEFAULT_SESSION_LIMIT, evict_lru,
                                     merge_overrides, section_of, touch)
from agent.core.services_catalog import ALL as SERVICE_KEYS, Services
from agent.core.session import (DEFAULT_SESSION, DEFAULT_TENANT, DEFAULT_USER,
                                SessionIdError, SessionIdentity, build_task_id,
                                coerce_identity, current_run_id,
                                current_session_key, make_identity, parse_task_id,
                                set_current_run_id, set_current_session_key)
from agent.core.state_keys import StateKeys
from agent.core.tokens import (clip_json, clip_text, clip_text_hard, estimate_tokens,
                               guard_source)

# ---- 提示词标记 ----------------------------------------------------
# 离线 FixtureLlm 按系统提示里的这些标记路由确定性响应，字面量只在此处定义一份。
MARK_CHAT_PLAN = "<!--chat:plan-->"
MARK_CHAT_ANSWER = "<!--chat:answer-->"
MARK_MEMORY_EXTRACT = "<!--memory:extract-->"
MARK_REACT_TURN = "<!--react:turn-->"
MARK_REACT_FINAL = "<!--react:final-->"


# ---- 配置视图接缝名 ------------------------------------------------
# 适配层把 view(section, session_key) 挂到该键上，插件侧软取，取不到就退回容器级配置。
SESSION_CFG_VIEW = "session.cfg_view"

# ---- 成本校准因子 --------------------------------------------------

_COST_RATIO_CLAMP = (0.5, 3.0)          # 防异常样本把窗口裁到不可用或彻底放开
_COST_RATIO_MIN_SAMPLES = 5             # 样本不足时不做校准


def cost_factor(ctx, default: float = 1.0) -> float:
    """读取估算成本的校准因子，真实计费约为估算值乘以该因子。

 样本不足、未维护或读取异常时返回 default。
 """
    try:
        stats = ctx.state.get(StateKeys.LLM_COST_RATIO) or {}
        model = (((getattr(ctx, "config", None) or {}).get("agent") or {})
                 .get("llm") or {}).get("model", "")
        m = stats.get(model) or {}
        r = float(m.get("ratio") or 0)
        if r <= 0 or int(m.get("samples") or 0) < _COST_RATIO_MIN_SAMPLES:
            return float(default)
        return min(_COST_RATIO_CLAMP[1], max(_COST_RATIO_CLAMP[0], r))
    except Exception:                   # noqa: BLE001 —— 读不到就按 default 走，不阻断
        return float(default)


# ---- 权限档位读写 --------------------------------------------------
# 值是 dict[会话键, mode]，读写两侧在此归一；历史遗留的 str 值仍按容器级默认识别。
from agent.core.permissions import current_permission_mode, set_permission_mode

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
    # 沙箱 confinement 后端
    "SpawnOptions",
    "ConfinementBackend",
    "NullBackend",
    "Wsl2Backend",
    "ContainerBackend",
    "BackendUnavailable",
    "select_backend",
    "describe_backend",
    "apply_wrap",
    "to_wsl_path",
    "BACKENDS",
    "STRENGTHS",
    # 桥能否在某个后端下跑
    "BRIDGE_PORTABILITY",
    "runnable_in",
    "is_portable",
    "unportable_bridges",
    "call_spawn_provider",
    # ctx.state 共享键
    "StateKeys",
    # 会话与租户身份
    "SessionIdentity",
    "SessionIdError",
    "build_task_id",
    "parse_task_id",
    "make_identity",
    "coerce_identity",
    "DEFAULT_TENANT",
    "DEFAULT_USER",
    "DEFAULT_SESSION",
    # 绑定当前请求所属的会话与 run
    "set_current_session_key",
    "current_session_key",
    "set_current_run_id",
    "current_run_id",
    # 配置视图接缝名
    "SESSION_CFG_VIEW",
    # 作用域配置
    "DEFAULT_SESSION_LIMIT",
    "merge_overrides",
    "section_of",
    "touch",
    "evict_lru",
    # 审计公共面
    "durable",
    "now_iso",
    # 请求标识、结构化日志与进程计数器
    "bind_request_id",
    "ensure_request_id",
    "current_request_id",
    "new_request_id",
    "trace_fields",
    "configure_logging",
    "inc_counter",
    "counters_snapshot",
    # 成本校准因子
    "cost_factor",
    # 权限档位读写
    "current_permission_mode",
    "set_permission_mode",
    # MCP stdio 客户端
    "McpStdioClient",
    # MCP inputSchema → 内核 task schema 的保真转换
    "build_task_schema",
    # token 估算与截断
    "estimate_tokens",
    "estimate_request_cost",
    "clip_text",
    "clip_json",
    "clip_text_hard",
    "guard_source",
    # 请求装配
    "wire_messages",
    "wire_tools",
    # 配置归一与校验
    "coerce_config",
    "validate_config",
    "DEFAULT_DB_PATH",
    # 提示词标记 SSOT
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
    # 能力接缝契约
    "LlmProvider",
    "ReactResultLike",
    # errors
    "AgentError",
    "CapabilityNotImplemented",
    "ConfigError",
    "CyclicDependencyError",
    "EngineUnavailable",
    "EventWiringError",
    "LlmUnavailableError",
    "MissingDependencyError",
    "ScopeClosedError",
    "ServiceConflictError",
    "ServiceNotFoundError",
    "TeardownError",
]
