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
  ★ 校准因子**保持全局**（不随会话拆分）：口径是模型属性，拆到会话会让每个会话样本
  数不足而退化成裸估算，精度反而变差（方案 §3.2）。
- F2/F3/P3：``run_id``（react 经 ``StateKeys.REACT_RUN_ID`` 提供，尽力归组）与
  ``cache_hit``（DeepSeek 前缀缓存命中，P3 计量）随账落 ``agent_budget_log``
  （缺列幂等迁移）；估算↔真实偏差 >25% 发 durable ``agent/llm/cost-drift``（F3）。
- ``agent/llm/post-call``（emit）：真实 token 入账，落 agent_budget_log。

G2（会话级，本次改造）：
- 额度（``max_tokens_per_run`` / ``finalize_reserve_tokens`` / ``warning_ratio``）与
  **状态**（``BUDGET_SPENT`` / ``BUDGET_WARNING`` / ``BUDGET_PENDING_EST``）一律
  **按会话分桶** ⇒ 会话 A 的消耗不再顶掉 B 的额度；
- ``CHAT_RESET`` 只清**本会话**（此前是清整个容器——多会话下 A 清历史会让 B 的
  预算凭空回满，属 P0 缺陷）；
- 账单 ``agent_budget_log`` 增 ``session`` 列（幂等迁移），使「本会话花了多少」可回答；
- 无会话覆盖时全部回落到容器值 ⇒ **行为与改造前逐值等价**。

状态形状（本次变更，注意兼容）：
  ``BUDGET_SPENT``        : ``dict[session_key, int]``（旧为 int；旧形状自动迁移）
  ``BUDGET_WARNING``      : ``dict[session_key, warn]``（消费方须按会话取，见 react/loop）
  ``BUDGET_PENDING_EST``  : ``dict[session_key, dict[caller, entry]]``（旧为 dict[caller, entry]）
"""

from __future__ import annotations

from agent.api import (Events, Services, SESSION_CFG_VIEW, StateKeys, cost_factor,
                       durable, estimate_request_cost, now_iso, plugin)

# 状态分桶上限（与配置覆盖的 sessions 上限**独立**）：只防内存无限增长，
# 淘汰最久未用的桶；淘汰只丢「累计值」，不影响任何已落账的账单。
_STATE_SESSION_LIMIT = 512


# ---- 会话级辅助（纯函数/小工具，无业务语义） --------------------------------

def _session_key(ctx) -> str:
    """当前会话键（2.2 的「尽力而为」通道；空串 = 容器级/未知）。"""
    try:
        return str((getattr(ctx, "state", None) or {}).get(StateKeys.SESSION_KEY) or "")
    except Exception:                        # noqa: BLE001
        return ""


def _cfg_view(ctx):
    """取会话配置视图接缝（软取；缺失即 None ⇒ 回落容器配置）。"""
    try:
        return ctx.get(SESSION_CFG_VIEW)
    except Exception:                        # noqa: BLE001
        return None


def _clamp_limits(max_tokens: int, reserve_raw: int, ratio_raw: float
                  ) -> tuple[int, int, float]:
    """钳制：预留不得吞掉全部正常额度；预警阈值必须落在正常拒绝线之前。"""
    reserve = max(0, int(reserve_raw or 0))
    if max_tokens:
        reserve = min(reserve, max_tokens // 2)
    ratio = float(ratio_raw or 0)
    if max_tokens and reserve:
        ratio = min(ratio, (max_tokens - reserve) / max_tokens)
    return max_tokens, reserve, ratio


def _limits(ctx, base_raw: tuple[int, int, float]) -> tuple[int, int, float]:
    """本会话的 (max, reserve, ratio)：会话覆盖优先，缺省回落容器值。"""
    sk = _session_key(ctx)
    view = _cfg_view(ctx)
    if not (sk and view):
        return _clamp_limits(*base_raw)
    try:
        cfg = view("budget", sk) or {}
    except Exception:                        # noqa: BLE001 —— 视图异常不拖垮主链路
        return _clamp_limits(*base_raw)
    if not cfg:
        return _clamp_limits(*base_raw)
    b_max, b_res, b_ratio = base_raw
    return _clamp_limits(int(cfg.get("max_tokens_per_run", b_max)),
                         int(cfg.get("finalize_reserve_tokens", b_res)),
                         float(cfg.get("warning_ratio", b_ratio)))


def _spent_map(ctx) -> dict:
    """``dict[sk, int]``（旧 int 形状自动迁移为单桶）。"""
    m = ctx.state.get(StateKeys.BUDGET_SPENT)
    if not isinstance(m, dict):
        m = {}
        ctx.state[StateKeys.BUDGET_SPENT] = m
    return m


def _spent_of(ctx, sk: str) -> int:
    return int(_spent_map(ctx).get(sk, 0) or 0)


def _set_spent(ctx, sk: str, value: int) -> None:
    m = _spent_map(ctx)
    m[sk] = max(0, int(value))
    if len(m) > _STATE_SESSION_LIMIT:
        m.pop(next(iter(m)), None)           # dict 保序：淘汰最久未用


def _warn_map(ctx) -> dict:
    """``dict[sk, warn]``（旧单值记录自动迁移到容器桶 ``""``）。

    旧形状本身也是 dict（``{at, spent, limit, ratio}``），不能只按类型判断——
    以「是否含记录字段 ``limit``」区分「记录」与「分桶」。
    """
    m = ctx.state.get(StateKeys.BUDGET_WARNING)
    if isinstance(m, dict) and "limit" in m:
        m = {"": m}                          # 旧单值记录 → 容器桶
        ctx.state[StateKeys.BUDGET_WARNING] = m
    elif not isinstance(m, dict):
        m = {} if m is None else {"": m}
        ctx.state[StateKeys.BUDGET_WARNING] = m
    return m


def _pending_map(ctx, sk: str) -> dict:
    """``dict[sk, dict[caller, entry]]``（旧 ``dict[caller, entry]`` 自动迁移）。"""
    m = ctx.state.get(StateKeys.BUDGET_PENDING_EST)
    if not isinstance(m, dict):
        m = {}
        ctx.state[StateKeys.BUDGET_PENDING_EST] = m
    # 旧形状识别：值不是 dict ⇒ 视为旧的单层 caller 槽，整体重置（进程内，最多丢一次预借）
    if any(not isinstance(v, dict) for v in m.values()):
        m.clear()
    slot = m.get(sk)
    if not isinstance(slot, dict):
        slot = {}
        m[sk] = slot
    return slot


def _trim_pending(ctx) -> None:
    m = ctx.state.get(StateKeys.BUDGET_PENDING_EST)
    if isinstance(m, dict) and len(m) > _STATE_SESSION_LIMIT:
        m.pop(next(iter(m)), None)


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
        # 方案A：收尾预留额度（语义与钳制见 _clamp_limits）
        raw_reserve = int(cfg.get("finalize_reserve_tokens", 0))
        raw_ratio = float(cfg.get("warning_ratio", 0.8))
        # F1：口径自校准总开关（G1 容器级：校准是模型属性，不随会话拆）
        calib = bool(cfg.get("cost_calibration", True))
        model = ((ctx.config or {}).get("agent") or {}).get("llm", {}).get("model", "fixture")
        base_raw = (max_tokens, raw_reserve, raw_ratio)
        _spent_map(ctx)                      # 初始化（并把旧 int 形状迁移为分桶）
        # F2/P3：账单列幂等迁移（run_id / cache_hit / session）
        for col, decl in (("run_id", "TEXT"), ("cache_hit", "INTEGER"),
                          ("reasoning_tokens", "INTEGER"), ("session", "TEXT")):
            try:
                await ctx.db.execute(
                    ctx, f"ALTER TABLE agent_budget_log ADD COLUMN {col} {decl}", ())
            except Exception:                # noqa: BLE001 —— 列已存在/表未建等 → 尽力而为
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
            sk = _session_key(ctx)
            max_tokens, reserve, ratio = _limits(ctx, base_raw)
            # F1：估算 × 校准因子（真实 ≈ 估算 × factor）后再判定——判定时刻贴近真实计费
            est = int(raw_est * cost_factor(ctx)) if calib else int(raw_est)
            est = max(0, est)
            spent = _spent_of(ctx, sk)
            is_finalize = bool(payload.get("finalize"))
            if max_tokens:
                limit = max_tokens if is_finalize else max_tokens - reserve
                if spent + est > limit:
                    return {"denied": True,
                            "reason": f"预算超限 spent={spent}+est={est}>{limit}"}
                # D1：跨预警阈值（只看正常额度线；收尾调用不触发）。只发一次：
                # 旗标已在位即跳过，避免每步重复事件刷屏。
                warn_map = _warn_map(ctx)
                if (not is_finalize and ratio > 0
                        and spent + est >= ratio * max_tokens
                        and not warn_map.get(sk)):
                    warn = {"at": now_iso(), "spent": spent + est,
                            "limit": max_tokens, "ratio": ratio, "session": sk}
                    warn_map[sk] = warn
                    await durable(ctx, Events.BUDGET_WARNING, dict(warn))
                    await ctx.events.emit(Events.BUDGET_WARNING, dict(warn))
            # 预借记账（确定性：与 post 竞态无关）；pending **按会话 + caller 分槽**
            # （D3：react 与 memory 并发调 LLM 时互不覆盖；G2：会话间亦互不覆盖）。
            caller = str(payload.get("caller") or "unknown")
            _set_spent(ctx, sk, spent + est)
            _pending_map(ctx, sk)[caller] = {
                "est": est, "raw": int(raw_est),
                "run_id": str(ctx.state.get(StateKeys.REACT_RUN_ID) or "")}
            _trim_pending(ctx)
            return await next()

        async def _post(payload: dict) -> None:
            # S1 对账：用实际入账替换预借（spent = spent - est + actual），
            # 账面精确等于 Σ实际。F1：真实/裸估算比喂 EWMA（校准因子）；F3：偏差审计。
            sk = _session_key(ctx)
            ti = int(payload.get("tokens_in", 0))
            to = int(payload.get("tokens_out", 0))
            caller = str(payload.get("caller") or "unknown")
            pending = _pending_map(ctx, sk)
            entry = pending.pop(caller, None) or {}
            if isinstance(entry, int):       # 兼容旧形状（纯数字预借）
                entry = {"est": entry, "raw": entry, "run_id": ""}
            est = int(entry.get("est") or 0)
            raw = int(entry.get("raw") or 0)
            run_id = str(entry.get("run_id") or "")
            cache_hit = payload.get("cache_hit")
            cache_hit = int(cache_hit) if cache_hit is not None else None
            # G2.0（P0）：思考 token 明细（provider 未提供 → NULL；口径与 tokens_out 同源）
            _rt = payload.get("reasoning_tokens")
            reasoning_tokens = int(_rt) if _rt is not None else None
            spent = _spent_of(ctx, sk)
            _set_spent(ctx, sk, max(0, spent - est + (ti + to)))
            await ctx.db.execute(
                ctx,
                "INSERT INTO agent_budget_log(model, tokens_in, tokens_out, est_cost,"
                " run_id, cache_hit, reasoning_tokens, session, at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (model, ti, to, est or None, run_id or None, cache_hit,
                 reasoning_tokens, sk or None, now_iso()),
            )
            # F3：口径漂移审计（与校准开关无关——漂移本身永远值得留证）
            if raw > 0 and abs(ti - raw) / raw > 0.25:
                await durable(ctx, Events.LLM_COST_DRIFT, {
                    "caller": caller, "model": model, "raw_est": raw, "real_in": ti,
                    "drift": round((ti - raw) / raw, 3), "at": now_iso()})
            # F1：EWMA 维护（样本数达标后 cost_factor 才生效——见 api.cost_factor）
            # ★ 全局单份：不按会话拆分（拆了会让每会话样本不足而退裸估算）。
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
            sk = _session_key(ctx)
            caller = str(payload.get("caller") or "unknown")
            pending = _pending_map(ctx, sk)
            entry = pending.pop(caller, None) or {}
            if isinstance(entry, int):       # 兼容旧形状
                entry = {"est": entry}
            est = int(entry.get("est") or 0)
            if est:
                _set_spent(ctx, sk, max(0, _spent_of(ctx, sk) - est))

        ctx.on(Events.LLM_PRE_CALL, _pre)
        ctx.on(Events.LLM_POST_CALL, _post)
        ctx.on(Events.LLM_CALL_FAILED, _call_failed)
        if cfg.get("reset_on_new_session", True):
            def _reset_session(payload: dict) -> None:      # 同步：emit 内联执行，立即生效
                # G2（P0 修复）：**只清本会话**。事件已带 session_id（sys/chat.py 发出）；
                # 不带则退回「清全部」以兼容既有调用方，并显式留痕——多会话下误清全局
                # 会让别人的预算凭空回满，是本次要消灭的缺陷。
                sid = str((payload or {}).get("session_id") or "")
                if sid:
                    m = _spent_map(ctx)
                    m.pop(sid, None)
                    w = _warn_map(ctx)
                    w.pop(sid, None)
                    p = ctx.state.get(StateKeys.BUDGET_PENDING_EST)
                    if isinstance(p, dict):
                        p.pop(sid, None)
                    return
                print("[budget] 警告：CHAT_RESET 未带 session_id —— 回退为清空全部会话累计",
                      file=__import__("sys").stderr)
                ctx.state[StateKeys.BUDGET_SPENT] = {}
                ctx.state[StateKeys.BUDGET_WARNING] = {}
                ctx.state[StateKeys.BUDGET_PENDING_EST] = {}
            ctx.on(Events.CHAT_RESET, _reset_session)
        ctx.provide(Services.BUDGET, self)


__all__ = ["Budget"]
