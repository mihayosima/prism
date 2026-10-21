"""agent.sys.budget —— 成本护栏。

- ``agent/llm/pre-call``：已花 + 本次估算 > 上限 → 否决。
 方案 A：``finalize_reserve_tokens`` 为收尾调用预留末段额度——
 正常调用上限是 ``max-reserve``，带 ``finalize=True`` 的收尾调用仍可用至 ``max``。
- D1：``warning_ratio``——花费预估跨 ``ratio*max`` 阈值时置
 ``StateKeys.BUDGET_WARNING`` 旗标 + durable/live ``agent/budget/warning`` 事件。
 消费方按需读状态键注入「尽快收束」提示——状态键前向契约，双方零直连。
- F1：``cost_calibration``——post-call 用「真实 tokens_in ÷ 当次裸估算」
 维护按模型分桶的 EWMA 校准因子，pre-call 估算乘因子
 后再判定/预借；治理器装配经 ``api.cost_factor`` 读同一因子。
 样本 <5 或因子越界 [0.5,3] 时诚实退裸估算/夹紧。
 校准因子保持全局：口径是模型属性，拆到会话会让每个会话样本
 数不足而退化成裸估算，精度反而变差。
- F2/F3/P3：``run_id`` 与
 ``cache_hit`` 随账落 ``agent_budget_log``
 ；估算↔真实偏差 >25% 发 durable ``agent/llm/cost-drift``。
- ``agent/llm/post-call``：真实 token 入账，落 agent_budget_log。

G2：
- 额度与
 状态一律
 按会话分桶 ⇒ 会话 A 的消耗不再顶掉 B 的额度；
- ``CHAT_RESET`` 只清本会话；
- 账单 ``agent_budget_log`` 增 ``session`` 列，使「本会话花了多少」可回答；
- 无会话覆盖时全部回落到容器值 ⇒ 行为与改造前逐值等价。

状态形状，本次变更，注意兼容：
 ``BUDGET_SPENT`` : ``dict[session_key, int]``
 ``BUDGET_WARNING`` : ``dict[session_key, warn]``
 ``BUDGET_PENDING_EST`` : ``dict[session_key, dict[caller, entry]]``
"""

from __future__ import annotations

import logging

from agent.api import (Events, Services, SESSION_CFG_VIEW, StateKeys, cost_factor,
                       current_run_id, current_session_key, durable,
                       estimate_request_cost, inc_counter, now_iso, plugin)
# 账单表的补列定义归口 agent/tables.py，此处只负责执行。
# 沿用 sys → agent.tables 的既有导入方向。
from agent.tables import BUDGET_LOG_FIELDS

logger = logging.getLogger("agent.budget")

# 状态分桶上限：只防内存无限增长，
# 淘汰最久未用的桶；淘汰只丢「累计值」，不影响任何已落账的账单。
_STATE_SESSION_LIMIT = 512


# ---- 会话级辅助 --------------------------------

def _session_key(ctx) -> str:
    """当前会话键。

 R1：走 ``current_session_key()``——ContextVar 优先，未绑定才回退 ``ctx.state``。此前只读容器级 state：并发请求
 交错时后写覆盖先写，会把 A 的账记进 B 的桶。
 """
    return current_session_key(ctx)


def _run_id(ctx) -> str:
    """当前 run 的账单归组键。

 与 ``_session_key`` 同款通道与理由：此前读容器级 state，
 并发下会把别人的 run 写进本行的 ``agent_budget_log.run_id``。独立成函数便于
 单测直击真实读取点。
 """
    return current_run_id(ctx)


def _pending_key(payload: dict) -> str:
    """预借槽的唯一关联键：优先 ``call_id``，缺省回退 ``caller``。

 为什么从 ``caller`` 改为 ``call_id``：``LLM_POST_CALL`` 是 fire-and-forget，
 预借槽按 caller 关联时，同 caller 的连续/并发调用会互相覆盖——先到的 post
 弹出后到的预借，使真实 token 记错账、校准样本丢失。``call_id``
 由 ``sys/llm.py::complete`` 每次调用生成并贯穿 pre/post/failed 三 payload，关联无歧义。
 """
    return str(payload.get("call_id") or payload.get("caller") or "unknown")


def _cfg_view(ctx):
    """取会话配置视图接缝。"""
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
    """``dict[sk, int]``。"""
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
    """``dict[sk, warn]``。

 旧形状本身也是 dict，不能只按类型判断——
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
    """``dict[sk, dict[caller, entry]]``。"""
    m = ctx.state.get(StateKeys.BUDGET_PENDING_EST)
    if not isinstance(m, dict):
        m = {}
        ctx.state[StateKeys.BUDGET_PENDING_EST] = m
    # 旧形状识别：值不是 dict ⇒ 视为旧的单层 caller 槽，整体重置
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
    """ctx.budget —— 预算护栏。guard 用 prepend 先裁决；budget 后裁决。"""

    name = "agent.budget"
    layer = "system"
    provides = [Services.BUDGET]
    inject = [Services.EVENTS, Services.DB, Services.LLM]

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("budget") or {}
        max_tokens = int(cfg.get("max_tokens_per_run", 10000))
        # 方案 A：收尾预留额度
        raw_reserve = int(cfg.get("finalize_reserve_tokens", 0))
        raw_ratio = float(cfg.get("warning_ratio", 0.8))
        # 口径自校准总开关
        calib = bool(cfg.get("cost_calibration", True))
        model = ((ctx.config or {}).get("agent") or {}).get("llm", {}).get("model", "fixture")
        base_raw = (max_tokens, raw_reserve, raw_ratio)
        _spent_map(ctx)                      # 初始化
        # F2/P3：账单明细列幂等补列——定义见 agent/tables.py::BUDGET_LOG_FIELDS
        # 表结构真相源归口于该文件；此处不写死列名，避免"看表结构要翻两处"。
        # 容错是刻意的：存量库这些列早已存在，ALTER 必然报 duplicate column。
        # 真失败必须可见：原实现全静默，
        # 补不上时表现为"账单明细列莫名其妙是空的"，日志零线索、只能手工 PRAGMA 才发现。
        # ``duplicate column`` 属正常幂等路径，不计失败，否则每次启动都误报。
        for col, decl in BUDGET_LOG_FIELDS:
            try:
                await ctx.db.execute(
                    ctx, f"ALTER TABLE agent_budget_log ADD COLUMN {col} {decl}", ())
            except Exception as exc:         # noqa: BLE001 —— 列已存在/表未建等 → 尽力而为
                if "duplicate column" not in str(exc).lower():
                    inc_counter("agent_budget_column_backfill_failed_total")
                    logger.warning("agent_budget_log 补列 %s 失败 ⇒ 该列将缺失: %s", col, exc)

        async def _pre(payload: dict, next) -> dict:
            # est 覆盖 messages + tools 目录+ schema；
            # 经 core 估算器按 wire 同构计量，根治此前按「工具名列表」估算 26% 系统性低估
            raw_est = estimate_request_cost(
                payload.get("messages", []),
                payload.get("tools") or None,
                payload.get("schema") or None,
            )
            sk = _session_key(ctx)
            max_tokens, reserve, ratio = _limits(ctx, base_raw)
            # 估算 × 校准因子后再判定——判定时刻贴近真实计费
            est = int(raw_est * cost_factor(ctx)) if calib else int(raw_est)
            est = max(0, est)
            spent = _spent_of(ctx, sk)
            is_finalize = bool(payload.get("finalize"))
            if max_tokens:
                limit = max_tokens if is_finalize else max_tokens - reserve
                if spent + est > limit:
                    return {"denied": True,
                            "reason": f"预算超限 spent={spent}+est={est}>{limit}"}
                # 跨预警阈值。只发一次：
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
            # 预借记账；pending 槽键 = 唯一 call_id
            _set_spent(ctx, sk, spent + est)
            _pending_map(ctx, sk)[_pending_key(payload)] = {
                "est": est, "raw": int(raw_est),
                # R1：归组键同走请求作用域通道
                "run_id": _run_id(ctx)}
            _trim_pending(ctx)
            return await next()

        async def _post(payload: dict) -> None:
            # S1 对账：用实际入账替换预借，
            # 账面精确等于 Σ实际。F1：真实/裸估算比喂 EWMA；F3：偏差审计。
            sk = _session_key(ctx)
            ti = int(payload.get("tokens_in", 0))
            to = int(payload.get("tokens_out", 0))
            caller = str(payload.get("caller") or "unknown")
            pending = _pending_map(ctx, sk)
            entry = pending.pop(_pending_key(payload), None) or {}
            if isinstance(entry, int):       # 兼容旧形状
                entry = {"est": entry, "raw": entry, "run_id": ""}
            est = int(entry.get("est") or 0)
            raw = int(entry.get("raw") or 0)
            run_id = str(entry.get("run_id") or "")
            cache_hit = payload.get("cache_hit")
            cache_hit = int(cache_hit) if cache_hit is not None else None
            # 思考 token 明细
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
            # 口径漂移审计
            if raw > 0 and abs(ti - raw) / raw > 0.25:
                await durable(ctx, Events.LLM_COST_DRIFT, {
                    "caller": caller, "model": model, "raw_est": raw, "real_in": ti,
                    "drift": round((ti - raw) / raw, 3), "at": now_iso()})
            # EWMA 维护
            # 全局单份：不按会话拆分。
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
            # 调用失败 → 全额退还本次预借。
            sk = _session_key(ctx)
            pending = _pending_map(ctx, sk)
            entry = pending.pop(_pending_key(payload), None) or {}
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
                # 只清本会话。事件已带 session_id；
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
