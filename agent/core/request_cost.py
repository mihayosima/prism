"""agent.core.request_cost —— 请求级 token 成本估算（与模型服务商真实 prompt_tokens 同源）。

彻底版（统一账本）：与 ``agent.sys.llm`` 共用同一套 wire 序列化函数，从源头消除
"三把尺子"里第二把（budget 放行判据）与第三把（服务商真实账单）之间的算法漂移。

纯度（继承 core 纪律）：零依赖、零业务词；不得 import sys/plugins 树（K0 守卫）。

对外暴露：
  - ``wire_messages`` / ``wire_tools``：内部消息/工具目录 → OpenAI 兼容 wire 形态
    （与 ``agent.sys.llm`` 原先的 ``_wire_messages`` / ``_wire_tools`` 逐字节同构；
    下沉为公共原语后，sys/llm 改调本模块，避免两处各自演化）；
  - ``estimate_request_cost``：请求级 token 估算，按 wire 形态序列化后统一走
    ``estimate_tokens``，故 est ≈ 真实账单，根治预算护栏此前 26% 的系统性低估
    （详见 docs/governance/token口径对账-问题记录.md）。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .tokens import estimate_tokens

_JSON_TYPES = {"str": "string", "int": "integer", "float": "number",
               "bool": "boolean", "array": "array", "object": "object"}


def _wire_prop(spec: dict) -> dict:
    """内核参数 spec → JSON Schema 片段（S6/P2：保真还原 MCP 侧的类型信息）。

    优先发 ``anyOf``：内核为便于校验会把联合类型归一化成单一 ``type``，
    若两者同时发会互相矛盾（``{"type":"object","anyOf":[…]}``）⇒ 只发 ``anyOf``。
    ``anyOf`` 分支里存的本就是**JSON 原生类型名**（``build_task_schema`` 直取原文），
    故**原样透传**，不可再过一次 ``_JSON_TYPES``（那是内部名→JSON 名的映射，
    用在这里会把 ``boolean`` 误判成未知而退化成 ``object``）。
    ``enum`` / ``default`` / ``description`` 有则原样带上 —— 这正是模型此前看不到
    ``mode = read|write|copy|move|delete|list|search|info`` 的原因。
    """
    anyof = spec.get("anyOf")
    if isinstance(anyof, list) and anyof:
        out: dict[str, Any] = {"anyOf": [{"type": str(x)} for x in anyof if isinstance(x, str)]
                              or [{"type": "object"}]}
    else:
        out = {"type": _JSON_TYPES.get(str(spec.get("type", "object")), "object")}
    if isinstance(spec.get("enum"), list) and spec["enum"]:
        out["enum"] = list(spec["enum"])
    if "default" in spec:
        out["default"] = spec["default"]
    if spec.get("description"):
        out["description"] = str(spec["description"])
    return out


# OpenAI / DeepSeek 工具名硬约束：仅允许 ^[a-zA-Z0-9_-]+$。
# 内部工具名带命名空间（如 mcp.windows.xxx、agent.ping），发往模型前必须净化，
# 模型回传的净化名再映射回内部原名以驱动 ctx.tasks.invoke。
_TOOL_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _safe_tool_name(name: str) -> str:
    """内部工具名 → 合规 wire 名（非法字符替换成 '_'）。"""
    safe = _TOOL_NAME_UNSAFE.sub("_", name or "")
    return safe or "tool"


def wire_messages(messages: list[dict]) -> list[dict]:
    """内部消息形状 → OpenAI 兼容 wire 形状（无工具时等价恒等）。

    内部：``{"role": "assistant", "content": str, "tool_calls": [{id,name,arguments}]}``
    与 ``{"role": "user", "content": {"tool_results": [...]}}``（观察回灌）。
    wire：assistant.tool_calls[].function.arguments 为 JSON 字符串；观察展开为多条 role=tool。
    B-fix：assistant 条目若携带 ``reasoning_content``（thinking 模式思考链）原样透传——
    DeepSeek 要求输出过思考的 assistant 消息回传时必须携带，剥离即 400。
    """
    out: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
        reasoning_kv = ({"reasoning_content": m["reasoning_content"]}
                        if role == "assistant" and m.get("reasoning_content") else {})
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for i, c in enumerate(m.get("tool_calls") or []):
                calls.append({
                    "id": str(c.get("id") or f"call_{i}"),
                    "type": "function",
                    "function": {"name": str(c.get("name") or ""),
                                 "arguments": json.dumps(c.get("arguments") or {},
                                                         ensure_ascii=False)},
                })
            out.append({"role": "assistant",
                        "content": content if isinstance(content, str) and content else None,
                        "tool_calls": calls, **reasoning_kv})
            continue
        if isinstance(content, dict) and content.get("tool_results") is not None:
            for r in content["tool_results"]:
                out.append({"role": "tool",
                            "tool_call_id": str(r.get("tool_call_id") or ""),
                            "content": str(r.get("content") or "")})
            continue
        if isinstance(content, str):
            out.append({"role": role, "content": content, **reasoning_kv})
        else:
            out.append({"role": role, "content": json.dumps(content, ensure_ascii=False)})
    return out


def wire_tools(tools: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """工具目录（``TaskRegistry.catalog()`` 形状）→ OpenAI 兼容 tools 数组。

    返回 ``(wired_tools, {safe_name: original_name})``：映射表供回程把模型
    回传的净化名还原成内部原名，避免命名空间信息在 wire 上丢失。
    重名冲突（不同原名净化后相同）自动追加 ``_2`` / ``_3`` 后缀消歧。
    """
    out: list[dict] = []
    name_map: dict[str, str] = {}
    seen: dict[str, str] = {}
    for t in tools or []:
        orig = str(t.get("name") or "")
        base = _safe_tool_name(orig)
        safe = base
        i = 2
        while safe in seen and seen[safe] != orig:
            safe = f"{base}_{i}"
            i += 1
        seen[safe] = orig
        name_map[safe] = orig
        props = {k: _wire_prop(v or {}) for k, v in (t.get("params") or {}).items()}
        out.append({"type": "function", "function": {
            "name": safe,
            "description": str(t.get("description") or "")[:200],
            "parameters": {"type": "object", "properties": props,
                           "required": [str(k) for k in (t.get("required") or [])]}}})
    return out, name_map


def estimate_request_cost(
    messages: list[dict],
    tools: list[dict] | None = None,
    schema: dict | None = None,
) -> int:
    """请求级 token 估算（与模型服务商真实 ``prompt_tokens`` 同源）。

    关键：messages / tools 先按 wire 形态序列化（与真实发送 payload 同构），
    再统一走 ``estimate_tokens``——tools 使用**完整参数 schema**（`description[:200]` +
    ``parameters`），而非仅名字。故 est ≈ 真实账单，根治预算护栏此前 26% 的系统性低估
    （详见 docs/governance/token口径对账-问题记录.md）。

    - ``tools`` 传 native 工具目录（``catalog()`` 形状）；``None``/空 → 不计入；
    - ``schema`` 传 JSON 响应结构；非 ``None`` → 计入 ``response_format`` 附加项
      （与 DeepSeekLlm 真实发送一致：仅置 ``{"type": "json_object"}``，不发送 schema 体本身）。
    """
    payload: dict[str, Any] = {"messages": wire_messages(messages)}
    if tools:
        payload["tools"] = wire_tools(tools)[0]
    if schema:
        payload["response_format"] = {"type": "json_object"}
    return estimate_tokens(payload)


__all__ = ["wire_messages", "wire_tools", "estimate_request_cost"]
