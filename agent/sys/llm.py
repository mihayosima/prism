"""agent.sys.llm —— 模型接缝（ServiceDefinition / Provider / Consumer）。

- ``LlmProvider`` 契约见 agent.core.contract（零业务）。
- ``LlmService`` 装配成一个能力：``ctx.llm.complete(...)`` —— dispatch capability 事件
  ``agent/llm/pre-call``（waterfall：guard/budget 可能否决）→ provider 调用 → 记账
  ``agent/llm/post-call``（emit）。
- Provider 出包化：fixture 在本包 fixtures，真实实现预留（未配 → CapabilityNotImplemented，不虚标）。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

# LlmProvider 契约的单一定义在 core/contract.py，经 api 出口取用（此前本文件有一份
# 字节级同构的副本，改契约时只改一处即静默漂移）。
from agent.api import (AgentError, CapabilityNotImplemented, Events, LlmProvider,
                       LlmUnavailableError, Services, durable, now_iso, plugin)
from agent.api import estimate_tokens
from agent.api import wire_messages as _wire_messages, wire_tools as _wire_tools


@dataclass(slots=True)
class LlmResult:
    data: dict
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = "fixture"
    provider: str = "fixture"
    # G2.0（P0）：思考 token 与正文 token 的分离计数（provider 未提供该明细时为 None，
    # **不猜**——诚实留空，便于事后区分「推理冗长」与「回应冗长」）。单位同 tokens_out。
    reasoning_tokens: int | None = None


def _reasoning_tokens(usage: dict) -> int | None:
    """从 usage 明细里取思考 token 数（OpenAI/DeepSeek 兼容的 ``completion_tokens_details``）。

    取不到即返回 ``None``（**不估、不猜**）：该字段是「44% 输出成本中推理占多少」
    这一问题的唯一可信来源；缺它时如实留空，由 X-1 实验在真实 provider 上核验。
    """
    det = (usage or {}).get("completion_tokens_details")
    if not isinstance(det, dict):
        return None
    val = det.get("reasoning_tokens")
    if val is None:
        return None
    try:
        return int(val)
    except (TypeError, ValueError):
        return None


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

    @property
    def model(self) -> str:
        """当前模型名（公开只读；控制台状态栏展示用）。"""
        return self._model

    async def complete(self, ctx, messages: list[dict], *,
                       schema: dict | None = None, tools: list[dict] | None = None,
                       caller: str = "unknown", finalize: bool = False) -> LlmResult:
        """模型调用（消费方视角，契约见 ``core/contract.LlmProvider``）。

        pre-call waterfall（guard→budget 预借）→ provider.complete → post-call 入账；
        失败发 ``agent/llm/call-failed``（budget 同步监听器内联退款）后原样抛出。
        结果与 ``stream`` 配置无关（schema 调用走流式也返回已解析 dict）。
        ``finalize=True``（方案A）：标记为触顶收尾调用——budget 据此允许动用
        收尾预留额度（正常调用只能用 ``max_tokens_per_run - finalize_reserve_tokens``）。
        """
        # call_id：本次调用的唯一关联键，贯穿 pre/post/failed 三个 payload。
        # 为什么需要它：budget 的预借槽此前按 **caller** 关联 pre↔post，而 post 是
        # fire-and-forget —— 同 caller 的连续/并发调用会互相覆盖槽位（先到的 post 弹出
        # 后到的预借），使真实 token 记错账、EWMA 少样本。改用唯一 call_id 后关联无歧义。
        call_id = uuid.uuid4().hex
        decision = await ctx.events.waterfall(
            Events.LLM_PRE_CALL,
            {"messages": messages, "schema": schema, "caller": caller,
             "tools": tools, "finalize": bool(finalize), "call_id": call_id},
            # 传原始工具目录（native 形态），由 budget 经 core 估算器同源计量
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
            payload = {"caller": caller, "error": str(exc), "call_id": call_id}
            await durable(ctx, Events.LLM_CALL_FAILED, dict(payload))
            await ctx.events.emit(Events.LLM_CALL_FAILED, dict(payload))
            raise
        # E 遥测：生成被服务端截断（finish_reason=length）→ durable 落盘，可事后回放。
        # 这是当晚「漏 mode 参数」事故的真源头，此前此信号被完全丢弃。
        if isinstance(data, dict) and data.get("truncated"):
            await durable(ctx, Events.LLM_TRUNCATED,
                          {"caller": caller, "finish_reason": data.get("finish_reason"),
                           "at": now_iso()})
        # S6：优先用 provider 返回的真实 usage 入账（诚实口径）；无则回退估算。
        usage = data.pop("usage", None) if isinstance(data, dict) else None
        cache_hit = None                     # P3：前缀缓存命中（DeepSeek usage 字段，尽力而为）
        reasoning_tokens = None              # P0：思考 token 与正文 token 分离遥测（尽力而为）
        if usage and (usage.get("prompt_tokens") or usage.get("completion_tokens")):
            tokens_in = int(usage.get("prompt_tokens") or 0) or estimate_tokens(messages)
            tokens_out = int(usage.get("completion_tokens") or 0) or estimate_tokens(data)
            if usage.get("prompt_cache_hit_tokens") is not None:
                cache_hit = int(usage.get("prompt_cache_hit_tokens") or 0)
            reasoning_tokens = _reasoning_tokens(usage)
        else:
            tokens_in, tokens_out = estimate_tokens(messages), estimate_tokens(data)
        await ctx.events.emit(Events.LLM_POST_CALL, {
            "provider": self._model, "tokens_in": tokens_in, "tokens_out": tokens_out,
            "caller": caller, "cache_hit": cache_hit,
            "reasoning_tokens": reasoning_tokens, "call_id": call_id,
        })
        return LlmResult(data=data, tokens_in=tokens_in, tokens_out=tokens_out,
                         model=self._model, provider=self.provider_label,
                         reasoning_tokens=reasoning_tokens)


def _parse_json(text: str) -> dict:
    """剥离 markdown code-fence 后解析 JSON。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    data = json.loads(s)
    if not isinstance(data, dict):
        raise ValueError("deepseek 返回非 dict JSON")
    return data


def _wire_call(raw: dict, name_map: dict[str, str] | None = None) -> dict:
    fn = raw.get("function") or {}
    raw_name = str(fn.get("name") or "")
    # 模型回传的是净化名，还原成内部原名（找不到则原样回退，兼容 fixture 路径）
    name = (name_map or {}).get(raw_name, raw_name)
    raw_args = fn.get("arguments")
    truncated = False
    try:
        args = json.loads(raw_args or "{}")
    except (TypeError, ValueError):
        # E（生成侧截断防护）：残缺/截断的参数 JSON **不再静默吞成 {}**——
        # 显式打 truncated 标记并保留原始片段，由 Actor 回灌自纠指引
        # （此前这里的静默吞正是「缺少必填参数 mode」误导表象的来源）。
        args, truncated = {}, True
    out: dict = {"id": str(raw.get("id") or ""), "name": name,
                 "arguments": args if isinstance(args, dict) else {}}
    if truncated:
        out["truncated"] = True
        out["arguments_raw"] = str(raw_args or "")[:2000]
    return out


class DeepSeekLlm:
    """DeepSeek 官方 API（OpenAI 兼容 chat/completions）。零业务词；凭据不落代码。

    key 解析：config.agent.llm.api_key → env DEEPSEEK_API_KEY → 工程根 local_secrets.json。
    ``tools`` 走 native function-calling（wire 转换见 ``_wire_messages`` / ``_wire_tools``）。
    S6：``AsyncClient`` 懒初始化并跨调用复用（连接池），``close()`` 释放；
    优先采集响应 ``usage`` 真实 token 数（非流式直读、流式经 ``stream_options.include_usage``），
    经返回值 ``usage`` 键上交 LlmService 入账。
    ``transport`` 仅供测试注入 httpx.MockTransport。
    """

    interface_version = (1, 0)
    label = "deepseek"
    BASE_URL = "https://api.deepseek.com/chat/completions"

    def __init__(self, model: str, api_key: str, transport: Any = None):
        self._model = model
        self._key = api_key
        self._transport = transport
        self._client: Any = None

    def _get_client(self) -> Any:
        import httpx

        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=90.0, transport=self._transport)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # #3：可重试的 HTTP 状态（限流/网关 5xx/网络抖动视为瞬态；4xx 视为不可重试）。
    _RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

    def _backoff(self, attempt: int) -> float:
        """指数退避 + 抖动（±50%），避免重试风暴对上游造成同步冲击。"""
        base = self._retry_backoff_s * (2 ** attempt)
        return base * (0.5 + random.random() * 0.5)

    async def _post_with_retry(self, client, payload, headers):
        """非流式封装：对可重试错误做指数退避重试；末次仍失败抛 LlmUnavailableError。

        成功返回 200 的 resp；不可重试状态（400/401/403 等）立即抛 AgentError（维持旧语义）。
        """
        last_status = None
        last_exc = None
        for attempt in range(self._retry_max + 1):
            try:
                resp = await client.post(self.BASE_URL, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.ConnectError, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self._retry_max:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                break
            if resp.status_code == 200:
                return resp
            last_status = resp.status_code
            if resp.status_code in self._RETRYABLE_STATUS:
                if attempt < self._retry_max:
                    await asyncio.sleep(self._backoff(attempt))
                    continue
                break  # 末次仍 5xx/429 → 退出循环，统一抛 LlmUnavailableError
            raise AgentError(f"deepseek api 异常 {resp.status_code}: {resp.text[:200]}")
        raise LlmUnavailableError(
            f"llm 在 {self._retry_max} 次重试后仍不可用；last_status={last_status}；"
            f"last_error={last_exc}"
        )

    async def _stream_with_retry(self, client, payload, headers, ctx, schema):
        """流式重试编排：复用 _stream_once，仅对「首帧前」的瞬态失败重试。"""
        last_status = None
        last_exc = None
        for attempt in range(self._retry_max + 1):
            outcome = await self._stream_once(client, payload, headers, ctx, schema)
            if outcome[0] == "ok":
                return outcome[1]
            if outcome[0] == "fail":
                status, text = outcome[1], outcome[2]
                raise AgentError(f"deepseek api 异常 {status}: {text}")
            # outcome[0] == "retry"
            last_status, last_exc = outcome[1], outcome[2]
            if attempt < self._retry_max:
                await asyncio.sleep(self._backoff(attempt))
                continue
            break
        raise LlmUnavailableError(
            f"llm 在 {self._retry_max} 次重试后仍不可用；last_status={last_status}；"
            f"last_error={last_exc}"
        )

    async def _stream_once(self, client, payload, headers, ctx, schema):
        """单次流式请求与聚合（含实时 token/thinking 帧发射）。

        返回元组首元素：
          "ok"    —— 成功，(content_acc, reasoning_acc, tool_acc, usage, finish)
          "retry" —— 瞬态失败，(status|None, error_str)，调用方可重试
          "fail"  —— 不可重试失败，(status, text)，调用方立即抛 AgentError
        三档边界（v1.1 固化）：连接建立失败 / 首帧前断开 → 可重试；
        首帧后断开 → 原样抛出（避免半截结果被当成完整响应）。
        重试只发生在「首帧前」，故 token/thinking 帧仅在最终成功的那次发出，不会重复。
        """
        content_acc = ""
        reasoning_acc = ""
        tool_acc: dict[int, dict] = {}
        usage: dict | None = None
        finish: str | None = None
        first_frame = False
        try:
            async with client.stream("POST", self.BASE_URL, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    await resp.aread()                  # 流式下不能 resp.json()
                    text = resp.text[:200]
                    if resp.status_code in self._RETRYABLE_STATUS:
                        return ("retry", resp.status_code, text)
                    return ("fail", resp.status_code, text)
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
                    first_frame = True
                    usage = obj.get("usage") or usage       # S6：usage 帧不含 choices，照单收下
                    choice = (obj.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    # 文本片段：**累积永远执行**（schema 调用也要拿到完整 content 解析 JSON）；
                    # 仅 token 帧受 schema 门控——schema 调用的中间散文不外发（D5 设计）。
                    piece = delta.get("content") or ""
                    if piece:
                        content_acc += piece
                        if schema is None:
                            await ctx.events.emit(Events.LLM_TOKEN, {"text": piece})
                    # D4 思考链（reasoning_content）照单全流：模型把推理过程放在该字段，
                    # 不并入最终 text（text 仅来自 content），避免思考混入答案。
                    # B-fix：无论 schema 门控与否都要**累积**——回传历史须携带，丢弃即 400。
                    rpiece = delta.get("reasoning_content") or ""
                    if rpiece:
                        reasoning_acc += rpiece
                        if schema is None:
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
                        finish = choice.get("finish_reason")   # E：此前仅用于 break，现在留证
                        break
        except (httpx.TimeoutException, httpx.ConnectError, httpx.TransportError) as exc:
            # 连接建立失败，或（首帧前）断开 → 可重试；首帧后断开 → 原样抛出（不重试）
            if not first_frame:
                return ("retry", None, str(exc))
            raise
        return ("ok", (content_acc, reasoning_acc, dict(tool_acc), usage, finish))

    async def complete(self, ctx: Any, messages: list[dict],
                       schema: dict | None = None,
                       tools: list[dict] | None = None) -> dict:
        cfg = (((ctx.config or {}).get("agent") or {}).get("llm") or {})
        stream_on = bool(cfg.get("stream", True))      # D5 默认 true
        # #3：重试参数（默认 3 次指数退避；max_retries=0 退化为旧行为：零重试）
        self._retry_max = int(cfg.get("max_retries", 3) or 0)
        self._retry_backoff_s = float(cfg.get("retry_backoff_s", 0.5) or 0.5)

        payload: dict = {
            "model": self._model,
            "messages": _wire_messages(messages),
            "temperature": 0.2,
        }
        # E：显式生成上限——「不设」等于接受服务端默认（deepseek-flash 非思考 8K），
        # 长参数（如写章节全文进工具 arguments）极易被 finish_reason="length" 腰斩。
        # 取值来自 config.agent.llm.max_tokens（默认 8192；<=0 视为不设置，交还服务端）。
        max_tokens = int(cfg.get("max_tokens", 8192) or 0)
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens
        name_map: dict[str, str] = {}
        if tools:
            wired_tools, name_map = _wire_tools(tools)
            payload["tools"] = wired_tools
            payload["tool_choice"] = "auto"
        if schema:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self._key}",
                   "Content-Type": "application/json"}

        client = self._get_client()                    # S6：跨调用复用连接池

        if not stream_on:
            # —— 非流式（行为同今天，仅叠加可重试层）——
            resp = await self._post_with_retry(client, payload, headers)
            # 此处 status 必为 200（非 200 重试耗尽已抛 LlmUnavailableError，
            # 不可重试状态已抛 AgentError）
            body = resp.json()
            message = body["choices"][0]["message"]
            finish = body["choices"][0].get("finish_reason")
            usage = body.get("usage")                  # S6：真实 token 数（若有）
            content = message.get("content") or ""
            # B-fix：thinking 模式思考链——回传历史时 DeepSeek 要求携带，必须捕获随行
            reasoning = message.get("reasoning_content") or ""
            reasoning_kv = {"reasoning_content": reasoning} if reasoning else {}
            raw_calls = message.get("tool_calls") or []
            if raw_calls:                                  # 工具调用优先（可能 content 为空）
                out = {"text": content,
                       "tool_calls": [_wire_call(c, name_map) for c in raw_calls],
                       **reasoning_kv,
                       **({"usage": usage} if usage else {})}
                if finish == "length":                     # E：截断显式标记，绝不静默
                    out["truncated"] = True
                return out
            if schema is None:
                out = {"text": content, **reasoning_kv, **({"usage": usage} if usage else {})}
                if finish == "length":
                    out["truncated"] = True
                return out
            parsed = _parse_json(content)
            if reasoning:
                parsed["reasoning_content"] = reasoning
            if finish == "length":
                parsed["truncated"] = True
            if usage:
                parsed["usage"] = usage
            return parsed

        # —— 流式（B5.2 打字机，叠加可重试层）——
        payload["stream"] = True                           # 关键：告知 DeepSeek 返回 SSE 而非单条 JSON
        payload["stream_options"] = {"include_usage": True}  # S6：末帧附带真实 usage
        content_acc, reasoning_acc, tool_acc, usage, finish = await self._stream_with_retry(
            client, payload, headers, ctx, schema)
        raw_calls = [slot for _idx, slot in sorted(tool_acc.items())]
        usage_kv = {"usage": usage} if usage else {}
        reasoning_kv = {"reasoning_content": reasoning_acc} if reasoning_acc else {}
        if raw_calls:                                      # 工具调用优先（可能 content 为空）
            out = {"text": content_acc,
                   "tool_calls": [_wire_call(c, name_map) for c in raw_calls],
                   **reasoning_kv, **usage_kv}
            if finish == "length":                         # E：截断显式标记，绝不静默
                out["truncated"] = True
            return out
        if schema is None:
            out = {"text": content_acc, **reasoning_kv, **usage_kv}
            if finish == "length":
                out["truncated"] = True
            return out
        parsed = _parse_json(content_acc)
        if reasoning_acc:
            parsed["reasoning_content"] = reasoning_acc
        if finish == "length":
            parsed["truncated"] = True
        if usage:
            parsed["usage"] = usage
        return parsed


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
        close = getattr(provider, "close", None)
        if close is not None:                          # S6：provider 持有连接池 → teardown 释放
            ctx.effect(lambda: close())
        ctx.provide(Services.LLM, LlmService(provider, model))


__all__ = ["LlmPlugin", "LlmResult", "LlmService", "LlmProvider", "PlaceholderLlm"]
