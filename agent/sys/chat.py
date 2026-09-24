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

from agent.api import AgentError, CapabilityNotImplemented, Events, Services, plugin
from agent.sys.audit import _now_iso, durable

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
                      {"message": message, "session_id": session_id, "at": _now_iso()})
        await self._append(ctx, session_id, "user", message)
        history = await self._history(ctx, session_id, limit=HISTORY_LIMIT)
        prior = history[:-1]              # 除本条外的最近历史 → 作为会话上下文喂给模型
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
                               "cancelled": True, "session_id": session_id, "at": _now_iso()})
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
            "ok": ok, "error": reply_error, "session_id": session_id, "at": _now_iso(),
        })
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
            res = await ctx.react.run(ctx, message, session_id=session_id, prior=prior)
        except asyncio.CancelledError:
            # 取消既非成功也非失败：如实落审计后**原样抛出**（不吞，保持取消语义）
            try:
                await durable(ctx, Events.REACT_EXHAUSTED,
                              {"reason": "已取消", "cancelled": True,
                               "session_id": session_id, "at": _now_iso()})
                await durable(ctx, Events.CHAT_DONE,
                              {"intent": "react", "ok": False, "error": "已取消",
                               "cancelled": True, "session_id": session_id, "at": _now_iso()})
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
            "session_id": session_id, "at": _now_iso(),
        })
        return ChatReply(text=text, plan={"intent": "react"}, evidence=res.as_evidence(),
                         tokens_in=res.tokens_in, tokens_out=res.tokens_out, ok=res.ok,
                         error=None if res.ok else res.reason, session_id=session_id)

    # ---------- 短期记忆：会话转写（session-scoped 历史） ----------

    async def _append(self, ctx, session_id: str, role: str, content: str) -> None:
        if ctx.get(Services.DB) is None:
            return
        await ctx.db.execute(
            ctx, "INSERT INTO agent_messages(session_id, role, content, at) VALUES (?,?,?,?)",
            (session_id, role, content, _now_iso()))

    async def _history(self, ctx, session_id: str, limit: int = HISTORY_LIMIT) -> list[dict]:
        if ctx.get(Services.DB) is None:
            return []
        rows = await ctx.db.query(
            ctx, "SELECT role, content FROM agent_messages WHERE session_id=?"
                 " ORDER BY seq DESC LIMIT ?", (session_id, limit))
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    async def history(self, ctx, session_id: str = "default", limit: int = 100) -> list[dict]:
        if ctx.get(Services.DB) is None:
            return []
        rows = await ctx.db.query(
            ctx, "SELECT seq, role, content, at FROM agent_messages WHERE session_id=?"
                 " ORDER BY seq ASC LIMIT ?", (session_id, limit))
        return [dict(r) for r in rows]

    async def reset(self, ctx, session_id: str = "default") -> int:
        if ctx.get(Services.DB) is None:
            return 0
        rows = await ctx.db.query(
            ctx, "SELECT COUNT(*) AS n FROM agent_messages WHERE session_id=?", (session_id,))
        await ctx.db.execute(ctx, "DELETE FROM agent_messages WHERE session_id=?", (session_id,))
        if ctx.has(Services.EVENTS):
            await ctx.events.emit(Events.CHAT_RESET, {"session_id": session_id})
        return rows[0]["n"]

    async def _memory_block(self, ctx, query: str) -> str:
        if not ctx.has(Services.MEMORY):
            return ""
        try:
            return await ctx.memory.context(ctx, query, limit=5)
        except Exception:  # noqa: BLE001 —— 召回失败不影响对话
            return ""

    # ---------- ① 规划 ----------

    async def _plan(self, ctx, message: str, prior: list[dict]) -> dict:
        system = f"<!--chat:plan-->\n{PLAN_SYSTEM}"
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
        system = f"<!--chat:answer-->\n{ANSWER_SYSTEM}\n\n证据 JSON:\n{snippet}"
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
    inject = [Services.LLM, Services.TASKS, Services.EVENTS, Services.DB]
    description = "对话驱动：意图规划 → 执行(guard) → LLM 解说"

    async def apply(self, ctx) -> None:
        ctx.provide(Services.CHAT, ChatDriver())


__all__ = ["Chat", "ChatDriver", "ChatReply"]