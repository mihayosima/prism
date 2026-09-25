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
    LLM_TOKEN = "agent/llm/token"          # B5.2 打字机：每片文本 {"text": 片段}
    LLM_THINKING = "agent/llm/thinking"    # B5.2 思考流（D4）：reasoning_content 每片 {"text": 片段}

    # 任务（工具）生命周期
    TASKS_PRE_EXECUTE = "agent/tasks/pre-execute"
    TASKS_POST_EXECUTE = "agent/tasks/post-execute"

    # 会话 / 聊天
    CHAT_IN = "agent/chat/in"
    CHAT_DONE = "agent/chat/done"
    CHAT_RESET = "agent/chat/reset"

    # 权限决策（guard 落审计；前端「权限」过滤用同名）
    PERMISSION_DECISION = "agent/permission/decision"

    # ReAct 插件
    REACT_START = "agent/react/start"
    REACT_STEP_START = "agent/react/step/start"
    REACT_STEP = "agent/react/step"
    REACT_STEP_END = "agent/react/step/end"
    REACT_FINAL = "agent/react/final"
    REACT_EXHAUSTED = "agent/react/exhausted"

    # 记忆插件
    MEMORY_REMEMBERED = "agent/memory/remembered"
    MEMORY_RECALL = "agent/memory/recall"
    # 上下文治理插件：agent/context/governed 事件名随 G1 接线（装配元数据 durable）
    # 一并登记——SSOT 守卫要求常量被源码引用，未接线的名字不提前入册。


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
    Events.LLM_TOKEN: "emit",              # B5.2 流式打字机（遥测性质，不进 required seam）
    Events.LLM_THINKING: "emit",           # B5.2 思考流（D4）：同属遥测性质，不进 required seam
    Events.TASKS_PRE_EXECUTE: "waterfall",
    Events.TASKS_POST_EXECUTE: "emit",
    Events.CHAT_IN: "emit",
    Events.CHAT_DONE: "emit",
    Events.CHAT_RESET: "emit",
    Events.PERMISSION_DECISION: "emit",   # durable 落盘（审计脊柱）
    Events.REACT_START: "emit",
    Events.REACT_STEP_START: "emit",
    Events.REACT_STEP: "emit",
    Events.REACT_STEP_END: "emit",
    Events.REACT_FINAL: "emit",
    Events.REACT_EXHAUSTED: "emit",
    Events.MEMORY_REMEMBERED: "emit",
    Events.MEMORY_RECALL: "emit",
}


# ---------------------------------------------------------------------------
# capability 接缝：必须有 ≥ 1 个实时监听（安全闸门）
# ---------------------------------------------------------------------------
# LLM_PRE_CALL：guard（prepend 先裁决）+ budget（后裁决）→ 期望 2 方
# TASKS_PRE_EXECUTE：仅 guard 裁决 → 期望 1 方
REQUIRED_SUBSCRIBER_EVENTS: frozenset[str] = frozenset({
    Events.LLM_PRE_CALL,
    Events.TASKS_PRE_EXECUTE,
})

# 每个 required seam 期望由哪些内核插件挂监听（仅用于 fail-fast 报错信息，
# 便于"当前挂了谁 / 期望谁挂"对照；不用于断言强制，避免与运行期裁剪冲突）。
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
