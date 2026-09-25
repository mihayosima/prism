"""agent.sys.budget —— 成本护栏（挂在模型接缝，非管线内嵌）。

- ``agent/llm/pre-call``（waterfall）：已花 + 本次估算 > 上限 → 否决（return 不带 next()）。
- ``agent/llm/post-call``（emit）：真实 token 入账，落 agent_budget_log。
- 状态放 ``ctx.state``（每容器隔离）。``max_tokens_per_run`` 是**容器生命周期累计**额度；
  ``reset_on_new_session``（默认 True）→ 网页"新会话"时经 ``agent/chat/reset`` 重置累计。
"""

from __future__ import annotations

from agent.api import Events, Services, StateKeys, durable, now_iso, plugin
from agent.sys.llm import _tokens


@plugin
class Budget:
    """ctx.budget —— 预算护栏。guard 用 prepend 先裁决；budget 后裁决（权限先、成本后）。"""

    name = "agent.budget"
    layer = "system"
    provides = [Services.BUDGET]
    inject = [Services.EVENTS, Services.DB, Services.LLM]

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("budget") or {}
        max_tokens = int(cfg.get("max_tokens_per_run", 10000))
        model = ((ctx.config or {}).get("agent") or {}).get("llm", {}).get("model", "fixture")
        ctx.state.setdefault(StateKeys.BUDGET_SPENT, 0)

        async def _pre(payload: dict, next) -> dict:
            # est 覆盖 messages + tools 目录 + schema（S1：不再漏算请求侧开销）
            est = (_tokens(payload.get("messages", []))
                   + _tokens(payload.get("tools") or [])
                   + _tokens(payload.get("schema") or {}))
            spent = ctx.state.get(StateKeys.BUDGET_SPENT, 0)
            if max_tokens and spent + est > max_tokens:
                return {"denied": True,
                        "reason": f"预算超限 spent={spent}+est={est}>{max_tokens}"}
            # 预借记账（确定性：与 post 竞态无关）；pending est **按 caller 分槽**（D3：
            # react 与 memory 并发调 LLM 时互不覆盖，est_cost 不再漏记）
            caller = str(payload.get("caller") or "unknown")
            ctx.state[StateKeys.BUDGET_SPENT] = spent + est
            ctx.state.setdefault(StateKeys.BUDGET_PENDING_EST, {})[caller] = est
            return await next()

        async def _post(payload: dict) -> None:
            # S1 对账：用实际入账替换预借（spent = spent - est + actual），
            # 账面精确等于 Σ实际；当前"实际"仍是估算口径，S6 接入真实 usage 后才有真实差值。
            ti = int(payload.get("tokens_in", 0))
            to = int(payload.get("tokens_out", 0))
            caller = str(payload.get("caller") or "unknown")
            pending = ctx.state.get(StateKeys.BUDGET_PENDING_EST) or {}
            est = int(pending.pop(caller, 0) or 0)
            spent = ctx.state.get(StateKeys.BUDGET_SPENT, 0)
            ctx.state[StateKeys.BUDGET_SPENT] = max(0, spent - est + (ti + to))
            await ctx.db.execute(
                ctx,
                "INSERT INTO agent_budget_log(model, tokens_in, tokens_out, est_cost, at)"
                " VALUES (?,?,?,?,?)",
                (model, ti, to, est or None, now_iso()),
            )

        def _call_failed(payload: dict) -> None:
            # S1：调用失败 → 全额退还本次预借（同步监听器，emit 内联执行立即生效）。
            caller = str(payload.get("caller") or "unknown")
            pending = ctx.state.get(StateKeys.BUDGET_PENDING_EST) or {}
            est = int(pending.pop(caller, 0) or 0)
            if est:
                ctx.state[StateKeys.BUDGET_SPENT] = max(
                    0, ctx.state.get(StateKeys.BUDGET_SPENT, 0) - est)

        ctx.on(Events.LLM_PRE_CALL, _pre)
        ctx.on(Events.LLM_POST_CALL, _post)
        ctx.on(Events.LLM_CALL_FAILED, _call_failed)
        if cfg.get("reset_on_new_session", True):
            def _reset_session(_payload: dict) -> None:      # 同步：emit 内联执行，立即生效
                ctx.state[StateKeys.BUDGET_SPENT] = 0
            ctx.on(Events.CHAT_RESET, _reset_session)
        ctx.provide(Services.BUDGET, self)


__all__ = ["Budget"]