"""agent.core.tokens —— token 估算与裁剪原语（纯函数，零依赖）。

G0 转正（D4 core/audit 同款模式）：``estimate_tokens`` 实现自 sys/llm 迁入内核，
sys 侧留兼容别名；另提供 ``clip_text`` / ``clip_json`` 裁剪原语，
供治理插件做预算内精裁、供 sys 层做源头护栏（sys 只可 import core，零特权不破）。

纯度：本模块零依赖、零业务词；不得 import sys/plugins 树（K0 守卫）。
"""

from __future__ import annotations

import json
from typing import Any

_CJK_LO, _CJK_HI = "\u4e00", "\u9fff"   # CJK 统一表意文字主区
_CJK_TOKEN_PER_CHAR = 0.75              # 中文 ≈ 每字 0.75 token（估算口径，诚实标注=estimate）

_OMIT_MARKER = "[...省略 {n} tokens...]"


def estimate_tokens(obj: Any) -> int:
    """tokens 估算（诚实标注=estimate）：ASCII 按 4 字符/token，CJK 按 0.75 token/字。

    旧口径 ``len//4`` 对中文系统性低估 2–3 倍（S1 修正），budget 预借与窗口
    裁剪一律以本函数为准。
    """
    s = json.dumps(obj, ensure_ascii=False)
    cjk = sum(1 for ch in s if _CJK_LO <= ch <= _CJK_HI)
    return max(1, int((len(s) - cjk) / 4 + cjk * _CJK_TOKEN_PER_CHAR))


def clip_text(text: str, max_tokens: int, *, head_ratio: float = 0.6) -> tuple[str, int]:
    """超预算文本裁剪原语：保头尾 + 中部省略标注。返回 ``(新文本, 省略的 token 数)``。

    - 未超限 → 原样返回 ``(text, 0)``；
    - ``max_tokens <= 0`` → 返回空串（诚实清空，不含原内容）；
    - 头部占比 ``head_ratio``，其余给尾部（G3 源头护栏语义：头尾保留、结构可辨）；
    - 字符占比近似 token 占比（同源文本内近似线性；估算口径见 estimate_tokens）。
    """
    if max_tokens <= 0:
        return "", estimate_tokens(text)
    total = estimate_tokens(text)
    if total <= max_tokens:
        return text, 0
    chars = len(text)
    keep = max(1, int(chars * max_tokens / total))
    head = max(1, min(chars, int(keep * head_ratio)))
    tail = max(0, min(chars - head, keep - head))
    middle = text[head:chars - tail] if tail else text[head:]
    omitted = estimate_tokens(middle)
    body = text[:head] + _OMIT_MARKER.format(n=omitted)
    if tail:
        body += text[chars - tail:]
    return body, omitted


def clip_json(obj: Any, max_tokens: int) -> tuple[str, int]:
    """JSON 裁剪原语：序列化后超预算 → 按 clip_text 头尾保留。

    返回 ``(json 字符串, 省略 token 数)``。注意：裁剪后的文本**不再是合法 JSON**，
    结构化保留（键摘要/字段挑选）属调用方的上层职责（G3 治理器），原语只管裁剪。
    """
    return clip_text(json.dumps(obj, ensure_ascii=False), max_tokens)


__all__ = ["estimate_tokens", "clip_text", "clip_json"]
