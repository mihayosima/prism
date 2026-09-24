"""agent.fixtures.llm —— 离线确定性 LLM 夹具（零触网）。

按 system prompt 里的标记路由到 canned 结构化响应，
保证：同一输入 → 同一输出（离线确定性）。
标记：<!--chat:plan--> · <!--chat:answer--> · <!--memory:extract--> ·
      <!--react:turn-->（脚本化 tool_calls）· <!--react:final-->（触顶收尾）。
"""

from __future__ import annotations

from typing import Any

from agent.api import Events


def _plan(user_text: str) -> dict:
    """chat 规划 canned：按关键词路由（离线确定性）。"""
    t = (user_text or "").lower()
    if any(k in t for k in ("ping", "健康", "在吗")):
        return {"intent": "ping"}
    if any(k in t for k in ("工具", "tool", "调用", "截图", "窗口", "屏幕")):
        return {"intent": "tool_call", "tool": "test.echo", "args": {}}
    return {"intent": "chat", "note": "未识别到可执行动作"}


def _route(messages: list[dict]) -> tuple[str, str]:
    """按角色取文：路由关键词只看用户消息，标记只看系统提示。

    只拼接 **str** 内容：ReAct 的观察回灌消息 content 是 dict（tool_results），
    不能参与关键词/标记匹配。
    """
    sys_text = ""
    user_text = ""
    for msg in messages or []:
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        if msg.get("role") == "system":
            sys_text += content
        else:
            user_text += content
    if "<!--react:final-->" in sys_text:
        return "react_final", user_text
    if "<!--react:turn-->" in sys_text:
        return "react", user_text
    if "<!--chat:plan-->" in sys_text:
        return "plan", user_text
    if "<!--chat:answer-->" in sys_text:
        return "answer", user_text
    if "<!--memory:extract-->" in sys_text:
        return "extract", user_text
    return "default", ""


# ---- ReAct 脚本化路由（离线确定性：同一输入 → 同一 tool_calls 序列） ----


def _react_state(messages: list[dict]) -> tuple[int, str]:
    """(已完成轮数, 原始请求)。

    轮数 = 已追加的带 tool_calls 的 assistant 消息数；请求 = **最后一个 str 用户消息**
    （ReAct 的观察回灌是 dict content，不参与）。
    """
    rounds = 0
    prompt = ""
    for msg in messages or []:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            rounds += 1
        elif role == "user" and isinstance(msg.get("content"), str):
            prompt = str(msg["content"])
    return rounds, prompt


def _react_reply(messages: list[dict]) -> dict:
    """按请求关键词 + 当前轮数给出脚本化回复（含 tool_calls 或最终答复）。"""
    rounds, prompt = _react_state(messages)
    t = prompt.lower()
    if "两步" in prompt or "two-step" in t:
        if rounds == 0:
            return {"text": "", "tool_calls": [
                {"id": "call_1", "name": "test.echo", "arguments": {"text": "one"}}]}
        return {"text": "两步链路完成（fixture）。"}
    if "自纠" in prompt:
        if rounds == 0:                      # 参数类型错 → 观察里出现 error
            return {"text": "", "tool_calls": [
                {"id": "call_1", "name": "test.echo", "arguments": {"text": 123}}]}
        if rounds == 1:                      # 改正参数 → 成功
            return {"text": "", "tool_calls": [
                {"id": "call_2", "name": "test.echo", "arguments": {"text": "ok"}}]}
        return {"text": "已纠正参数并完成（fixture）。"}
    if "大结果" in prompt:
        if rounds == 0:
            return {"text": "", "tool_calls": [
                {"id": "call_1", "name": "test.big", "arguments": {}}]}
        return {"text": "已拿到大结果并完成（fixture）。"}
    if "触顶" in prompt:                     # 永不收敛 → 触发 max_steps
        return {"text": "", "tool_calls": [
            {"id": f"call_{rounds}", "name": "test.echo", "arguments": {"text": "loop"}}]}
    return {"text": f"直接回答（fixture）：{prompt[:40]}"}


class FixtureLlm:
    """离线确定性 Provider。label='fixture'；interface_version=(1,0)。

    路由：chat:plan → 规划 canned；chat:answer → 解说 canned；memory:extract → 记忆抽取 canned；
    react:turn → ReAct 脚本化（tool_calls/final）；react:final → 触顶收尾 canned。
    ``tools`` 在本 provider 下被忽略（脚本化，不解析真实工具目录）。
    """

    interface_version = (1, 0)
    label = "fixture"

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None,
                       tools: list[dict] | None = None) -> dict:
        kind, route = _route(messages or [])
        if kind == "react":
            result = _react_reply(messages or [])
        elif kind == "react_final":
            result = {"text": "已达执行上限，基于已有信息作答（fixture）。"}
        elif kind == "plan":
            result = _plan(route)
        elif kind == "answer":
            result = {"text": "好的，已按意图执行（fixture 解说）。"}
        elif kind == "extract":
            t = (route or "")[:40].strip()
            result = {"facts": [f"fixture-mem：{t}"] if t else [],
                      "intentions": [], "entities": [], "relations": []}
        else:
            result = {"text": "（fixture 默认响应）"}

        # B5.2 离线切帧：stream 开启且非结构化调用 → 把散文切成 4 片依次 emit
        stream_on = bool((((ctx.config or {}).get("agent") or {}).get("llm") or {}).get("stream", True))
        text = result.get("text")
        if stream_on and schema is None and isinstance(text, str) and text:
            step = max(1, len(text) // 4)
            pieces = [text[i:i + step] for i in range(0, len(text), step)]
            for p in pieces:
                await ctx.events.emit(Events.LLM_TOKEN, {"text": p})
        return result


__all__ = ["FixtureLlm"]