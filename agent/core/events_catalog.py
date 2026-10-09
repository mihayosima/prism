"""agent.core.events_catalog —— 事件名单一真相源（SSOT）。

所有跨组件事件名集中在此；``emit`` / ``durable`` / ``waterfall`` / ``ctx.on``
一律引用 ``Events.X`` 而非手写字符串，使改名 / 拼错在**导入或装配期**即报错，
而非运行期静默失效（Guard / Budget 失联即此类隐患）。

值字符串与历史完全一致——本文件只统一引用方式，不改变任何运行时行为。
"""

from __future__ import annotations

from agent.core.errors import AgentError


class Events:
    # LLM 生命周期
    LLM_PRE_CALL = "agent/llm/pre-call"
    LLM_POST_CALL = "agent/llm/post-call"
    LLM_CALL_FAILED = "agent/llm/call-failed"   # S1：provider 失败（durable 审计 + budget 退款）
    LLM_TRUNCATED = "agent/llm/truncated"       # E：finish_reason=length 生成被截断（durable 审计）
    LLM_TOKEN = "agent/llm/token"          # B5.2 打字机：每片文本 {"text": 片段}
    LLM_THINKING = "agent/llm/thinking"    # B5.2 思考流（D4）：reasoning_content 每片 {"text": 片段}

    # 预算（D1）：跨阈值预警（budget 发；消费方按需读 BUDGET_WARNING 状态键，零直连）
    BUDGET_WARNING = "agent/budget/warning"
    # 口径（F3）：估算与真实计费偏差超阈值（budget durable 落盘，可事后回放）
    LLM_COST_DRIFT = "agent/llm/cost-drift"

    # 任务（工具）生命周期
    TASKS_PRE_EXECUTE = "agent/tasks/pre-execute"
    TASKS_POST_EXECUTE = "agent/tasks/post-execute"

    # 会话 / 聊天
    CHAT_IN = "agent/chat/in"
    CHAT_DONE = "agent/chat/done"
    CHAT_RESET = "agent/chat/reset"

    # 权限决策（guard 落审计；前端「权限」过滤用同名）
    PERMISSION_DECISION = "agent/permission/decision"

    # 外部工具隔离档位自述（plugins.sandbox）：durable 直写（不经总线）。
    # 2026-10-03 转型：事件名由 ``agent/sandbox/decision`` 改为 ``agent/sandbox/confinement``——
    # 沙箱**不再拥有准入裁决权**（那份判定与 guard 重叠，已收口），它现在只回答一个问题：
    # "这次调用**在什么隔离档位下跑、实际强度多少**"。名字里留着 "decision" 会让读者
    # 误以为它仍在做准入决策。
    # 载荷：``{mode, enforcement, ceiling, gap, enforced_by, bridge, targets…}``。
    # ★ ``enforcement`` 是**自报的实际强度**（当前恒 ``none``）——"未实现隔离"必须可见，
    # 否则插件页显示"一切正常"就是误导（对齐 DeepSeek Harness 的 ``SandboxEnforcement``）。
    # 代价：前端「事件」视图的 ``agent/sandbox`` 过滤项与旧名不匹配，需同步更新。
    SANDBOX_CONFINEMENT = "agent/sandbox/confinement"

    # 审批插件（plugins.approval）：asked/answered/timeout/mode_changed 由 durable
    # 直写 agent_events（不经总线）；PENDING 为 live emit（第 2 期前端弹窗消费）
    APPROVAL_ASKED = "agent/approval/asked"
    APPROVAL_ANSWERED = "agent/approval/answered"
    APPROVAL_TIMEOUT = "agent/approval/timeout"
    APPROVAL_MODE_CHANGED = "agent/approval/mode_changed"
    APPROVAL_PENDING = "agent/approval/pending"
    ASK_USER_PENDING = "agent/ask_user/pending"        # 澄清问询：选项+自由文本卡片
    ASK_USER_ANSWERED = "agent/ask_user/answered"      # 问询了结（前端卡片消隐）

    # ReAct 插件
    REACT_START = "agent/react/start"
    REACT_STEP_START = "agent/react/step/start"
    REACT_STEP = "agent/react/step"
    REACT_STEP_END = "agent/react/step/end"
    REACT_FINAL = "agent/react/final"
    REACT_EXHAUSTED = "agent/react/exhausted"
    REACT_ARCHIVED = "agent/react/archived"  # P1：链存档落工作记忆（审计）

    # 子 Agent 委派（plugins.subagent）：spawn 审计（durable 直写 agent_events；无 live 监听者）。
    # 载荷：{parent_run_id, parent_session_key, child_run_id, child_session_key, role, depth, task, status}
    # 父子 run 关联靠 child_run_id（子循环自生成的 uuid4.hex），与父经 parent_run_id 关联。
    SUBAGENT_SPAWN = "agent/subagent/spawn"

    # 记忆插件
    MEMORY_REMEMBERED = "agent/memory/remembered"
    MEMORY_RECALL = "agent/memory/recall"

    # 上下文治理插件（G1 分块装配）：装配元数据
    # （各块实占 / 裁剪 / 命中 / 刚性超限 / 回吐；遥测性质，非安全 seam）
    CONTEXT_GOVERNED = "agent/context/governed"
    # G2.0（P0/F-1）：前缀稳定性体检红灯——**续接**但头部未冻结（旧存档 / 对齐开关关闭）
    # ⇒ 链之前的头部字节与本轮不同，整条链将在前缀缓存上失效（9-29 成本事故的机制）。
    # 载荷：stage / reason / chain_entries。durable 直写（审计脊柱），无 live 监听者。
    CONTEXT_PREFIX_UNSTABLE = "agent/context/prefix-unstable"
    # G2.0（P1）：**水位体检**（enum 与 EVENT_MODE 两处同时登记，项目硬约束）。
    # 载荷：scope / phase / used / window / ratio / level / gates。纯观测（水位可回放）。
    CONTEXT_WATERLINE = "agent/context/waterline"
    # G2.0（P2）：治理动词三事件（**enum 与 EVENT_MODE 两处同时登记**，项目硬约束）。
    # 与 P2 代码同批登记——契约守卫（test_every_event_constant_is_used）禁止「登记了却无人用」
    # 的孤儿常量（这是守卫的正确拦截，不是偏离）。
    # 载荷：mode / actions / reason / gates / shadow。**shadow 数据的唯一来源**（决策可回放）。
    CONTEXT_COMPACT_DECIDED = "agent/context/compact-decided"
    # 载荷：applied / skipped / released / invariants / degraded。执行审计（账实相符）。
    CONTEXT_COMPACT_APPLIED = "agent/context/compact-applied"
    # 载荷：invariant / target / detail。结构性错误即时暴露（I-1 / I-2 违反）。
    CONTEXT_INVARIANT_VIOLATION = "agent/context/invariant-violation"


def all_events() -> set[str]:
    """返回所有事件名常量值（供契约一致性测试使用）。"""
    return {
        v
        for k, v in vars(Events).items()
        if not k.startswith("_") and isinstance(v, str)
    }


# ---------------------------------------------------------------------------
# 事件分发模式（与架构文档 §8 一致；SSOT 元数据）
# ---------------------------------------------------------------------------
EVENT_MODE: dict[str, str] = {
    Events.LLM_PRE_CALL: "waterfall",
    Events.LLM_POST_CALL: "emit",
    Events.LLM_CALL_FAILED: "emit",        # S1：失败审计/退款（live 通知 budget；durable 由 llm 接缝直写）
    Events.LLM_TRUNCATED: "emit",          # E：截断审计（durable 由 llm 接缝直写；无 live 监听者）
    Events.LLM_TOKEN: "emit",              # B5.2 流式打字机（遥测性质，不进 required seam）
    Events.LLM_THINKING: "emit",           # B5.2 思考流（D4）：同属遥测性质，不进 required seam
    Events.BUDGET_WARNING: "emit",         # D1：跨阈值预警（durable 由 budget 直写；live 通知遥测面）
    Events.LLM_COST_DRIFT: "emit",         # F3：口径漂移审计（durable 由 budget 直写；无 live 监听者）
    Events.TASKS_PRE_EXECUTE: "waterfall",
    Events.TASKS_POST_EXECUTE: "emit",
    Events.CHAT_IN: "emit",
    Events.CHAT_DONE: "emit",
    Events.CHAT_RESET: "emit",
    Events.PERMISSION_DECISION: "emit",   # durable 落盘（审计脊柱）
    Events.SANDBOX_CONFINEMENT: "emit",   # 同上（plugins.sandbox 隔离档位自述，另立事件）
    Events.APPROVAL_ASKED: "emit",        # durable 直写（审计脊柱）；bus 无监听者
    Events.APPROVAL_ANSWERED: "emit",     # 同上
    Events.APPROVAL_TIMEOUT: "emit",      # 同上
    Events.APPROVAL_MODE_CHANGED: "emit",  # 同上
    Events.APPROVAL_PENDING: "emit",      # live：待审批弹窗信号（第 2 期前端消费）
    Events.ASK_USER_PENDING: "emit",      # live：澄清问询卡片信号（ask_user 工具）
    Events.ASK_USER_ANSWERED: "emit",     # live：问询了结（前端卡片消隐）
    Events.REACT_START: "emit",
    Events.REACT_STEP_START: "emit",
    Events.REACT_STEP: "emit",
    Events.REACT_STEP_END: "emit",
    Events.REACT_FINAL: "emit",
    Events.REACT_EXHAUSTED: "emit",
    Events.REACT_ARCHIVED: "emit",        # P1：链存档审计（durable 由 react 直写）
    Events.SUBAGENT_SPAWN: "emit",        # 子 Agent 委派 spawn 审计（durable 由 subagent 直写）
    Events.MEMORY_REMEMBERED: "emit",
    Events.MEMORY_RECALL: "emit",
    Events.CONTEXT_GOVERNED: "emit",      # G1：装配元数据遥测（无监听者即 no-op）
    Events.CONTEXT_PREFIX_UNSTABLE: "emit",  # P0：前缀稳定性体检（durable 由 react 直写）
    Events.CONTEXT_WATERLINE: "emit",        # P1：水位体检（遥测；无监听者即 no-op）
    Events.CONTEXT_COMPACT_DECIDED: "emit",       # P2：压缩决策（shadow 数据来源）
    Events.CONTEXT_COMPACT_APPLIED: "emit",       # P2：执行审计（账实相符）
    Events.CONTEXT_INVARIANT_VIOLATION: "emit",   # P2：不变量违反告警
}


# ---------------------------------------------------------------------------
# capability 接缝：必须有 ≥ 1 个实时监听（安全闸门）
# ---------------------------------------------------------------------------
# LLM_PRE_CALL：guard（prepend）+ budget（append）→ 期望 2 方。**guard 是唯一挂载者**
#   （``plugins.sandbox`` 完全不挂该 seam ⇒ 它管不到模型调用）。
# TASKS_PRE_EXECUTE：期望 ≥ 1 方。**实际有三方**（2026-10-03 更正）：
#   guard（prepend）+ plugins.sandbox（prepend）+ plugins.approval（append）。
#   ⚠️ 两个 prepend 争同一位置，而 ``prepend`` 是 ``insert(0, …)`` ⇒ **后注册者在前**。
#   装配序：``profiles.py:137`` 先装 SYS_PLUGINS（guard）→ ``:145`` 再装目录插件（sandbox）
#   ⇒ **实际执行顺序 sandbox → guard → approval**。改装配顺序会改变裁决优先级。
#   本常量只表达"期望谁挂"（用于 fail-fast 报错文案），**不表达执行顺序**，勿据它推断。
REQUIRED_SUBSCRIBER_EVENTS: frozenset[str] = frozenset({
    Events.LLM_PRE_CALL,
    Events.TASKS_PRE_EXECUTE,
})

# 每个 required seam 期望由哪些内核插件挂监听（仅用于 fail-fast 报错信息，
# 便于"当前挂了谁 / 期望谁挂"对照；不用于断言强制，避免与运行期裁剪冲突）。
# ⚠️ 目录插件（``plugins.*``）不进此表——它们可被用户停用，列进来会让报错文案撒谎。
SEAM_SUBSCRIBERS: dict[str, list[str]] = {
    Events.LLM_PRE_CALL: ["agent.guard", "agent.budget"],
    Events.TASKS_PRE_EXECUTE: ["agent.guard"],
}

# CHAT_RESET 故意不纳入 REQUIRED_SUBSCRIBER_EVENTS：
#   无监听仅导致预算不随新会话重置（reset_on_new_session 失效），属非安全项
#   （不阻断执行）；若未来变为安全相关，移入上方集合即可。

# SSOT 内部自洽：required seam 必为 waterfall（不调 next() 即否决的 capability 闸门）。
# 用 raise 而非 assert：-O 会剥离 assert，安全契约不能依赖它；导入即崩，越早越好。
for _seam in REQUIRED_SUBSCRIBER_EVENTS:
    if EVENT_MODE.get(_seam) != "waterfall":
        raise AgentError(
            f"[events_catalog] 安全契约破坏：{_seam!r} 在 REQUIRED_SUBSCRIBER_EVENTS 中"
            f"必须是 waterfall，却在 EVENT_MODE 声明为 {EVENT_MODE.get(_seam)!r}"
        )
