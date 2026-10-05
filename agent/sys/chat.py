"""agent.sys.chat —— 对话驱动（第二 driver，交互问答外设）。

机器保持权威，LLM 只做「规划 + 复述」，不越权自由裁量：
  send(message) → ①规划（LLM→JSON intent）→ ②执行（走 tasks/只读面，guard 矩阵照样裁决）
  → ③解说（LLM 按机器证据复述成友好 markdown）。

- 多步：若装配了 ``plugins.react``（``ctx.has("react")``）→ **委托**给 ReAct 循环
  （Think→Act+Observe→收敛），chat 自身契约不变；未装配即回退下方单步路径。
- 执行一律经 `tasks.invoke`（guard 照生效、owner/caller 审计）。
- durable 事件：`agent/chat/in`、`agent/chat/done`（审计脊柱）。
- LLM 任何一步被拒（预算/guard/解析失败）→ 诚实降级：不给假成功，解说用证据兜底。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from agent.api import (
    AgentError,
    CapabilityNotImplemented,
    Events,
    MARK_CHAT_ANSWER,
    MARK_CHAT_PLAN,
    ReactResultLike,
    Services,
    SessionIdentity,
    StateKeys,
    coerce_identity,
    durable,
    ensure_request_id,
    estimate_request_cost,
    estimate_tokens,
    now_iso,
    plugin,
    set_current_session_key,
)

PLAN_SYSTEM = (
    "你是通用 Agent 的任务规划器。根据用户输入只输出一个 JSON（不要解释，不要 markdown 围栏）：\n"
    "{\"intent\": \"tool_call|ping|chat\", \"tool\": \"\", \"args\": {}, \"note\": \"\"}\n"
    "intent 语义：\n"
    "- tool_call: 调用一个可用工具（tool=工具名，args=参数；只允许调用下方「可用工具」里列出的）\n"
    "- ping: 健康检查\n"
    "- chat: 无明确可执行动作时的闲聊/解释"
)

ANSWER_SYSTEM = (
    "你是通用 Agent 的解说员。下面给出本轮机器执行的『证据』（意图/动作/结构化结果）。"
    "请给用户一段简洁、友好、可读的 markdown 回复。若涉及技能产出，列出结果与关键依据；"
    "若涉及工具调用，把工具结果转述清楚；若被权限/预算拒绝，直接如实说明原因。只输出回复文本，不要输出 JSON。"
)

PLAN_SCHEMA = {
    "intent": {"type": "str"},
    "tool": {"type": "str"},
    "args": {"type": "object"},
    "note": {"type": "str"},
}

HISTORY_LIMIT = 12   # 短期记忆窗口：最近 N 条消息（≈6 轮）

# G4：治理器 token 配额 → memory.context max_chars 反推（详见 docs/governance/记忆配额反推方案-2026-09-28.md）
# 反推系数：core/tokens.py 的 CJK 启发式为 0.75 token/字 ⇒ 1 token ≈ 1.33 中文字符；
# 取 1.5 为宽松经验值（中文主导场景留余量）；严格不超 token 配额可改 1.33（=1/0.75）。
_MEMORY_TOKEN_TO_CHAR = 1.5
_MEMORY_MAX_CHARS_MIN = 600   # 保底下限，退化配置仍有可用记忆


def _env_facts(environ: dict | None = None) -> str:
    """运行环境事实块（纯函数，可注入 environ，便于离线快照测试）。

    机器权威：把真实用户名/主目录/桌面喂给规划器，并明令禁用 %VAR% 占位符，
    从源头杜绝 `C:\\Users\\%USERNAME%\\...` 这类不会被展开的路径。
    """
    environ = os.environ if environ is None else environ
    home = environ.get("USERPROFILE") or environ.get("HOME") or ""
    desktop = os.path.join(home, "Desktop") if home else ""
    return (
        "运行环境（构造路径时必须使用下列真实值，禁止输出 %VAR% 或 ~ 占位符）:\n"
        f"- 用户名: {environ.get('USERNAME') or environ.get('USER') or ''}\n"
        f"- 用户主目录: {home}\n"
        f"- 桌面: {desktop}\n"
        "- 文件类工具的相对路径以桌面为基准，能用相对路径就用相对路径（如 hello.py）"
    )


@dataclass(slots=True)
class _Assembled:
    """装配结果（消费层本地视图；governor 的 Assembly 亦按同属性面消费，不 import 其类型）。"""

    system_text: str
    prior: list[dict]
    report: Any = None          # governor 的 AssemblyReport（无治理器时为 None）


def _fallback_assemble(blocks: list[dict]) -> _Assembled:
    """无治理器（或治理器无 ``assemble`` 接缝）时的**降级装配**——复刻现状拼接语义。

    规则（与治理器预算充足时逐字节等价，供对拍）：
      - ``text`` 块：非空且 ``present``，按 ``priority`` 升序，以 ``\\n\\n`` 连接；
      - ``messages`` 块：非空且 ``present``，按 ``coverage.lo`` 升序拼接，输出剥离内部 ``seq``。
    """
    texts = [b for b in blocks
             if b.get("kind") == "text" and b.get("present")
             and str(b.get("payload") or "").strip()]
    texts.sort(key=lambda b: b.get("priority", 100))
    system_text = "\n\n".join(str(b["payload"]) for b in texts)

    msgs = [b for b in blocks
            if b.get("kind") == "messages" and b.get("present") and b.get("payload")]
    msgs.sort(key=lambda b: (b.get("coverage") or (0, 0))[0])
    prior: list[dict] = []
    for b in msgs:
        for m in b["payload"]:
            prior.append({k: v for k, v in m.items() if k != "seq"})
    return _Assembled(system_text=system_text, prior=prior)


# G2.0（P0）声明式注册：**每个装配端点的「应有块」清单**（配置期常量，声明幂等）。
# 完整性校验口径＝「连提交都没有」——``present=False`` 仍算已提交，故只有整块从装配
# 管线里消失才亮红灯（低误报）。prior 两块的缺席是**数据依赖**（空会话合法），故
# ``required=False``（只登记、不告警）。
_STAGE_DECLS: dict[str, list[dict]] = {
    "plan": [{"key": "plan_head", "kind": "text"},
             {"key": "memory", "kind": "text"},
             {"key": "tools", "kind": "text"},
             {"key": "env", "kind": "text"}],
    "prior": [{"key": "summary", "kind": "messages", "required": False},
              {"key": "history", "kind": "messages", "required": False}],
    "narrate": [{"key": "answer_head", "kind": "text"},
                {"key": "memory", "kind": "text"}],
}


def _declare_stage_blocks(gov, stage: str) -> None:
    """把本端点的应有块清单注入治理器（幂等；无该接缝 → 静默跳过，不劣化）。"""
    decls = _STAGE_DECLS.get(stage)
    fn = getattr(gov, "declare", None)
    if not decls or not callable(fn):
        return
    try:
        fn(decls, phase=stage)
    except Exception as exc:      # noqa: BLE001 —— 声明失败不阻断对话
        logging.getLogger("agent.chat").warning("context declare failed: %s", exc)


def is_resume_intent(ctx, message: str) -> bool:
    """P2：续接意图识别（决议暂定轻量关键词匹配；词表配置化，可后升级为 ask_user）。

    模块级纯函数（@plugin 包装类上取不到属性，测试/消费方一律 import 本名）。
    """
    cfg = ((getattr(ctx, "config", None) or {}).get("agent") or {}).get("react") or {}
    kws = cfg.get("resume_keywords") or []
    text = str(message or "").strip().lower()
    if not text or not kws:
        return False
    return any(str(k).lower() in text for k in kws)


# ---- 阶段 2（2.1/2.2）：崩溃自动续接探测辅助 ------------------------------------
# 身份（tenant/user/session）**一律由调用方传入**：本层不猜、不硬编码 tenant
# （2.1 曾固定 "default"，2.2 起由 run/chat.py 按请求解析后线程化至此）。


async def _set_snapshot_status(wm, ident: SessionIdentity, task_id: str,
                               status: str) -> None:
    """改写快照状态（读-改-写单 key；原子性由后端单 key 保证）。"""
    snap = await wm.snapshot_load(ident.tenant, task_id)
    if snap:
        snap["status"] = status
        await wm.snapshot_save(ident.tenant, task_id, snap)


@dataclass(slots=True)
class ChatReply:
    text: str                       # 解说 markdown
    plan: dict                      # 规划 JSON
    evidence: dict                  # 机器执行证据（权威）
    tokens_in: int
    tokens_out: int
    ok: bool                        # 执行是否成功
    error: str | None = None
    session_id: str = "default"     # 会话（短期记忆单元）
    notice: str | None = None       # 阶段 2（D9）：自动续接等可见提示（前端可选展示）


class ChatDriver:
    """ctx.chat —— 会话/问答驱动。"""

    def __init__(self) -> None:
        #: 在途后台任务（压缩/重建）——**必须持有引用并追踪**。
        #: 此前是裸 ``asyncio.create_task(...)``：不持有引用就可能被 GC 中途回收，
        #: teardown 也不会等它 ⇒ 压缩静默丢失（表现为"历史有时没压缩"）。
        #: 与 ``core/events.py`` 的 ``_tasks`` + ``aclose()`` 同一纪律。
        self._bg: set = set()

    def _spawn_bg(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)
        return task

    async def drain(self) -> None:
        """收尾：取消并等待在途后台任务（对称卸载，不留悬挂写库）。"""
        pending = [t for t in self._bg if not t.done()]
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except BaseException:                 # noqa: BLE001 —— 取消/任务自身异常均吞
                pass
        self._bg.clear()

    async def send(self, ctx, message: str, session_id: str = "default",
                   identity: SessionIdentity | None = None) -> ChatReply:
        """``identity``：2.2 会话/租户身份（适配层解析后显式线程化；缺省按 session_id 退化）。"""
        ident = identity if identity is not None else coerce_identity(session_id)
        # 当前会话提示：approval / guard 等**无 ctx 入参**的通道据此判定归属。
        # R1（2026-10-02）：**主通道是 ContextVar**（按 asyncio task 隔离 ⇒ 每请求独立）；
        # ``ctx.state`` 仍照写，只为兼容直接读 state 的既有测试与调用点。读取方一律用
        # ``current_session_key()``（ContextVar 优先、未绑定才回退 state）。
        set_current_session_key(ident.session_key)
        ctx.state[StateKeys.SESSION_KEY] = ident.session_key
        # #5 可观测性：HTTP 入口已由中间件绑定请求标识；此处兜底**无 HTTP 入口**的路径
        # （如 ``--once``、离线调用），使账本与日志里的 request_id 不为空（可串联）。
        ensure_request_id()
        if not message or not message.strip():
            return ChatReply(text="请输入一条消息。", plan={"intent": "chat"},
                             evidence={}, tokens_in=0, tokens_out=0, ok=True,
                             session_id=session_id)
        await durable(ctx, Events.CHAT_IN,
                      {"message": message, "session_id": session_id, "at": now_iso()})
        await self._append(ctx, session_id, "user", message)
        # 短期记忆（内容层）提供历史 + 上下文治理（调度层）收窗；二者任一缺失 → 旧 HISTORY_LIMIT 硬裁
        prior = await self._build_prior(ctx, session_id, message)
        # 多步 ReAct（plugins.react，目录插件）：装配了则**委托**；缺省回退下方单步路径。
        # 契约不变：chat 只经 ctx.has("react") 感知其存在，不 import 其实现。
        if ctx.has(Services.REACT):
            # 预算闸（S3）：prior 超输入预算 → 先压缩一次并重建（见 _ensure_react_headroom）
            prior = await self._ensure_react_headroom(ctx, session_id, message, prior)
            return await self._send_via_react(ctx, message, session_id, prior,
                                              identity=ident)
        try:
            plan = await self._plan(ctx, message, prior)
            evidence = await self._execute(ctx, plan)
            text, ti, to, error = await self._narrate(ctx, plan, evidence, message, prior)
        except asyncio.CancelledError:
            # 单步路径同样：取消如实落审计后原样抛出
            try:
                await durable(ctx, Events.CHAT_DONE,
                              {"intent": "chat", "ok": False, "error": "已取消",
                               "cancelled": True, "session_id": session_id, "at": now_iso()})
            except Exception:      # noqa: BLE001 —— 审计写失败不阻断取消
                pass
            raise
        await self._append(ctx, session_id, "assistant", text)
        ok = bool(evidence.get("ok")) and error is None
        # reply.error：解说失败 || 执行被拒（guard/预算）都要如实上浮，不吞
        reply_error = error if error is not None else (
            None if evidence.get("ok") else evidence.get("error"))
        # 记忆（长期）：本轮对话/决策喂给 memory（LLM 提取，guard/budget 照生效；失败不破坏主链路）
        if ctx.has(Services.MEMORY):
            try:
                await ctx.memory.remember(ctx, {
                    "user": message, "reply": text, "plan": plan, "evidence": evidence,
                })
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("agent.chat").warning("memory remember failed: %s", exc)
        await durable(ctx, Events.CHAT_DONE, {
            "intent": plan.get("intent"),
            "ok": ok, "error": reply_error, "session_id": session_id, "at": now_iso(),
        })
        if ctx.has(Services.SHORT_TERM):
            self._spawn_bg(self._safe_compress(ctx, session_id))
        return ChatReply(text=text, plan=plan, evidence=evidence,
                         tokens_in=ti, tokens_out=to, ok=ok, error=reply_error,
                         session_id=session_id)

    # ---------- 多步循环（plugins.react 委托路径） ----------

    async def _probe_resume(self, ctx, message: str, is_continue: bool,
                            ident: SessionIdentity) -> tuple[dict | None, str | None]:
        """阶段 2（2.1/D6/D8/D9/D15 + 2.2/H1）：崩溃自动续接探测 + 意图分流（plan 花钱之前）。

        2.2：扫描前缀精确到**会话级**（``run_snapshot:{tenant}:{user}:{session}:``）——
        否则会命中别的会话的未完成 run（H1：在会话 C 发消息却续接会话 B 的任务）。

        返回 ``(resume_state, notice)``：
        - 命中未完成快照（status=="running"）且本条为「继续」意图 → 续接**最新 1 个**（其余标
          orphaned，D8），返回其快照（优先于 react_chain，D15）+ 可见提示；
        - 命中但本条为「新任务」意图 → **不**自动续接（旧 run 全标 orphaned），处理新消息（D9，
          避免"我要查天气却在写代码"）；
        - 无未完成快照 → ``(None, None)``（走原有 react_chain「继续」/全新 run 逻辑）。
        """
        if not ctx.has(Services.WORK_MEMORY):
            return None, None
        wm = ctx.get(Services.WORK_MEMORY)
        try:
            pending = [s for s in await wm.snapshot_list(ident.tenant,
                                                         ident.snapshot_prefix())
                       if s.get("status") == "running"]
        except Exception:                                    # noqa: BLE001 —— 探测失败不阻断主链路
            return None, None
        if not pending:
            return None, None
        if is_continue:
            latest = pending[0]                              # snapshot_list 已按 ts 倒序
            state = await wm.snapshot_load(ident.tenant, latest["task_id"])
            # 被续接者标 ``resumed``（**关键：否则旧快照仍 running，下次探测会重复续接**）；
            # 其余未完成标 orphaned（D8）。
            await _set_snapshot_status(wm, ident, latest["task_id"], "resumed")
            for s in pending[1:]:
                await _set_snapshot_status(wm, ident, s["task_id"], "orphaned")
            if state:
                logging.getLogger("agent.chat").info(
                    "chat: auto-resume unfinished run %s", latest["task_id"])
                return state, "检测到上一次任务未完成，正在继续……"
            return None, None
        # 新任务意图：不吞掉新语义，旧 run 全部 orphaned（D9）
        for s in pending:
            await _set_snapshot_status(wm, ident, s["task_id"], "orphaned")
        return None, None

    async def _send_via_react(self, ctx, message: str, session_id: str,
                              prior: list[dict],
                              identity: SessionIdentity | None = None) -> ChatReply:
        """把本轮交给 ReAct 循环（多步 Think→Act+Observe），再按同一纪律收尾。

        与单步路径保持同一外部纪律：写短期记忆（user/assistant）、写 durable
        ``agent/chat/done``、喂 ``ctx.memory``（若启用）、返回 ``ChatReply``。

        阶段 2（2.1/D6）：进入 react 循环（plan 花钱）**之前**先探测未完成 run 快照并按意图分流。
        """
        ident = identity if identity is not None else coerce_identity(session_id)
        is_continue = is_resume_intent(ctx, message)
        resume_state, notice = await self._probe_resume(ctx, message, is_continue, ident)
        try:
            res: ReactResultLike = await ctx.react.run(
                ctx, message, session_id=session_id, prior=prior,
                resume=(is_continue or resume_state is not None),
                resume_state=resume_state, identity=ident)
        except asyncio.CancelledError:
            # 取消既非成功也非失败：如实落审计后**原样抛出**（不吞，保持取消语义）
            try:
                await durable(ctx, Events.REACT_EXHAUSTED,
                              {"reason": "已取消", "cancelled": True,
                               "session_id": session_id, "at": now_iso()})
                await durable(ctx, Events.CHAT_DONE,
                              {"intent": "react", "ok": False, "error": "已取消",
                               "cancelled": True, "session_id": session_id, "at": now_iso()})
            except Exception:      # noqa: BLE001 —— 审计写失败不阻断取消
                pass
            raise
        text = res.text or "（无回复）"
        await self._append(ctx, session_id, "assistant", text)
        if ctx.has(Services.MEMORY):
            try:
                await ctx.memory.remember(ctx, {
                    "user": message, "reply": text,
                    # C（方案）：进度快照——收尾摘要前置，确保 LLM 提取（4000 字符截断）
                    # 时进度信息不被大体积 evidence 挤掉，「继续」可低成本续接。
                    "progress": str(getattr(res, "summary", "") or "")[:1500],
                    "plan": {"intent": "react"}, "evidence": res.as_evidence(),
                })
            except Exception as exc:  # noqa: BLE001
                logging.getLogger("agent.chat").warning("memory remember failed: %s", exc)
        await durable(ctx, Events.CHAT_DONE, {
            "intent": "react", "ok": res.ok,
            "error": None if res.ok else res.reason,
            "session_id": session_id, "at": now_iso(),
        })
        if ctx.has(Services.SHORT_TERM):
            self._spawn_bg(self._safe_compress(ctx, session_id))
        return ChatReply(text=text, plan={"intent": "react"}, evidence=res.as_evidence(),
                         tokens_in=res.tokens_in, tokens_out=res.tokens_out, ok=res.ok,
                         error=None if res.ok else res.reason, session_id=session_id,
                         notice=notice)

    # ---------- 短期记忆：会话转写（session-scoped 历史） ----------
    #
    # 数据层归属：``agent_messages`` / ``agent_session_summaries`` 两表**归短期记忆内容层**
    # （plugins/short_term）。chat 作为消费方**零 SQL**——append / history / reset 一律
    # 经 ``ctx.get(Services.SHORT_TERM)``；插件未装配（目录插件 opt-in）→ 无会话转写，
    # 对话照常进行，绝不裸崩。这样"内容层 owner"才名副其实，也不会出现两套实现漂移。

    async def _append(self, ctx, session_id: str, role: str, content: str) -> None:
        short = ctx.get(Services.SHORT_TERM)
        if short is None:
            return                                  # 未装配：无转写，不落库也不报错
        await short.append(ctx, session_id, role, content)

    async def _history(self, ctx, session_id: str, limit: int = HISTORY_LIMIT) -> list[dict]:
        short = ctx.get(Services.SHORT_TERM)
        if short is None:
            return []
        rows = await short.recent(ctx, session_id, limit)
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    async def history(self, ctx, session_id: str = "default", limit: int = 100) -> list[dict]:
        short = ctx.get(Services.SHORT_TERM)
        if short is None:
            return []
        return await short.history(ctx, session_id, limit)

    # ---------- 短期记忆：prior 构造（内容层提供 + 调度层收窗） ----------

    async def _build_prior(self, ctx, session_id: str, message: str) -> list[dict]:
        """构造喂给模型的会话上下文（经治理器 messages 块装配 + 统一互斥）。

        职责边界（关键）：
        - **短期记忆**提供内容（全量历史 + 滚动摘要前情）；
        - 历史**不再做收窗**（``pick_history`` 收窗机制已移除）：未摘要历史随对话增长，
          由短期记忆的滚动摘要（``maybe_compress`` + ``_threshold()``）按 token 阈值压缩晋升，
          压缩时机完全由 ``compact_trigger_tokens`` 决定；
        - **统一互斥（左开右闭）**：summary 覆盖 ``(0, covers]``、history 覆盖 ``(covers, N]``，
          边界由 ``covers_up_to_seq`` 单一来源派生；两块 ``coverage`` 作**入参**传给治理器
          （governor 不反向查询），重叠由治理器统一剔除，绝不双份注入；
        - **循环保底（S3）**：``plugins.react`` 已装配时，经 ``reserve_tokens`` 预留
          ``react_min_chain_tokens``——防 prior 吃满预算致循环内无空间（循环空转）；
        - 单条超大工具/文件结果仍由 ``short_term.truncate_view`` 做可逆截短（安全网，非收窗）。

        降级：短期记忆缺失 → 退回旧 ``HISTORY_LIMIT`` 条数硬裁（不劣化、不裸崩）；
        治理器缺失 → ``_fallback_assemble`` 降级拼接（与治理器等价）。
        """
        if ctx.has(Services.SHORT_TERM):
            short = ctx.get(Services.SHORT_TERM)
            raw = await short.get_history(ctx, session_id)
            raw_excl = raw[:-1] if raw else []
            meta = await short.summary_meta(ctx, session_id)
            covers = int(meta["covers_up_to_seq"]) if meta else 0
            post = [m for m in raw_excl if m["seq"] > covers]
            post_view = short.truncate_view(post)
            summary = await short.latest_summary(ctx, session_id)
            blocks: list[dict] = []
            if summary:
                blocks.append({
                    "key": "summary", "kind": "messages", "present": True,
                    "payload": [{"role": "user",
                                 "content": "[会话前情 · 以下为历史摘要，仅供参考]\n" + summary,
                                 "seq": covers}],
                    "weight": 0.10, "coverage": (0, covers), "layer": "memory",
                })
            if post_view:
                hi = max(int(m["seq"]) for m in post_view)
                blocks.append({
                    "key": "history", "kind": "messages", "present": True,
                    "payload": [dict(m) for m in post_view],
                    "weight": 0.60, "coverage": (covers, hi), "layer": "memory",
                })
            return (await self._assemble(ctx, blocks, stage="prior",
                                         reserve_tokens=self._react_reserve(ctx))).prior
        # 降级：旧 HISTORY_LIMIT 条数硬裁（除本轮 user 外）
        history = await self._history(ctx, session_id, limit=HISTORY_LIMIT)
        return history[:-1]

    def _react_reserve(self, ctx) -> int:
        """会话级装配时为多步循环预留的 chain 保底额度（S3）。

        仅当 ``plugins.react`` 已装配时预留——否则无循环可保底。
        治理器缺失/无该接缝 → 0（不劣化）。
        """
        if not ctx.has(Services.REACT):
            return 0
        gov = ctx.get(Services.CONTEXT) if ctx.has(Services.CONTEXT) else None
        mn = getattr(gov, "react_min_chain_tokens", None)
        if not callable(mn):
            return 0
        try:
            return max(0, int(mn()))
        except Exception:      # noqa: BLE001 —— 预算查询异常不阻断对话
            return 0

    async def _ensure_react_headroom(self, ctx, session_id: str, message: str,
                                     prior: list[dict]) -> list[dict]:
        """多步循环前的**预算闸**：prior 超输入预算 → 一次性压缩 + 重建 prior（不重试）。

        职责归属（S3 钉死）：
        - **决策**在调度层（本方法）——不由 react 发起，也不由治理器发起；
        - **裁决**仍在内容层 ``plugins.short_term``——只调用既有 ``maybe_compress``，
          **不自定阈值**（压缩时机由 ``compact_trigger_tokens`` 单独决定）；
        - 压缩后仍超 → **不拒绝进入**（否则多步能力整体失效），交治理器刚性降级规则
          诚实标注兜底；只尝试一次，防抖、防死循环。
        - 无治理器 → 跳过该闸（退回现状，不劣化）。
        """
        gov = ctx.get(Services.CONTEXT) if ctx.has(Services.CONTEXT) else None
        budget = getattr(gov, "input_budget", None)
        if not callable(budget):
            return prior
        try:
            if estimate_tokens(prior) <= int(budget()):
                return prior
        except Exception:      # noqa: BLE001 —— 预算查询异常不阻断对话
            return prior
        logging.getLogger("agent.chat").info(
            "prior 超输入预算，触发一次会话级压缩并重建（session=%s）", session_id)
        await self._safe_compress(ctx, session_id)
        try:
            rebuilt = await self._build_prior(ctx, session_id, message)
        except Exception as exc:      # noqa: BLE001 —— 重建失败退回原 prior，不裸崩
            logging.getLogger("agent.chat").warning("prior rebuild failed: %s", exc)
            return prior
        return rebuilt or prior

    async def _safe_compress(self, ctx, session_id: str) -> None:
        """异步触发压缩（双保险：maybe_compress 内部已吞执行异常并按退避处理）。"""
        try:
            short = ctx.get(Services.SHORT_TERM)
            if short is not None:
                await short.maybe_compress(ctx, session_id)
        except Exception as exc:  # noqa: BLE001 —— 压缩失败绝不拖累主回复
            logging.getLogger("agent.chat").warning("short_term compress task failed: %s", exc)

    async def reset(self, ctx, session_id: str = "default") -> int:
        short = ctx.get(Services.SHORT_TERM)
        # 无短期记忆插件 → 无转写可清（n=0），但**事件照发**：新会话语义与记忆层是否装配
        # 无关，预算累计等订阅方依赖 ``CHAT_RESET`` 清零。
        n = await short.reset(ctx, session_id) if short is not None else 0
        if ctx.has(Services.EVENTS):
            await ctx.events.emit(Events.CHAT_RESET, {"session_id": session_id})
        return n

    async def _assemble(self, ctx, blocks: list[dict], *, stage: str,
                        reserve_tokens: int = 0) -> _Assembled:
        """经治理器分块装配（软依赖）；无治理器/无 ``assemble`` 接缝 → 降级拼接（不劣化）。

        ``blocks`` 以 **dict** 传入——消费层不 import 插件契约类型（解耦纪律）。
        ``reserve_tokens`` 为**后续块**预留额度（S3：会话级装配时为多步循环的 chain 留空间）；
        注入实现不认该参数时自动退回原签名（不劣化）。
        装配后发 ``agent/context/governed`` 遥测事件（各块实占 / 裁剪 / 刚性超限 / 回吐）。
        """
        gov = ctx.get(Services.CONTEXT) if ctx.has(Services.CONTEXT) else None
        asm = None
        reserve = max(0, int(reserve_tokens or 0))
        if gov is not None:
            _declare_stage_blocks(gov, stage)      # G2.0（P0）：声明应有块（幂等）
            fn = getattr(gov, "assemble", None)
            if callable(fn):
                from agent.api import cost_factor
                tries = ({"reserve_tokens": reserve, "cost_factor": cost_factor(ctx),
                          "phase": stage},
                         {"reserve_tokens": reserve, "cost_factor": cost_factor(ctx)},
                         {"reserve_tokens": reserve}, {})
                for kwargs in tries:
                    try:
                        asm = fn(blocks, **kwargs)
                        break
                    except TypeError:
                        continue        # 注入实现不认该参数 → 退回更旧签名
                    except Exception as exc:  # noqa: BLE001 —— 装配失败退降级，绝不裸崩
                        logging.getLogger("agent.chat").warning("context assemble failed: %s", exc)
                        break
        out = (_Assembled(str(asm.system_text), list(asm.prior), getattr(asm, "report", None))
               if asm is not None else _fallback_assemble(blocks))
        await self._emit_governed(ctx, stage, out)
        return out

    async def _emit_governed(self, ctx, stage: str, out: _Assembled) -> None:
        """发装配遥测事件（失败绝不阻断对话）。"""
        if not ctx.has(Services.EVENTS):
            return
        rep = out.report
        payload: dict = {"stage": stage, "at": now_iso(),
                         "system_tokens": estimate_tokens(out.system_text)}
        if rep is not None:
            payload["input_budget"] = int(getattr(rep, "input_budget", 0))
            payload["budget_raw"] = int(getattr(rep, "budget_raw", 0))   # P3：I-4 对照基数
            payload["total_used"] = int(getattr(rep, "total_used", 0))
            payload["redistributed"] = int(getattr(rep, "redistributed", 0))
            payload["blocks"] = [
                {"key": r.key, "present": r.present, "reason": r.reason,
                 "requested": r.requested, "allocated": r.allocated, "used": r.used,
                 "layer": getattr(r, "layer", ""), "cap": getattr(r, "cap", 0)}
                for r in getattr(rep, "blocks", [])
            ]
            payload["rigid_overflow"] = [r.key for r in getattr(rep, "rigid_overflow", [])]
            # ---- G2.0（P0）只读观测：记账 + 缓存友好度（旧治理器无这些字段 → 0/空）----
            payload["unaccounted_tokens"] = int(getattr(rep, "unaccounted_tokens", 0))
            payload["stable_prefix_tokens"] = int(getattr(rep, "stable_prefix_tokens", 0))
            payload["volatile_blocks"] = list(getattr(rep, "volatile_blocks", []) or [])
            payload["missing_blocks"] = list(getattr(rep, "missing_blocks", []) or [])
            payload["attachments"] = [
                {"key": a.key, "claimed": int(a.claimed), "used": int(a.used)}
                for a in getattr(rep, "attachments", []) or []
            ]
        try:
            await ctx.events.emit(Events.CONTEXT_GOVERNED, payload)
        except Exception as exc:              # noqa: BLE001 —— 遥测失败不阻断
            logging.getLogger("agent.chat").warning("context governed emit failed: %s", exc)
        # ---- G2.0（P1-3）：同点发**水位体检**（assess 纯函数；治理器缺失即跳过）----
        gov = ctx.get(Services.CONTEXT) if ctx.has(Services.CONTEXT) else None
        assess = getattr(gov, "assess", None)
        if callable(assess) and rep is not None:
            try:
                wl = assess(int(getattr(rep, "total_used", 0)), phase=stage)
                await ctx.events.emit(Events.CONTEXT_WATERLINE, {
                    "stage": stage, "phase": stage, "at": now_iso(),
                    "used": wl.used, "window": wl.working_window,
                    "capability": wl.capability_window, "ratio": wl.load_ratio,
                    "level": wl.level, "suggested": wl.suggested, "gates": wl.gates})
            except Exception as exc:          # noqa: BLE001 —— 遥测失败不阻断
                logging.getLogger("agent.chat").warning(
                    "context waterline emit failed: %s", exc)
        # ---- G2.0（P3-2）：装配后**不变量断言**（I-3 前缀可测 / I-4 预算守恒）----
        # 记录 + 告警（**不阻断**）：违规即发 ``context/invariant-violation`` 并记日志。
        # 记账面判定经治理器门面软调用（消费方不 import 插件内部）。
        inv_fn = getattr(gov, "check_invariants", None)
        if callable(inv_fn) and rep is not None:
            try:
                bad = [k for k, ok in inv_fn(payload).items() if not ok]
            except Exception as exc:          # noqa: BLE001 —— 断言失败不阻断
                logging.getLogger("agent.chat").warning(
                    "context invariant check failed: %s", exc)
                bad = []
            if bad:
                logging.getLogger("agent.chat").warning(
                    "context invariant violation: stage=%s %s", stage, bad)
                try:
                    await ctx.events.emit(Events.CONTEXT_INVARIANT_VIOLATION, {
                        "stage": stage, "scope": None, "invariants": bad,
                        "detail": {"unaccounted_tokens": payload.get("unaccounted_tokens"),
                                   "budget_raw": payload.get("budget_raw"),
                                   "stable_prefix_tokens": payload.get("stable_prefix_tokens"),
                                   "volatile_blocks": payload.get("volatile_blocks")}})
                except Exception as exc:      # noqa: BLE001 —— 告警失败不阻断
                    logging.getLogger("agent.chat").warning(
                        "context invariant emit failed: %s", exc)

    async def _memory_block(self, ctx, query: str) -> str:
        if not ctx.has(Services.MEMORY):
            return ""
        try:
            # G4：用治理器反推的记忆配额（token）挂钩窗口预算（默认 1800 token）
            max_chars = 1200  # 退化默认，与改动前一致
            gov = ctx.get(Services.CONTEXT) if ctx.has(Services.CONTEXT) else None
            if gov is not None:
                max_chars = max(
                    _MEMORY_MAX_CHARS_MIN,
                    int(gov.memory_budget() * _MEMORY_TOKEN_TO_CHAR),
                )
            return await ctx.memory.context(ctx, query, limit=5, max_chars=max_chars)
        except Exception:  # noqa: BLE001 —— 召回失败不影响对话
            return ""

    # ---------- ① 规划 ----------

    async def _plan(self, ctx, message: str, prior: list[dict]) -> dict:
        sm = ctx.get(Services.SYSTEM_MEMORY) or ""
        mem = await self._memory_block(ctx, message)
        tools = self._tools_block(ctx)
        # 头部（标记 + 系统记忆 + 规划器提示）作为**单个刚性块**，保证与降级拼接逐字节一致。
        head = MARK_CHAT_PLAN + ("\n" + sm if sm else "") + "\n" + PLAN_SYSTEM
        # G2.0（P1-4）：层标注（决定缺省 cap：resident / on_demand / input 有 cap）。
        blocks = [
            {"key": "plan_head", "kind": "text", "present": True, "payload": head,
             "rigid": True, "priority": 0, "layer": "resident"},
            {"key": "memory", "kind": "text", "present": bool(mem), "payload": mem,
             "weight": 0.0, "priority": 10, "layer": "memory"},
            {"key": "tools", "kind": "text", "present": bool(tools), "payload": tools,
             "weight": 0.0, "priority": 20, "layer": "on_demand"},
            {"key": "env", "kind": "text", "present": True, "payload": _env_facts(),
             "rigid": True, "priority": 90, "layer": "resident"},
        ]
        # 彻底版·统一账本：plan 调用带 schema（response_format），属请求附加项、不在 messages
        # 预算内，装配时经 reserve_tokens 先扣其占用，使窗口总占用对齐真实 prompt_tokens。
        reserve = estimate_request_cost(messages=[], tools=None, schema=PLAN_SCHEMA)
        system = (await self._assemble(ctx, blocks, stage="plan",
                                       reserve_tokens=reserve)).system_text
        messages = ([{"role": "system", "content": system}] + list(prior)
                    + [{"role": "user", "content": message}])
        try:
            data = (await ctx.llm.complete(ctx, messages, schema=PLAN_SCHEMA,
                                           caller="agent.chat")).data
            intent = str(data.get("intent") or "chat")
            if intent not in ("tool_call", "ping", "chat"):
                intent = "chat"
            plan = {"intent": intent}
            if data.get("tool"):
                plan["tool"] = str(data["tool"])
            if isinstance(data.get("args"), dict):
                plan["args"] = data["args"]
            note = data.get("note")
            if note:
                plan["note"] = str(note)
            return plan
        except CapabilityNotImplemented as exc:
            return {"intent": "chat", "denied": str(exc)}
        except Exception as exc:  # noqa: BLE001 —— 规划失败 → 当闲聊，不假动作
            return {"intent": "chat", "parse_error": str(exc)}

    def _tools_block(self, ctx) -> str:
        """动态工具目录 → 塞进规划提示词（工具集合可运行时增删）。"""
        try:
            cat = ctx.tasks.catalog()
        except Exception:  # noqa: BLE001
            return ""
        if not cat:
            return ""
        lines = ["可用工具（tool_call 时只可调用下列之一）:"]
        for t in cat:
            req = ",".join(t.get("required") or []) or "-"
            desc = (t.get("description") or "")[:60]          # 截断描述，控制提示词体积
            params = ",".join((t.get("params") or {}).keys()) or "-"
            lines.append(f"- {t['name']}：{desc}；参数 {params}；必填 {req}")
        return "\n".join(lines)

    # ---------- ② 执行（机器权威，guard 照生效） ----------

    async def _execute(self, ctx, plan: dict) -> dict:
        intent = plan.get("intent", "chat")
        if plan.get("denied"):
            return {"intent": "chat", "ok": False, "error": plan.get("denied")}
        try:
            if intent == "ping":
                pong = await ctx.tasks.invoke(ctx, "agent.ping", caller="agent.chat")
                return {"intent": "ping", "ok": True, "ping": pong}
            if intent == "tool_call":
                tool = plan.get("tool")
                if not tool:
                    return {"intent": "tool_call", "ok": False, "error": "未指定工具名"}
                args = dict(plan.get("args") or {})
                result = await ctx.tasks.invoke(ctx, tool, caller="agent.chat", **args)
                return {"intent": "tool_call", "tool": tool, "ok": True, "result": result}
            return {"intent": "chat", "ok": True, "note": plan.get("note")}
        except CapabilityNotImplemented as exc:
            return {"intent": intent, "ok": False, "error": str(exc)}
        except AgentError as exc:
            return {"intent": intent, "ok": False, "error": str(exc)}

    # ---------- ③ 解说 ----------

    async def _narrate(self, ctx, plan: dict, evidence: dict, message: str, prior: list[dict]):
        snippet = json.dumps({"plan": plan, "evidence": evidence},
                             ensure_ascii=False)
        sm = ctx.get(Services.SYSTEM_MEMORY) or ""
        mem = await self._memory_block(ctx, message)
        head = (MARK_CHAT_ANSWER + ("\n" + sm if sm else "") + "\n" + ANSWER_SYSTEM
                + "\n\n证据 JSON:\n" + snippet)
        blocks = [
            {"key": "answer_head", "kind": "text", "present": True, "payload": head,
             "rigid": True, "priority": 0, "layer": "resident"},
            {"key": "memory", "kind": "text", "present": bool(mem), "payload": mem,
             "weight": 0.0, "priority": 10, "layer": "memory"},
        ]
        system = (await self._assemble(ctx, blocks, stage="narrate")).system_text
        messages = ([{"role": "system", "content": system}] + list(prior)
                    + [{"role": "user", "content": message}])
        try:
            lr = await ctx.llm.complete(ctx, messages, caller="agent.chat")
            text = str(lr.data.get("text") or "").strip()
            if not text:
                text = "（已执行，但解说未生成）"
            return text, lr.tokens_in, lr.tokens_out, None
        except CapabilityNotImplemented as exc:
            return self._fallback_text(plan, evidence), 0, 0, str(exc)
        except Exception as exc:  # noqa: BLE001
            return self._fallback_text(plan, evidence), 0, 0, str(exc)

    def _fallback_text(self, plan: dict, evidence: dict) -> str:
        if not evidence.get("ok"):
            return f"本轮未能执行：{evidence.get('error')}"
        return json.dumps(evidence, ensure_ascii=False)[:2000]


@plugin
class Chat:
    """第二 driver：对话/问答驱动（交互外设）。"""

    name = "agent.chat"
    version = "0.1.0"
    layer = "business"
    provides = [Services.CHAT]
    # 记忆四层均为**软依赖**（目录插件 opt-in，可能未启用）→ 不写进 inject，
    # 否则未启用时容器装配直接 MissingDependencyError。消费处一律 ctx.get(...) or 默认。
    inject = [Services.LLM, Services.TASKS, Services.EVENTS, Services.DB]
    description = "对话驱动：意图规划 → 执行(guard) → LLM 解说"

    async def apply(self, ctx) -> None:
        driver = ChatDriver()
        ctx.provide(Services.CHAT, driver)
        # 对称收尾：取消/等待在途后台任务（否则压缩写库会在 teardown 后悬挂或被丢弃）
        ctx.effect(driver.drain)


__all__ = ["Chat", "ChatDriver", "ChatReply", "is_resume_intent"]