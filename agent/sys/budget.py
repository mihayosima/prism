"""agent.sys.budget —— 成本护栏（挂在模型接缝，非管线内嵌）。

- ``agent/llm/pre-call``（waterfall）：已花 + 本次估算 > 上限 → 否决（return 不带 next()）。
- ``agent/llm/post-call``（emit）：真实 token 入账，落 agent_budget_log。
- 状态放 ``ctx.state``（每容器隔离）。``max_tokens_per_run`` 是**容器生命周期累计**额度；
  ``reset_on_new_session``（默认 True）→ 网页"新会话"时经 ``agent/chat/reset`` 重置累计。
"""

from __future__ import annotations

from agent.api import Events, Services, plugin
from agent.sys.audit import _now_iso
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
        ctx.state.setdefault("budget_spent", 0)

        async def _pre(payload: dict, next) -> dict:
            est = _tokens(payload.get("messages", []))
            spent = ctx.state.get("budget_spent", 0)
            if max_tokens and spent + est > max_tokens:
                return {"denied": True,
                        "reason": f"预算超限 spent={spent}+est={est}>{max_tokens}"}
            # 预借记账（确定性：与 post 竞态无关；est 与真实同口径）
            ctx.state["budget_spent"] = spent + est
            return await next()

        async def _post(payload: dict) -> None:
            # 记账行（实际 tokens）；花费已在 pre 借用，这里只落账审计
            ti = int(payload.get("tokens_in", 0))
            to = int(payload.get("tokens_out", 0))
            await ctx.db.execute(
                ctx,
                "INSERT INTO agent_budget_log(model, tokens_in, tokens_out, est_cost, at)"
                " VALUES (?,?,?,?,?)",
                (model, ti, to, None, _now_iso()),
            )

        ctx.on(Events.LLM_PRE_CALL, _pre)
        ctx.on(Events.LLM_POST_CALL, _post)
        if cfg.get("reset_on_new_session", True):
            def _reset_session(_payload: dict) -> None:      # 同步：emit 内联执行，立即生效
                ctx.state["budget_spent"] = 0
            ctx.on(Events.CHAT_RESET, _reset_session)
        ctx.provide(Services.BUDGET, self)


__all__ = ["Budget"]