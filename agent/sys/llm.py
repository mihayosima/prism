"""agent.sys.llm —— 模型接缝（ServiceDefinition / Provider / Consumer）。

- ``LlmProvider`` 契约见 agent.core.contract（零业务）。
- ``LlmService`` 装配成一个能力：``ctx.llm.complete(...)`` —— dispatch capability 事件
  ``agent/llm/pre-call``（waterfall：guard/budget 可能否决）→ provider 调用 → 记账
  ``agent/llm/post-call``（emit）。
- Provider 出包化：fixture 在本包 fixtures，真实实现预留（未配 → CapabilityNotImplemented，不虚标）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.api import AgentError, CapabilityNotImplemented, Events, Services, plugin
from agent.sys.audit import durable


class LlmProvider(Protocol):
    interface_version = (1, 0)

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None,
                       tools: list[dict] | None = None) -> dict: ...


@dataclass(slots=True)
class LlmResult:
    data: dict
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = "fixture"
    provider: str = "fixture"


_CJK_LO, _CJK_HI = "\u4e00", "\u9fff"   # CJK 统一表意文字主区
_CJK_TOKEN_PER_CHAR = 0.75              # 中文 ≈ 每字 0.75 token（估算口径，诚实标注=estimate）


def _tokens(obj: Any) -> int:
    """tokens 估算（诚实标注=estimate）：ASCII 按 4 字符/token，CJK 按 0.75 token/字。

    旧口径 ``len//4`` 对中文系统性低估 2–3 倍（S1 修正），budget 预借以本函数为准。
    """
    s = json.dumps(obj, ensure_ascii=False)
    cjk = sum(1 for ch in s if _CJK_LO <= ch <= _CJK_HI)
    return max(1, int((len(s) - cjk) / 4 + cjk * _CJK_TOKEN_PER_CHAR))


class PlaceholderLlm:
    """占位 Provider（无法直接 import 大模型 SDK 的真实实现未接入 → 诚实拒绝，不虚标）。"""

    interface_version = (1, 0)
    label = "placeholder"

    def __init__(self, provider: str):
        self._provider = provider

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None,
                       tools: list[dict] | None = None) -> dict:
        raise CapabilityNotImplemented(
            "agent.llm.complete",
            f"provider={self._provider!r} 未接入（fixture 离线走通；真实实现需凭据/适配）",
        )


class LlmService:
    """ctx.llm —— 装配后的模型接缝（capability 事件包夹）。"""

    def __init__(self, provider: LlmProvider, model: str):
        self._provider = provider
        self._model = model

    @property
    def provider_label(self) -> str:
        return getattr(self._provider, "label", type(self._provider).__name__)

    async def complete(self, ctx, messages: list[dict], *,
                       schema: dict | None = None, tools: list[dict] | None = None,
                       caller: str = "unknown") -> LlmResult:
        decision = await ctx.events.waterfall(
            Events.LLM_PRE_CALL,
            {"messages": messages, "schema": schema, "caller": caller,
             "tools": [str(t.get("name", "")) for t in (tools or [])]},
        )
        if isinstance(decision, dict) and decision.get("denied"):
            raise CapabilityNotImplemented(
                "agent.llm.complete", decision.get("reason", "被 guard/budget 否决")
            )
        try:
            data = await self._provider.complete(ctx, messages, schema=schema, tools=tools)
        except Exception as exc:
            # S1：失败路径——durable 审计 + live 通知（budget 同步退款），随后原样抛出。
            # 成功路径行为零变化。
            payload = {"caller": caller, "error": str(exc)}
            await durable(ctx, Events.LLM_CALL_FAILED, dict(payload))
            await ctx.events.emit(Events.LLM_CALL_FAILED, dict(payload))
            raise
        tokens_in, tokens_out = _tokens(messages), _tokens(data)
        await ctx.events.emit(Events.LLM_POST_CALL, {
            "provider": self._model, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "caller": caller,
        })
        return LlmResult(data=data, tokens_in=tokens_in, tokens_out=tokens_out,
                         model=self._model, provider=self.provider_label)


def _parse_json(text: str) -> dict:
    """剥离 markdown code-fence 后解析 JSON。"""
    import json
    import re

    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    data = json.loads(s)
    if not isinstance(data, dict):
        raise ValueError("deepseek 返回非 dict JSON")
    return data


def _wire_messages(messages: list[dict]) -> list[dict]:
    """内部消息形状 → OpenAI 兼容 wire 形状（无工具时等价恒等）。

    内部：``{"role": "assistant", "content": str, "tool_calls": [{id,name,arguments}]}``
    与 ``{"role": "user", "content": {"tool_results": [...]}}``（观察回灌）。
    wire：assistant.tool_calls[].function.arguments 为 JSON 字符串；观察展开为多条 role=tool。
    """
    out: list[dict] = []
    for m in messages or []:
        role = m.get("role")
        content = m.get("content")
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
                        "tool_calls": calls})
            continue
        if isinstance(content, dict) and content.get("tool_results") is not None:
            for r in content["tool_results"]:
                out.append({"role": "tool",
                            "tool_call_id": str(r.get("tool_call_id") or ""),
                            "content": str(r.get("content") or "")})
            continue
        if isinstance(content, str):
            out.append({"role": role, "content": content})
        else:
            out.append({"role": role, "content": json.dumps(content, ensure_ascii=False)})
    return out


_JSON_TYPES = {"str": "string", "int": "integer", "float": "number",
               "bool": "boolean", "array": "array", "object": "object"}

# OpenAI / DeepSeek 工具名硬约束：仅允许 ^[a-zA-Z0-9_-]+$。
# 内部工具名带命名空间（如 mcp.windows.xxx、agent.ping），发往模型前必须净化，
# 模型回传的净化名再映射回内部原名以驱动 ctx.tasks.invoke。
_TOOL_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")


def _safe_tool_name(name: str) -> str:
    """内部工具名 → 合规 wire 名（非法字符替换成 '_'）。"""
    safe = _TOOL_NAME_UNSAFE.sub("_", name or "")
    return safe or "tool"


def _wire_tools(tools: list[dict]) -> tuple[list[dict], dict[str, str]]:
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
        props = {k: {"type": _JSON_TYPES.get(str(v.get("type", "object")), "object")}
                 for k, v in (t.get("params") or {}).items()}
        out.append({"type": "function", "function": {
            "name": safe,
            "description": str(t.get("description") or "")[:200],
            "parameters": {"type": "object", "properties": props,
                           "required": [str(k) for k in (t.get("required") or [])]}}})
    return out, name_map


def _wire_call(raw: dict, name_map: dict[str, str] | None = None) -> dict:
    fn = raw.get("function") or {}
    raw_name = str(fn.get("name") or "")
    # 模型回传的是净化名，还原成内部原名（找不到则原样回退，兼容 fixture 路径）
    name = (name_map or {}).get(raw_name, raw_name)
    try:
        args = json.loads(fn.get("arguments") or "{}")
    except (TypeError, ValueError):
        args = {}
    return {"id": str(raw.get("id") or ""), "name": name,
            "arguments": args if isinstance(args, dict) else {}}


class DeepSeekLlm:
    """DeepSeek 官方 API（OpenAI 兼容 chat/completions）。零业务词；凭据不落代码。

    key 解析：config.agent.llm.api_key → env DEEPSEEK_API_KEY → 工程根 local_secrets.json。
    ``tools`` 走 native function-calling（wire 转换见 ``_wire_messages`` / ``_wire_tools``）。
    """

    interface_version = (1, 0)
    label = "deepseek"
    BASE_URL = "https://api.deepseek.com/chat/completions"

    def __init__(self, model: str, api_key: str):
        self._model = model
        self._key = api_key

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None,
                       tools: list[dict] | None = None) -> dict:
        import httpx

        cfg = (((ctx.config or {}).get("agent") or {}).get("llm") or {})
        stream_on = bool(cfg.get("stream", True))      # D5 默认 true

        payload: dict = {
            "model": self._model,
            "messages": _wire_messages(messages),
            "temperature": 0.2,
        }
        name_map: dict[str, str] = {}
        if tools:
            wired_tools, name_map = _wire_tools(tools)
            payload["tools"] = wired_tools
            payload["tool_choice"] = "auto"
        if schema:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json"}

        if not stream_on:
            # —— 非流式（行为同今天）——
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(self.BASE_URL, json=payload, headers=headers)
            if resp.status_code != 200:
                raise AgentError(f"deepseek api 异常 {resp.status_code}: {resp.text[:200]}")
            message = resp.json()["choices"][0]["message"]
            content = message.get("content") or ""
            raw_calls = message.get("tool_calls") or []
            if raw_calls:                                  # 工具调用优先（可能 content 为空）
                return {"text": content,
                        "tool_calls": [_wire_call(c, name_map) for c in raw_calls]}
            if schema is None:
                return {"text": content}
            return _parse_json(content)

        # —— 流式（B5.2 打字机）——
        payload["stream"] = True                           # 关键：告知 DeepSeek 返回 SSE 而非单条 JSON
        content_acc = ""
        reasoning_acc = ""                                # D4 思考链（reasoning_content 每片累积）
        tool_acc: dict[int, dict] = {}                     # index -> 归并槽（D3 完整重组）
        async with httpx.AsyncClient(timeout=90.0) as client:
            async with client.stream("POST", self.BASE_URL, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    await resp.aread()                      # 流式下不能 resp.json()
                    raise AgentError(f"deepseek api 异常 {resp.status_code}: {resp.text[:200]}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    if not data:
                        continue
                    try:
                        obj = json.loads(data)
                    except (TypeError, ValueError):
                        continue                            # 跳过非 JSON 行（注释/心跳）
                    choice = (obj.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    # 文本片段：schema 为 None（用户可见散文）才发帧；
                    # D4 照单全流——带 tool_calls 轮的 content（思考文本）也照发
                    piece = delta.get("content") or ""
                    if piece and schema is None:
                        content_acc += piece
                        await ctx.events.emit(Events.LLM_TOKEN, {"text": piece})
                    # D4 思考链（reasoning_content）照单全流：模型把推理过程放在该字段，
                    # 不并入最终 text（text 仅来自 content），避免思考混入答案。
                    rpiece = delta.get("reasoning_content") or ""
                    if rpiece and schema is None:
                        reasoning_acc += rpiece
                        await ctx.events.emit(Events.LLM_THINKING, {"text": rpiece})
                    # 工具调用 delta 归并（按 index）
                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index", 0)
                        slot = tool_acc.setdefault(idx, {
                            "id": "", "type": "function",
                            "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        if tc.get("type"):
                            slot["type"] = tc["type"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["function"]["arguments"] += fn["arguments"]
                    if choice.get("finish_reason"):
                        break
        raw_calls = [slot for _idx, slot in sorted(tool_acc.items())]
        if raw_calls:                                      # 工具调用优先（可能 content 为空）
            return {"text": content_acc,
                    "tool_calls": [_wire_call(c, name_map) for c in raw_calls]}
        if schema is None:
            return {"text": content_acc}
        return _parse_json(content_acc)


def _resolve_key(ctx) -> str | None:
    import os

    cfg = ((ctx.config or {}).get("agent") or {}).get("llm") or {}
    if cfg.get("api_key"):
        return str(cfg["api_key"])
    if os.environ.get("DEEPSEEK_API_KEY"):
        return os.environ["DEEPSEEK_API_KEY"]
    from pathlib import Path

    secret = Path(__file__).resolve().parent.parent.parent / "local_secrets.json"
    if secret.is_file():
        import json

        try:
            data = json.loads(secret.read_text(encoding="utf-8"))
            return data.get("deepseek_api_key") or None
        except Exception:  # noqa: BLE001 —— 秘钥文件损坏不崩装配
            return None
    return None


@plugin
class LlmPlugin:
    """ctx.llm —— 按配置选 Provider（fixture 离线 / deepseek 真实 / 其它占位诚实拒绝）。"""

    name = "agent.llm"
    layer = "system"
    provides = [Services.LLM]
    inject = [Services.EVENTS]

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("llm") or {}
        name_ = cfg.get("provider", "fixture")
        model = cfg.get("model")
        if name_ in ("fixture", "offline"):
            from agent.fixtures.llm import FixtureLlm

            provider = FixtureLlm()
            model = model or "fixture"
        elif name_ in ("deepseek", "ds"):
            key = _resolve_key(ctx)
            model = model or "deepseek-flash"          # 线上 API 合法名：deepseek-flash / deepseek-v4-pro
            if not key:
                provider = PlaceholderLlm("deepseek（未配置 key：env DEEPSEEK_API_KEY 或 local_secrets.json）")
            else:
                provider = DeepSeekLlm(model, key)
        else:
            provider = PlaceholderLlm(name_)
            model = model or name_
        ctx.provide(Services.LLM, LlmService(provider, model))


__all__ = ["LlmPlugin", "LlmResult", "LlmService", "LlmProvider", "PlaceholderLlm", "_tokens"]