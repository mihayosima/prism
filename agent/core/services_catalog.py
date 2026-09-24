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
    REACT = "react"
    MEMORY = "memory"
    WINDOWS_MCP = "windows_mcp"
    PLAYWRIGHT_MCP = "playwright_mcp"
    UI_THEME = "ui_theme"


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
        Services.REACT,
        Services.MEMORY,
        Services.WINDOWS_MCP,
        Services.PLAYWRIGHT_MCP,
        Services.UI_THEME,
    }
)


__all__ = ["Services", "ALL"]
