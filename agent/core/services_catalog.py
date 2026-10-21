"""agent.core.services_catalog —— 服务键唯一真源。

与事件名同类：服务键此前是裸字符串，拼错即静默失效。本模块把键常量化，使契约显式、可被 AST 守卫。

值不变：所有常量的值与历史裸字符串完全一致 ⇒ 纯重构，零行为改动。
插件自管键不在此列，由 ``test_services_contract`` 显式 allowlist 放行。

零业务词；内核不得反向依赖 sys/plugins。
"""

from __future__ import annotations


class Services:
    """服务键常量，值 = 历史裸字符串，勿改值，否则破坏既有装配。"""

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
    # : 受限循环工厂接缝：``(tools_override, cfg_overrides,
    # : components_override, governance_override, depth) -> ReactLoop``。
    # : 引擎无关契约：签名不绑定 ReactLoop，只要求返回对象
    # : 具备 ``run(ctx, prompt, identity) -> ReactResult`` 方法；子引擎换成 LangGraph 时
    # : 只改工厂实现一行，派遣契约不变。未装配 ⇒ None ⇒ ``agent.delegate`` 优雅降级。
    REACT_FACTORY = "react.factory"
    MEMORY = "memory"
    CONTEXT = "context"
    SYSTEM_MEMORY = "system_memory"
    WORK_MEMORY = "work_memory"
    SHORT_TERM = "short_term"
    WINDOWS_MCP = "windows_mcp"
    PLAYWRIGHT_MCP = "playwright_mcp"
    # : 子进程启动选项接缝：``(session_key) -> {"cwd":..., "env":{...}} | None``。
    # : 为什么是中性名字：桥只依赖内核契约，不该知道背后是谁在管隔离
    # : 若键名带具体提供方，
    # : 就把桥和某一实现焊死了 —— 换实现要改桥，正是解耦要避免的。
    # : 桥侧因此消费的是"内核定义的机制"，提供方是可替换的；未装配 ⇒ None ⇒ 不劣化。
    CHILD_SPAWN_OPTIONS = "mcp.child_spawn_options"
    # : 结构化待办/规划插件服务键：暴露 TodoWrite 能力；react 经 ctx.get 取实例读计划/进度。
    TODO = "todo"
    # : 技能目录服务键：暴露 ``SkillsCatalog``。
    # : react 经 ``ctx.get(Services.SKILLS)`` 反向读取，不 import skill 插件 ⇒ 单向解耦。
    # : 未装配 ⇒ None ⇒ react 回落「无技能」分支，零劣化。
    SKILLS = "skills.catalog"
    # : 只读技能 MCP 桥服务键：暴露桥实例供 status/探活。
    # : 桥把本仓技能目录以 MCP 工具对外暴露；
    # : 未启用时插件早退、不起子进程，但仍 provide 自身。
    SKILLS_MCP = "skills_mcp"
    # : 语音识别服务键：暴露 ``ASRService`` 门面
    # : 端点在 ``run/chat.py`` 经 ``ctx.root.get`` 软取，
    # : 插件未启用 ⇒ None ⇒ 端点 503 明示 ⇒ 不崩服务。
    ASR = "asr"


# 全部“内核服务键”值集合
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
        Services.TODO,
        Services.SKILLS,
        Services.SKILLS_MCP,
        Services.ASR,
    }
)


__all__ = ["Services", "ALL"]
