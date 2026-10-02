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
- ``REACT_RUN_ID``       F2：react 写、budget 读（当前 run 的归组键，落 agent_budget_log）。
                         **R1（2026-10-02）起主读通道是 ContextVar**（``current_run_id()``，
                         按 asyncio task 隔离 ⇒ 每请求独立）；本键仍照写，仅为兼容直接读
                         state 的调用点。2.2 起值为 **task_id**（``{user}:{session}:{run}``）
- ``SESSION_KEY``        2.2：run/chat 解析身份后写、approval 等读（当前会话 ``{user}:{session}``）。
                         **R1（2026-10-02）起主读通道是 ContextVar**（``current_session_key()``，
                         按请求隔离）；本键仍照写，仅为兼容直接读 state 的既有测试与调用点。
                         它是**无参数线程化时**（waterfall 监听只收纯数据 payload，拿不到 ctx）
                         判定「这次请求属于哪个会话」的唯一依据
- ``REACT_LAST``         react 写、react.status 读（最近一次 run 摘要）
- ``PERMISSION_MODE``    approval 写、react 读（档位 default/plan/auto/bypass）。
                         ⚠️ **语义是会话级、存储却在容器级**（单值）⇒ A 会话切 bypass 会放行
                         B 会话在跑 run 的审批闸。**R1 未覆盖此键**（它需要按会话分桶，属值存储
                         问题而非选择器问题），待裁定后修
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
    REACT_RUN_ID = "react_run_id"            # F2：react 写 / budget 读（归组键=task_id；R1 起读优先 ContextVar）
    SESSION_KEY = "session_key"              # 2.2：run/chat 写 / approval 等读（R1 起读优先 ContextVar）
    REACT_LAST = "react_last"
    PERMISSION_MODE = "permission_mode"      # approval 写 / react 读；⚠️ 语义会话级但存容器级（见模块头）
    LOOP_RUNNING = "loop_running"
    MCP_TOOLS = "{bridge}_tools"             # 模板键：.format(bridge="windows_mcp") 等
    MCP_ERROR = "{bridge}_error"


__all__ = ["StateKeys"]
