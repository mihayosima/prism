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

# ---- 提示词标记 SSOT（D4）-------------------------------------------------
# 离线 FixtureLlm 据系统提示中的标记路由确定性响应；生产侧（chat/memory/react）
# 与夹具侧（fixtures/llm.py）一律引用本处常量，禁止第三处书写 `<!--...-->` 字面量。
MARK_CHAT_PLAN = "<!--chat:plan-->"
MARK_CHAT_ANSWER = "<!--chat:answer-->"
MARK_MEMORY_EXTRACT = "<!--memory:extract-->"
MARK_REACT_TURN = "<!--react:turn-->"
MARK_REACT_FINAL = "<!--react:final-->"


# ---- G2（会话级）：配置视图接缝名 ------------------------------------------
# 适配层（run/*）把 ``view(section, session_key) -> dict`` 挂到该键上；插件侧
# ``ctx.get(SESSION_CFG_VIEW)`` **软取**，缺失即退化为容器级配置（行为等价、不裸崩）。
# 之所以放内核：插件不得 import 适配层，接缝名必须双方可见且不产生反向依赖。
SESSION_CFG_VIEW = "session.cfg_view"

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


# ---- 权限档位（PERMISSION_MODE）的读写助手 ------------------------------------
# 为什么放内核出口：该键有**两个互不相识的消费方**——approval（判定闸门）与 react
# （工具可见性裁剪 + 状态上报）。若各自判断值形态，就会出现第二处实现（形状 SSOT 破裂）。
# R1 补（2026-10-02）：值由**单值 str** 改为 ``dict[会话键, mode]``（与 BUDGET_SPENT 同款
# 分桶）——此前是「会话级语义 + 容器级存储」⇒ A 会话切 bypass 会放行 B 会话在跑 run 的
# 审批闸。读写两侧经本处归一，**已存在的 str 旧值仍被识别**（视作容器级默认）以兼容直接
# 写 state 的既有调用点与测试。

_PERMISSION_BUCKET_LIMIT = 512          # 只防长期运行无限增长（同 budget 的桶上限口径）


def current_permission_mode(ctx, default: str = "default") -> str:
    """当前**会话**的权限档位（跨插件共享键的形状容错读取，SSOT）。

    解析顺序：``dict`` 桶中本会话的值 → 整值为 ``str`` 的旧形状（视作容器级默认）
    → ``default``。读取异常一律退 ``default``（绝不阻断主链路）。
    """
    try:
        raw = (getattr(ctx, "state", None) or {}).get(StateKeys.PERMISSION_MODE)
    except Exception:                   # noqa: BLE001
        return str(default)
    if isinstance(raw, dict):
        return str(raw.get(current_session_key(ctx)) or default)
    if isinstance(raw, str) and raw:
        return raw
    return str(default)


def set_permission_mode(ctx, mode: str) -> None:
    """把**当前会话**的档位写入分桶（整值替换，不做原地改 ⇒ 并发下无半更新态）。

    非 ``dict`` 的旧值按容器级默认丢弃（该旧值只可能是单值形态，无法归属到会话）。
    """
    sk = current_session_key(ctx)
    try:
        raw = (getattr(ctx, "state", None) or {}).get(StateKeys.PERMISSION_MODE)
    except Exception:                   # noqa: BLE001
        raw = None
    bucket = dict(raw) if isinstance(raw, dict) else {}
    bucket.pop(sk, None)                          # 重插以把本会话推到"最新"侧
    bucket[sk] = str(mode)
    while len(bucket) > _PERMISSION_BUCKET_LIMIT:
        bucket.pop(next(iter(bucket)), None)      # dict 保序：最早插入者先淘汰
    ctx.state[StateKeys.PERMISSION_MODE] = bucket

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
    # 会话/租户身份原语（阶段 2.2；实现位于 core/session，纯值对象 + 纯函数）
    "SessionIdentity",
    "SessionIdError",
    "build_task_id",
    "parse_task_id",
    "make_identity",
    "coerce_identity",
    "DEFAULT_TENANT",
    "DEFAULT_USER",
    "DEFAULT_SESSION",
    # R1（请求作用域绑定）：旁路会话键/归组键的强隔离原语。消费点多为**无 ctx 入参**
    # 的事件回调或装配期捕获的 ctx，故必须有一处公共原语可读（实现位于 core/session）。
    "set_current_session_key",
    "current_session_key",
    "set_current_run_id",
    "current_run_id",
    # G2（会话级）：配置视图接缝名（适配层注入 / 插件软取）
    "SESSION_CFG_VIEW",
    # G2（会话级）：作用域配置**纯规则**（实现位于 core/scope_config；适配层经本出口取用，
    # 不得直连 core——分层守卫 test_layered_import_direction 对此有硬断言）
    "DEFAULT_SESSION_LIMIT",
    "merge_overrides",
    "section_of",
    "touch",
    "evict_lru",
    # 审计公共面（D4 转正；实现位于 core/audit）
    "durable",
    "now_iso",
    # 可观测性（路线图 #5；实现位于 core/obs）：请求标识贯穿 + 结构化日志 + 进程计数器
    "bind_request_id",
    "ensure_request_id",
    "current_request_id",
    "new_request_id",
    "trace_fields",
    "configure_logging",
    "inc_counter",
    "counters_snapshot",
    # F1：口径校准因子（budget 维护 / 消费方经 api 只读）
    "cost_factor",
    # 权限档位的形状容错读写（approval 与 react 两个消费方共用，避免形状判断分叉）
    "current_permission_mode",
    "set_permission_mode",
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
    # 请求装配原语（实现位于 core/request_cost；供 sys 层经本出口取用）
    "wire_messages",
    "wire_tools",
    # 配置归一/校验（实现位于 core/config_schema；装配根 profiles 经本出口取用）
    "coerce_config",
    "validate_config",
    "DEFAULT_DB_PATH",
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
    "LlmUnavailableError",
    "MissingDependencyError",
    "ScopeClosedError",
    "ServiceConflictError",
    "ServiceNotFoundError",
    "TeardownError",
]
