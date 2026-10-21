"""agent.core.state_keys —— ``ctx.state`` 跨插件共享键 SSOT。

``ctx.state`` 是跨插件隐形共享面：键名裸字符串默契一旦改名即静默失效。
本模块把全部跨模块消费的键常量化——写方与读方一律 ``from agent.api import StateKeys``。
守卫测试断言这些值字面量只出现在本文件。

键目录：
- ``BUDGET_SPENT`` budget 写、run/chat 状态栏读
- ``BUDGET_PENDING_EST`` budget 写读
- ``BUDGET_WARNING`` D1：budget 写、react 读
- ``LLM_COST_RATIO`` F1：budget 写、api.cost_factor/治理器消费方读
- ``REACT_RUN_ID`` F2：react 写、budget 读。
 R1 起主读通道是 ContextVar；本键仍照写，仅为兼容直接读
 state 的调用点。2.2 起值为 task_id
- ``SESSION_KEY`` 2.2：run/chat 解析身份后写、approval 等读。
 R1 起主读通道是 ContextVar；本键仍照写，仅为兼容直接读 state 的既有测试与调用点。
 它是无参数线程化时
 判定「这次请求属于哪个会话」的唯一依据
- ``REACT_LAST`` react 写、react.status 读
- ``PERMISSION_MODE`` approval 写、react 读。
 值是 ``dict[会话键, mode]``——此前是单值 str。读写一律经内核助手
 ``permission_mode()`` / ``set_permission_mode()``
- ``LOOP_RUNNING`` sys/loop 写读
- ``MCP_TOOLS/ERROR`` MCP 桥模板键：``.format(bridge="windows_mcp")`` 等
"""

from __future__ import annotations


class StateKeys:
    """ctx.state 共享键。"""

    BUDGET_SPENT = "budget_spent"
    BUDGET_PENDING_EST = "budget_pending_est"
    BUDGET_WARNING = "budget_warning"        # budget 写 / react 读
    LLM_COST_RATIO = "llm_cost_ratio"        # budget 写 / cost_factor 读
    REACT_RUN_ID = "react_run_id"            # react 写 / budget 读
    SESSION_KEY = "session_key"              # 2.2：run/chat 写 / approval 等读
    REACT_LAST = "react_last"
    PERMISSION_MODE = "permission_mode"      # approval 写 / react 读；值 = dict[会话键, mode]
    LOOP_RUNNING = "loop_running"
    MCP_TOOLS = "{bridge}_tools"             # 模板键：.format(bridge="windows_mcp") 等
    MCP_ERROR = "{bridge}_error"


__all__ = ["StateKeys"]
