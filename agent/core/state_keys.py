"""agent.core.state_keys —— ``ctx.state`` 跨插件共享键 SSOT（D3）。

``ctx.state`` 是跨插件隐形共享面：键名裸字符串默契一旦改名即**静默失效**。
本模块把全部跨模块消费的键常量化——写方与读方一律 ``from agent.api import StateKeys``。
守卫测试（tests/test_decouple_guards）断言这些值字面量只出现在本文件。

键目录：
- ``BUDGET_SPENT``       budget 写、run/chat 状态栏读（容器生命周期累计已花 token）
- ``BUDGET_PENDING_EST`` budget 写读（预借 est 按 caller 分槽：``dict[caller, est]``）
- ``BUDGET_WARNING``     D1：budget 写、react 读（预算预警旗标 ``dict[at,spent,limit]``；
                         与 PERMISSION_MODE 同款「状态键前向契约」——双方零直连）
- ``LLM_COST_RATIO``     F1：budget 写、api.cost_factor/治理器消费方读（估算↔真实口径
                         EWMA 校准因子 ``dict[model, dict[ratio, samples]]``）
- ``REACT_RUN_ID``       F2：react 写、budget 读（当前 run 的归组键，落 agent_budget_log；
                         尽力而为——并发多会话时以后写者为准）
                         2.2 起值为 **task_id**（``{user}:{session}:{run}``）——即便被后写者
                         覆盖，账单行仍自带会话维度（治 H5 串号）
- ``SESSION_KEY``        2.2：run/chat 解析身份后写、approval 读（当前会话 ``{user}:{session}``）。
                         与 REACT_RUN_ID 同为「尽力而为」的当前上下文提示（``ctx.state`` 是
                         容器级，并发下以后写者为准）；它是**无参数线程化时**approval 判定
                         「这次请求属于哪个会话」的唯一依据
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
    BUDGET_WARNING = "budget_warning"        # D1：budget 写 / react 读（预警旗标，非直连）
    LLM_COST_RATIO = "llm_cost_ratio"        # F1：budget 写 / cost_factor 读（口径校准 EWMA）
    REACT_RUN_ID = "react_run_id"            # F2：react 写 / budget 读（账单归组键；= task_id）
    SESSION_KEY = "session_key"              # 2.2：run/chat 写 / approval 读（当前会话键）
    REACT_LAST = "react_last"
    PERMISSION_MODE = "permission_mode"      # 前向契约：approval 写 / react 读（当前无写者）
    LOOP_RUNNING = "loop_running"
    MCP_TOOLS = "{bridge}_tools"             # 模板键：.format(bridge="windows_mcp") 等
    MCP_ERROR = "{bridge}_error"


__all__ = ["StateKeys"]
