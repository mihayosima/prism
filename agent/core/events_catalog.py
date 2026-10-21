"""事件名常量单一来源：跨组件事件名集中在此，``emit`` / ``waterfall`` / ``ctx.on`` 一律引用 ``Events.X``。"""

from __future__ import annotations

from agent.core.errors import AgentError


class Events:
    # LLM 生命周期
    LLM_PRE_CALL = "agent/llm/pre-call"
    LLM_POST_CALL = "agent/llm/post-call"
    LLM_CALL_FAILED = "agent/llm/call-failed"   # provider 失败
    LLM_TRUNCATED = "agent/llm/truncated"       # finish_reason=length 生成被截断
    LLM_TOKEN = "agent/llm/token"          # 打字机：每片文本 {"text": 片段}
    LLM_THINKING = "agent/llm/thinking"    # 思考流：reasoning_content 每片 {"text": 片段}

    # 预算：跨阈值预警
    BUDGET_WARNING = "agent/budget/warning"
    # 口径：估算与真实计费偏差超阈值
    LLM_COST_DRIFT = "agent/llm/cost-drift"

    # 任务生命周期
    TASKS_PRE_EXECUTE = "agent/tasks/pre-execute"
    TASKS_POST_EXECUTE = "agent/tasks/post-execute"

    # 会话 / 聊天
    CHAT_IN = "agent/chat/in"
    CHAT_DONE = "agent/chat/done"
    CHAT_RESET = "agent/chat/reset"

    # 权限决策
    PERMISSION_DECISION = "agent/permission/decision"

    # 外部工具隔离档位自述：durable 直写。
    # 载荷：{mode, enforcement, ceiling, gap, enforced_by, bridge, targets…}。
    SANDBOX_CONFINEMENT = "agent/sandbox/confinement"

    # 审批插件：asked/answered/timeout/mode_changed 由 durable
    # 直写 agent_events；PENDING 为 live emit
    APPROVAL_ASKED = "agent/approval/asked"
    APPROVAL_ANSWERED = "agent/approval/answered"
    APPROVAL_TIMEOUT = "agent/approval/timeout"
    APPROVAL_MODE_CHANGED = "agent/approval/mode_changed"
    APPROVAL_PENDING = "agent/approval/pending"
    ASK_USER_PENDING = "agent/ask_user/pending"        # 澄清问询：选项+自由文本卡片
    ASK_USER_ANSWERED = "agent/ask_user/answered"      # 问询了结

    # ReAct 插件
    REACT_START = "agent/react/start"
    REACT_STEP_START = "agent/react/step/start"
    REACT_STEP = "agent/react/step"
    REACT_STEP_END = "agent/react/step/end"
    REACT_FINAL = "agent/react/final"
    REACT_EXHAUSTED = "agent/react/exhausted"
    REACT_ARCHIVED = "agent/react/archived"  # 链存档落工作记忆

    # 子 Agent 委派：spawn 审计。
    # 载荷：{parent_run_id, parent_session_key, child_run_id, child_session_key, role, depth, task, status}
    # 父子 run 关联靠 child_run_id，与父经 parent_run_id 关联。
    SUBAGENT_SPAWN = "agent/subagent/spawn"

    # 记忆插件
    MEMORY_REMEMBERED = "agent/memory/remembered"
    MEMORY_RECALL = "agent/memory/recall"

    # 规划插件：完成度实时帧
    TODO_PROGRESS = "agent/todo/progress"

    # 上下文治理插件：装配元数据
    CONTEXT_GOVERNED = "agent/context/governed"
    # 前缀稳定性体检红灯——续接但头部未冻结
    # ⇒ 整条链将在前缀缓存上失效。载荷：stage / reason / chain_entries。durable 直写。
    CONTEXT_PREFIX_UNSTABLE = "agent/context/prefix-unstable"
    # 水位体检。
    # 载荷：scope / phase / used / window / ratio / level / gates。纯观测。
    CONTEXT_WATERLINE = "agent/context/waterline"
    # 治理动词三事件。
    # 载荷：mode / actions / reason / gates / shadow。shadow 数据的唯一来源。
    CONTEXT_COMPACT_DECIDED = "agent/context/compact-decided"
    # 载荷：applied / skipped / released / invariants / degraded。执行审计。
    CONTEXT_COMPACT_APPLIED = "agent/context/compact-applied"
    # 载荷：invariant / target / detail。结构性错误即时暴露。
    CONTEXT_INVARIANT_VIOLATION = "agent/context/invariant-violation"


def all_events() -> set[str]:
    """返回所有事件名常量值。"""
    return {
        v
        for k, v in vars(Events).items()
        if not k.startswith("_") and isinstance(v, str)
    }


# 事件分发模式
EVENT_MODE: dict[str, str] = {
    Events.LLM_PRE_CALL: "waterfall",
    Events.LLM_POST_CALL: "emit",
    Events.LLM_CALL_FAILED: "emit",        # 失败审计/退款
    Events.LLM_TRUNCATED: "emit",          # 截断审计
    Events.LLM_TOKEN: "emit",              # 流式打字机
    Events.LLM_THINKING: "emit",           # 思考流：同属遥测性质，不进 required seam
    Events.BUDGET_WARNING: "emit",         # 跨阈值预警
    Events.LLM_COST_DRIFT: "emit",         # 口径漂移审计
    Events.TASKS_PRE_EXECUTE: "waterfall",
    Events.TASKS_POST_EXECUTE: "emit",
    Events.CHAT_IN: "emit",
    Events.CHAT_DONE: "emit",
    Events.CHAT_RESET: "emit",
    Events.PERMISSION_DECISION: "emit",   # durable 落盘
    Events.SANDBOX_CONFINEMENT: "emit",   # 同上
    Events.APPROVAL_ASKED: "emit",        # durable 直写；bus 无监听者
    Events.APPROVAL_ANSWERED: "emit",     # 同上
    Events.APPROVAL_TIMEOUT: "emit",      # 同上
    Events.APPROVAL_MODE_CHANGED: "emit",  # 同上
    Events.APPROVAL_PENDING: "emit",      # live：待审批弹窗信号
    Events.ASK_USER_PENDING: "emit",      # live：澄清问询卡片信号
    Events.ASK_USER_ANSWERED: "emit",     # live：问询了结
    Events.REACT_START: "emit",
    Events.REACT_STEP_START: "emit",
    Events.REACT_STEP: "emit",
    Events.REACT_STEP_END: "emit",
    Events.REACT_FINAL: "emit",
    Events.REACT_EXHAUSTED: "emit",
    Events.REACT_ARCHIVED: "emit",        # 链存档审计
    Events.SUBAGENT_SPAWN: "emit",        # 子 Agent 委派 spawn 审计
    Events.MEMORY_REMEMBERED: "emit",
    Events.MEMORY_RECALL: "emit",
    Events.TODO_PROGRESS: "emit",       # 规划完成度
    Events.CONTEXT_GOVERNED: "emit",      # 装配元数据遥测
    Events.CONTEXT_PREFIX_UNSTABLE: "emit",  # 前缀稳定性体检
    Events.CONTEXT_WATERLINE: "emit",        # 水位体检
    Events.CONTEXT_COMPACT_DECIDED: "emit",       # 压缩决策
    Events.CONTEXT_COMPACT_APPLIED: "emit",       # 执行审计
    Events.CONTEXT_INVARIANT_VIOLATION: "emit",   # 不变量违反告警
}


# capability 接缝：必须有 ≥ 1 个实时监听
# 本常量只表达"期望谁挂"，不表达执行顺序。
REQUIRED_SUBSCRIBER_EVENTS: frozenset[str] = frozenset({
    Events.LLM_PRE_CALL,
    Events.TASKS_PRE_EXECUTE,
})

# 每个 required seam 期望由哪些内核插件挂监听。
# 目录插件不进此表——它们可被用户停用。
SEAM_SUBSCRIBERS: dict[str, list[str]] = {
    Events.LLM_PRE_CALL: ["agent.guard", "agent.budget"],
    Events.TASKS_PRE_EXECUTE: ["agent.guard"],
}

# CHAT_RESET 故意不纳入：无监听仅导致预算不随新会话重置，属非安全项。

# SSOT 内部自洽：required seam 必为 waterfall。
# 用 raise 而非 assert：-O 会剥离 assert，安全契约不能依赖它；导入即崩，越早越好。
for _seam in REQUIRED_SUBSCRIBER_EVENTS:
    if EVENT_MODE.get(_seam) != "waterfall":
        raise AgentError(
            f"[events_catalog] 安全契约破坏：{_seam!r} 在 REQUIRED_SUBSCRIBER_EVENTS 中"
            f"必须是 waterfall，却在 EVENT_MODE 声明为 {EVENT_MODE.get(_seam)!r}"
        )
