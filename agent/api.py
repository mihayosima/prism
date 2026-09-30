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
from agent.core.mcp_client import McpStdioClient, build_task_schema
from agent.core.request_cost import estimate_request_cost
from agent.core.services_catalog import ALL as SERVICE_KEYS, Services
from agent.core.state_keys import StateKeys
from agent.core.tokens import (clip_json, clip_text, clip_text_hard, estimate_tokens,
                               guard_source)

# ---- 提示词标记 SSOT（D4）-------------------------------------------------
# 离线 FixtureLlm 据系统提示中的标记路由确定性响应；生产侧（chat/memory/react）
# 与夹具侧（fixtures/llm.py）一律引用本处常量，禁止第三处书写 `<!--...-->` 字面量。
MARK_CHAT_PLAN = "<!--chat:plan-->"
MARK_CHAT_ANSWER = "<!--chat:answer-->"
MARK_MEMORY_EXTRACT = "<!--memory:extract-->"
MARK_REACT_TURN = "<!--react:turn-->"
MARK_REACT_FINAL = "<!--react:final-->"


# ---- F1：口径校准因子（budget 写 LLM_COST_RATIO，消费方经本助手只读）----------

_COST_RATIO_CLAMP = (0.5, 3.0)          # 防异常样本把窗口裁到不可用/彻底放开
_COST_RATIO_MIN_SAMPLES = 5             # 冷启动样本不足不启用（宁裸估算，不引入抖动）


def cost_factor(ctx, default: float = 1.0) -> float:
    """估算→真实口径的校准因子（EWMA，按模型分桶；budget 维护，消费方只读）。

    语义：``真实计费 ≈ 估算 × factor``。样本不足 / 未维护 / 读取异常 → 1.0
    （退回裸估算现状，不劣化）。消费点：budget 预借判定、治理器装配预算。
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
    except Exception:                   # noqa: BLE001 —— 因子读取失败退裸估算，绝不阻断
        return float(default)

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
    # F1：口径校准因子（budget 维护 / 消费方经 api 只读）
    "cost_factor",
    # MCP stdio 客户端（实现位于 core/mcp_client；业务插件一律经本出口）
    "McpStdioClient",
    # MCP inputSchema → 内核 task schema 的保真转换（S6/P2：enum/anyOf/default 不丢）
    "build_task_schema",
    # token 原语（实现位于 core/tokens；插件/系统层一律经本出口，不得直连 core）
    "estimate_tokens",
    "estimate_request_cost",
    "clip_text",
    "clip_json",
    "clip_text_hard",
    "guard_source",
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