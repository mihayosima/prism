"""agent.sys.budget —— 成本护栏（挂在模型接缝，非管线内嵌）。

- ``agent/llm/pre-call``（waterfall）：已花 + 本次估算 > 上限 → 否决（return 不带 next()）。
  方案A：``finalize_reserve_tokens``（默认 24000，配置化）为收尾调用预留末段额度——
  正常调用上限是 ``max-reserve``，带 ``finalize=True`` 的收尾调用仍可用至 ``max``。
- D1：``warning_ratio``（默认 0.8，<=0 关闭）——花费预估跨 ``ratio*max`` 阈值时置
  ``StateKeys.BUDGET_WARNING`` 旗标 + durable/live ``agent/budget/warning`` 事件（各一次）。
  消费方（react）按需读状态键注入「尽快收束」提示——**状态键前向契约，双方零直连**。
- F1：``cost_calibration``（默认 True）——post-call 用「真实 tokens_in ÷ 当次裸估算」
  维护按模型分桶的 EWMA 校准因子（``StateKeys.LLM_COST_RATIO``），pre-call 估算乘因子
  后再判定/预借；治理器装配经 ``api.cost_factor`` 读同一因子（彻底作用，双消费点）。
  样本 <5 或因子越界 [0.5,3] 时诚实退裸估算/夹紧。
- F2/F3/P3：``run_id``（react 经 ``StateKeys.REACT_RUN_ID`` 提供，尽力归组）与
  ``cache_hit``（DeepSeek 前缀缓存命中，P3 计量）随账落 ``agent_budget_log``
  （缺列幂等迁移）；估算↔真实偏差 >25% 发 durable ``agent/llm/cost-drift``（F3）。
- ``agent/llm/post-call``（emit）：真实 token 入账，落 agent_budget_log。
- 状态放 ``ctx.state``（每容器隔离）。``max_tokens_per_run`` 是**容器生命周期累计**额度；
  ``reset_on_new_session``（默认 True）→ 网页"新会话"时经 ``agent/chat/reset`` 重置累计。
"""

from __future__ import annotations

from agent.api import (Events, Services, StateKeys, cost_factor, durable,
                       estimate_request_cost, now_iso, plugin)


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
        # 方案A：收尾预留额度——正常调用只能用 max-reserve，收尾调用（finalize=True）
        # 可用至 max，保证预算耗尽后仍能产出体面收尾（当晚事故的直接对策）。
        # reserve <= 0 → 行为与旧版完全一致；reserve 超过 max 一半时钳到 max//2
        # （预留若吞掉全部正常额度，正常调用将全部被拒，护栏反向失效）。
        reserve = max(0, int(cfg.get("finalize_reserve_tokens", 0)))
        if max_tokens:
            reserve = min(reserve, max_tokens // 2)
        # D1：预警阈值（占 max 比例；<=0 关闭）。预警必须在「正常额度拒绝线」之前触发
        # 才有意义，故阈值上限钳到 (max-reserve)/max。
        ratio = float(cfg.get("warning_ratio", 0.8))
        if max_tokens and reserve:
            ratio = min(ratio, (max_tokens - reserve) / max_tokens)
        # F1：口径自校准总开关
        calib = bool(cfg.get("cost_calibration", True))
        model = ((ctx.config or {}).get("agent") or {}).get("llm", {}).get("model", "fixture")
        ctx.state.setdefault(StateKeys.BUDGET_SPENT, 0)
        # F2/P3：账单列幂等迁移（run_id / cache_hit）；失败降级为不落新列，不阻断装配
        try:
            await ctx.db.execute(ctx, "ALTER TABLE agent_budget_log ADD COLUMN run_id TEXT",
                                 ())
        except Exception:                        # noqa: BLE001 —— 列已存在/表未建等 → 尽力而为
            pass
        try:
            await ctx.db.execute(
                ctx, "ALTER TABLE agent_budget_log ADD COLUMN cache_hit INTEGER", ())
        except Exception:                        # noqa: BLE001 —— 同上
            pass
        # G2.0（P0）：思考 token 与正文 token 分离落账（provider 未给明细 → NULL，不猜）
        try:
            await ctx.db.execute(
                ctx, "ALTER TABLE agent_budget_log ADD COLUMN reasoning_tokens INTEGER", ())
        except Exception:                        # noqa: BLE001 —— 同上
            pass

        async def _pre(payload: dict, next) -> dict:
            # est 覆盖 messages + tools 目录（native 形态）+ schema；
            # 经 core 估算器按 wire 同构计量，根治此前按「工具名列表」估算 26% 系统性低估
            # （详见 docs/governance/token口径对账-问题记录.md）。
            raw_est = estimate_request_cost(
                payload.get("messages", []),
                payload.get("tools") or None,
                payload.get("schema") or None,
            )
            # F1：估算 × 校准因子（真实 ≈ 估算 × factor）后再判定——判定时刻贴近真实计费
            est = int(raw_est * cost_factor(ctx)) if calib else int(raw_est)
            est = max(0, est)
            spent = ctx.state.get(StateKeys.BUDGET_SPENT, 0)
            is_finalize = bool(payload.get("finalize"))
            if max_tokens:
                limit = max_tokens if is_finalize else max_tokens - reserve
                if spent + est > limit:
                    return {"denied": True,
                            "reason": f"预算超限 spent={spent}+est={est}>{limit}"}
                # D1：跨预警阈值（只看正常额度线；收尾调用不触发）。只发一次：
                # 旗标已在位即跳过，避免每步重复事件刷屏。
                if (not is_finalize and ratio > 0
                        and spent + est >= ratio * max_tokens
                        and not ctx.state.get(StateKeys.BUDGET_WARNING)):
                    warn = {"at": now_iso(), "spent": spent + est,
                            "limit": max_tokens, "ratio": ratio}
                    ctx.state[StateKeys.BUDGET_WARNING] = warn
                    await durable(ctx, Events.BUDGET_WARNING, dict(warn))
                    await ctx.events.emit(Events.BUDGET_WARNING, dict(warn))
            # 预借记账（确定性：与 post 竞态无关）；pending **按 caller 分槽**（D3：
            # react 与 memory 并发调 LLM 时互不覆盖）。F1 存裸估算供 EWMA；F2 带走
            # react 的 run_id（尽力归组；并发多会话时以后写者为准）。
            caller = str(payload.get("caller") or "unknown")
            ctx.state[StateKeys.BUDGET_SPENT] = spent + est
            ctx.state.setdefault(StateKeys.BUDGET_PENDING_EST, {})[caller] = {
                "est": est, "raw": int(raw_est),
                "run_id": str(ctx.state.get(StateKeys.REACT_RUN_ID) or "")}
            return await next()

        async def _post(payload: dict) -> None:
            # S1 对账：用实际入账替换预借（spent = spent - est + actual），
            # 账面精确等于 Σ实际。F1：真实/裸估算比喂 EWMA（校准因子）；F3：偏差审计。
            ti = int(payload.get("tokens_in", 0))
            to = int(payload.get("tokens_out", 0))
            caller = str(payload.get("caller") or "unknown")
            pending = ctx.state.get(StateKeys.BUDGET_PENDING_EST) or {}
            entry = pending.pop(caller, None) or {}
            if isinstance(entry, int):           # 兼容旧形状（纯数字预借）
                entry = {"est": entry, "raw": entry, "run_id": ""}
            est = int(entry.get("est") or 0)
            raw = int(entry.get("raw") or 0)
            run_id = str(entry.get("run_id") or "")
            cache_hit = payload.get("cache_hit")
            cache_hit = int(cache_hit) if cache_hit is not None else None
            # G2.0（P0）：思考 token 明细（provider 未提供 → NULL；口径与 tokens_out 同源）
            _rt = payload.get("reasoning_tokens")
            reasoning_tokens = int(_rt) if _rt is not None else None
            spent = ctx.state.get(StateKeys.BUDGET_SPENT, 0)
            ctx.state[StateKeys.BUDGET_SPENT] = max(0, spent - est + (ti + to))
            await ctx.db.execute(
                ctx,
                "INSERT INTO agent_budget_log(model, tokens_in, tokens_out, est_cost,"
                " run_id, cache_hit, reasoning_tokens, at) VALUES (?,?,?,?,?,?,?,?)",
                (model, ti, to, est or None, run_id or None, cache_hit,
                 reasoning_tokens, now_iso()),
            )
            # F3：口径漂移审计（与校准开关无关——漂移本身永远值得留证）
            if raw > 0 and abs(ti - raw) / raw > 0.25:
                await durable(ctx, Events.LLM_COST_DRIFT, {
                    "caller": caller, "model": model, "raw_est": raw, "real_in": ti,
                    "drift": round((ti - raw) / raw, 3), "at": now_iso()})
            # F1：EWMA 维护（样本数达标后 cost_factor 才生效——见 api.cost_factor）
            if calib and raw > 0 and ti > 0:
                stats = dict(ctx.state.get(StateKeys.LLM_COST_RATIO) or {})
                m = dict(stats.get(model) or {})
                n = int(m.get("samples") or 0) + 1
                r_now = min(3.0, max(0.5, ti / raw))
                old = float(m.get("ratio") or 0)
                r = r_now if (n == 1 or old <= 0) else (0.8 * old + 0.2 * r_now)
                stats[model] = {"ratio": round(min(3.0, max(0.5, r)), 4), "samples": n}
                ctx.state[StateKeys.LLM_COST_RATIO] = stats

        def _call_failed(payload: dict) -> None:
            # S1：调用失败 → 全额退还本次预借（同步监听器，emit 内联执行立即生效）。
            caller = str(payload.get("caller") or "unknown")
            pending = ctx.state.get(StateKeys.BUDGET_PENDING_EST) or {}
            entry = pending.pop(caller, None) or {}
            if isinstance(entry, int):           # 兼容旧形状
                entry = {"est": entry}
            est = int(entry.get("est") or 0)
            if est:
                ctx.state[StateKeys.BUDGET_SPENT] = max(
                    0, ctx.state.get(StateKeys.BUDGET_SPENT, 0) - est)

        ctx.on(Events.LLM_PRE_CALL, _pre)
        ctx.on(Events.LLM_POST_CALL, _post)
        ctx.on(Events.LLM_CALL_FAILED, _call_failed)
        if cfg.get("reset_on_new_session", True):
            def _reset_session(_payload: dict) -> None:      # 同步：emit 内联执行，立即生效
                ctx.state[StateKeys.BUDGET_SPENT] = 0
                # D1：预警旗标随新会话重置（State 无 pop，置 None 即 falsy 关闭）
                ctx.state[StateKeys.BUDGET_WARNING] = None
            ctx.on(Events.CHAT_RESET, _reset_session)
        ctx.provide(Services.BUDGET, self)


__all__ = ["Budget"]