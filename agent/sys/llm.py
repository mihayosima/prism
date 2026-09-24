"""agent.sys.llm —— 模型接缝（ServiceDefinition / Provider / Consumer）。

- ``LlmProvider`` 契约见 agent.core.contract（零业务）。
- ``LlmService`` 装配成一个能力：``ctx.llm.complete(...)`` —— dispatch capability 事件
  ``agent/llm/pre-call``（waterfall：guard/budget 可能否决）→ provider 调用 → 记账
  ``agent/llm/post-call``（emit）。
- Provider 出包化：fixture 在本包 fixtures，真实实现预留（未配 → CapabilityNotImplemented，不虚标）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.api import AgentError, CapabilityNotImplemented, plugin


class LlmProvider(Protocol):
    interface_version = (1, 0)

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None) -> dict: ...


@dataclass(slots=True)
class LlmResult:
    data: dict
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = "fixture"
    provider: str = "fixture"


def _tokens(obj: Any) -> int:
    """tokens 估算（诚实标注=estimate）：字符数≈tokens 的 4 倍（粗略，用于账目）。"""
    return max(1, len(json.dumps(obj, ensure_ascii=False)) // 4)


class PlaceholderLlm:
    """占位 Provider（无法直接 import 大模型 SDK 的真实实现未接入 → 诚实拒绝，不虚标）。"""

    interface_version = (1, 0)
    label = "placeholder"

    def __init__(self, provider: str):
        self._provider = provider

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None) -> dict:
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
                       schema: dict | None = None, caller: str = "unknown") -> LlmResult:
        decision = await ctx.events.waterfall(
            "agent/llm/pre-call", {"messages": messages, "schema": schema, "caller": caller}
        )
        if isinstance(decision, dict) and decision.get("denied"):
            raise CapabilityNotImplemented(
                "agent.llm.complete", decision.get("reason", "被 guard/budget 否决")
            )
        data = await self._provider.complete(ctx, messages, schema=schema)
        tokens_in, tokens_out = _tokens(messages), _tokens(data)
        await ctx.events.emit("agent/llm/post-call", {
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


class DeepSeekLlm:
    """DeepSeek 官方 API（OpenAI 兼容 chat/completions）。零业务词；凭据不落代码。

    key 解析：config.agent.llm.api_key → env DEEPSEEK_API_KEY → 工程根 local_secrets.json。
    """

    interface_version = (1, 0)
    label = "deepseek"
    BASE_URL = "https://api.deepseek.com/chat/completions"

    def __init__(self, model: str, api_key: str):
        self._model = model
        self._key = api_key

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None) -> dict:
        import httpx

        payload = {
            "model": self._model,
            "messages": messages,
            "temperature": 0.2,
        }
        if schema:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(self.BASE_URL, json=payload, headers=headers)
        if resp.status_code != 200:
            raise AgentError(f"deepseek api 异常 {resp.status_code}: {resp.text[:200]}")
        content = resp.json()["choices"][0]["message"]["content"]
        # 有 schema（要结构化）→ 解析 JSON；无 schema（要散文）→ 原样文本
        if schema is None:
            return {"text": content}
        return _parse_json(content)


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
    provides = ["llm"]
    inject = ["events"]

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
        ctx.provide("llm", LlmService(provider, model))


__all__ = ["LlmPlugin", "LlmResult", "LlmService", "LlmProvider", "PlaceholderLlm", "_tokens"]