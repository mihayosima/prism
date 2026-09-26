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
    durable,
    now_iso,
    plugin,
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
class ChatReply:
    text: str                       # 解说 markdown
    plan: dict                      # 规划 JSON
    evidence: dict                  # 机器执行证据（权威）
    tokens_in: int
    tokens_out: int
    ok: bool                        # 执行是否成功
    error: str | None = None
    session_id: str = "default"     # 会话（短期记忆单元）


class ChatDriver:
    """ctx.chat —— 会话/问答驱动。"""

    async def send(self, ctx, message: str, session_id: str = "default") -> ChatReply:
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
            return await self._send_via_react(ctx, message, session_id, prior)
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
            asyncio.create_task(self._safe_compress(ctx, session_id))
        return ChatReply(text=text, plan=plan, evidence=evidence,
                         tokens_in=ti, tokens_out=to, ok=ok, error=reply_error,
                         session_id=session_id)

    # ---------- 多步循环（plugins.react 委托路径） ----------

    async def _send_via_react(self, ctx, message: str, session_id: str,
                              prior: list[dict]) -> ChatReply:
        """把本轮交给 ReAct 循环（多步 Think→Act+Observe），再按同一纪律收尾。

        与单步路径保持同一外部纪律：写短期记忆（user/assistant）、写 durable
        ``agent/chat/done``、喂 ``ctx.memory``（若启用）、返回 ``ChatReply``。
        """
        try:
            res: ReactResultLike = await ctx.react.run(ctx, message, session_id=session_id,
                                                       prior=prior)
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
            asyncio.create_task(self._safe_compress(ctx, session_id))
        return ChatReply(text=text, plan={"intent": "react"}, evidence=res.as_evidence(),
                         tokens_in=res.tokens_in, tokens_out=res.tokens_out, ok=res.ok,
                         error=None if res.ok else res.reason, session_id=session_id)

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
        """构造喂给模型的会话上下文。

        职责边界（关键）：
        - **短期记忆**提供内容（全量历史 + 滚动摘要前情）；
        - 历史**不再做收窗**（``pick_history`` 收窗机制已移除）：未摘要历史随对话增长，
          由短期记忆的滚动摘要（``maybe_compress`` + ``_threshold()``）按 token 阈值压缩晋升，
          压缩时机完全由 ``compact_trigger_tokens`` 决定；
        - 单条超大工具/文件结果仍由 ``short_term.truncate_view`` 做可逆截短（安全网，非收窗）。

        降级：短期记忆缺失 → 退回旧 ``HISTORY_LIMIT`` 条数硬裁（不劣化、不裸崩）。
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
            prior: list[dict] = []
            if summary:
                prior.append({
                    "role": "user",
                    "content": "[会话前情 · 以下为历史摘要，仅供参考]\n" + summary,
                })
            prior.extend(post_view)
            return prior
        # 降级：旧 HISTORY_LIMIT 条数硬裁（除本轮 user 外）
        history = await self._history(ctx, session_id, limit=HISTORY_LIMIT)
        return history[:-1]

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

    async def _memory_block(self, ctx, query: str) -> str:
        if not ctx.has(Services.MEMORY):
            return ""
        try:
            return await ctx.memory.context(ctx, query, limit=5)
        except Exception:  # noqa: BLE001 —— 召回失败不影响对话
            return ""

    # ---------- ① 规划 ----------

    async def _plan(self, ctx, message: str, prior: list[dict]) -> dict:
        sm = ctx.get(Services.SYSTEM_MEMORY) or ""
        system = MARK_CHAT_PLAN
        if sm:                                       # 系统记忆：静态底座（声明层，前置注入）
            system += "\n" + sm
        system += "\n" + PLAN_SYSTEM
        mem = await self._memory_block(ctx, message)
        if mem:
            system += "\n\n" + mem
        tools = self._tools_block(ctx)
        if tools:
            system += "\n\n" + tools
        system += "\n\n" + _env_facts()
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
        system = MARK_CHAT_ANSWER
        if sm:                                       # 系统记忆：静态底座（声明层，前置注入）
            system += "\n" + sm
        system += "\n" + ANSWER_SYSTEM + "\n\n证据 JSON:\n" + snippet
        mem = await self._memory_block(ctx, message)
        if mem:
            system += "\n\n" + mem
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
        ctx.provide(Services.CHAT, ChatDriver())


__all__ = ["Chat", "ChatDriver", "ChatReply"]