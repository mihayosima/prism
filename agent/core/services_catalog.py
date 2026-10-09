"""agent.core.services_catalog —— 服务键唯一真源（SSOT）。

与事件名（``Events``）同类：服务键此前是裸字符串（``ctx.get("events")``、``provides=["llm"]``、
``inject=["events","db"]``），拼错即**静默失效**。本模块把键常量化，使契约显式、可被 AST 守卫。

**值不变**：所有常量的值与历史裸字符串完全一致 ⇒ 纯重构，零行为改动。
插件自管键（如 ``migrations``）不在此列，由 ``test_services_contract`` 显式 allowlist 放行。

零业务词（test_agent_purity 守卫）；内核不得反向依赖 sys/plugins（K0 守卫）。
"""

from __future__ import annotations


class Services:
    """服务键常量（值 = 历史裸字符串，勿改值，否则破坏既有装配）。"""

    EVENTS = "events"
    LLM = "llm"
    TASKS = "tasks"
    DB = "db"
    CHAT = "chat"
    LOOP = "loop"
    GUARD = "guard"
    BUDGET = "budget"
    APPROVAL = "approval"
    REACT = "react"
    #: **受限循环工厂**接缝（子 Agent 插件用）：``(tools_override, cfg_overrides,
    #: components_override, governance_override, depth) -> ReactLoop``。
    #:
    #: ★ 引擎无关契约（子 Agent 方案 v0.3 §3.2）：签名不绑定 ReactLoop，只要求返回对象
    #: 具备 ``run(ctx, prompt, identity) -> ReactResult`` 方法；子引擎换成 LangGraph 时
    #: 只改工厂实现一行，派遣契约不变。未装配 ⇒ None ⇒ ``agent.delegate`` 优雅降级（不注册）。
    REACT_FACTORY = "react.factory"
    MEMORY = "memory"
    CONTEXT = "context"
    SYSTEM_MEMORY = "system_memory"
    WORK_MEMORY = "work_memory"
    SHORT_TERM = "short_term"
    WINDOWS_MCP = "windows_mcp"
    PLAYWRIGHT_MCP = "playwright_mcp"
    #: **子进程启动选项**接缝（2026-10-04）：``(session_key) -> {"cwd":..., "env":{...}} | None``。
    #:
    #: ★ **为什么是中性名字**：桥（MCP 插件）只依赖内核契约，**不该知道**背后是谁在管隔离
    #: （今天是沙箱，将来可能是受限令牌后端或别的 confinement 实现）。若键名带具体提供方，
    #: 就把桥和某一实现焊死了 —— 换实现要改桥，正是解耦要避免的。
    #: 桥侧因此消费的是"内核定义的机制"，提供方是**可替换**的；未装配 ⇒ None ⇒ 不劣化。
    CHILD_SPAWN_OPTIONS = "mcp.child_spawn_options"


# 全部“内核服务键”值集合（供 AST 契约测试比对）
ALL = frozenset(
    {
        Services.EVENTS,
        Services.LLM,
        Services.TASKS,
        Services.DB,
        Services.CHAT,
        Services.LOOP,
        Services.GUARD,
        Services.BUDGET,
        Services.APPROVAL,
        Services.REACT,
        Services.REACT_FACTORY,
        Services.MEMORY,
        Services.CONTEXT,
        Services.SYSTEM_MEMORY,
        Services.WORK_MEMORY,
        Services.SHORT_TERM,
        Services.WINDOWS_MCP,
        Services.PLAYWRIGHT_MCP,
        Services.CHILD_SPAWN_OPTIONS,
    }
)


__all__ = ["Services", "ALL"]
