"""agent.core.state_keys —— ``ctx.state`` 跨插件共享键 SSOT（D3）。

``ctx.state`` 是跨插件隐形共享面：键名裸字符串默契一旦改名即**静默失效**。
本模块把全部跨模块消费的键常量化——写方与读方一律 ``from agent.api import StateKeys``。
守卫测试（tests/test_decouple_guards）断言这些值字面量只出现在本文件。

键目录：
- ``BUDGET_SPENT``       budget 写、run/chat 状态栏读（容器生命周期累计已花 token）
- ``BUDGET_PENDING_EST`` budget 写读（预借 est 按 caller 分槽：``dict[caller, est]``）
- ``REACT_LAST``         react 写、react.status 读（最近一次 run 摘要）
- ``PERMISSION_MODE``    **前向契约**：approval 插件（未实施）写、react 读；当前无写者
- ``LOOP_RUNNING``       sys/loop 写读（在跑任务集合）
- ``MCP_TOOLS/ERROR``    MCP 桥模板键：``.format(bridge="windows_mcp")`` 等
"""

from __future__ import annotations


class StateKeys:
    """ctx.state 共享键（仅常量，禁止实例化）。"""

    BUDGET_SPENT = "budget_spent"
    BUDGET_PENDING_EST = "budget_pending_est"
    REACT_LAST = "react_last"
    PERMISSION_MODE = "permission_mode"      # 前向契约：approval 写 / react 读（当前无写者）
    LOOP_RUNNING = "loop_running"
    MCP_TOOLS = "{bridge}_tools"             # 模板键：.format(bridge="windows_mcp") 等
    MCP_ERROR = "{bridge}_error"


__all__ = ["StateKeys"]
