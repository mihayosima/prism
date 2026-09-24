"""agent.fixtures.llm —— 离线确定性 LLM 夹具（零触网）。

按 system prompt 里的标记路由到 canned 结构化响应，
保证：同一输入 → 同一输出（离线确定性）。
"""

from __future__ import annotations

from typing import Any


def _plan(user_text: str) -> dict:
    """chat 规划 canned：按关键词路由（离线确定性）。"""
    t = (user_text or "").lower()
    if any(k in t for k in ("ping", "健康", "在吗")):
        return {"intent": "ping"}
    if any(k in t for k in ("工具", "tool", "调用", "截图", "窗口", "屏幕")):
        return {"intent": "tool_call", "tool": "test.echo", "args": {}}
    return {"intent": "chat", "note": "未识别到可执行动作"}


def _route(messages: list[dict]) -> tuple[str, str]:
    """按角色取文：路由关键词只看用户消息，标记只看系统提示。"""
    sys_text = ""
    user_text = ""
    for msg in messages or []:
        content = str(msg.get("content", ""))
        if msg.get("role") == "system":
            sys_text += content
        else:
            user_text += content
    if "<!--chat:plan-->" in sys_text:
        return "plan", user_text
    if "<!--chat:answer-->" in sys_text:
        return "answer", user_text
    if "<!--memory:extract-->" in sys_text:
        return "extract", user_text
    return "default", ""


class FixtureLlm:
    """离线确定性 Provider。label='fixture'；interface_version=(1,0)。

    路由：chat:plan → 规划 canned；chat:answer → 解说 canned；memory:extract → 记忆抽取 canned。
    """

    interface_version = (1, 0)
    label = "fixture"

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None) -> dict:
        kind, route = _route(messages or [])
        if kind == "plan":
            return _plan(route)
        if kind == "answer":
            return {"text": "好的，已按意图执行（fixture 解说）。"}
        if kind == "extract":
            t = (route or "")[:40].strip()
            return {"facts": [f"fixture-mem：{t}"] if t else [],
                    "intentions": [], "entities": [], "relations": []}
        return {"text": "（fixture 默认响应）"}


__all__ = ["FixtureLlm"]