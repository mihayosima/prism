"""agent.core.tokens —— token 估算与裁剪原语。

G0 转正：``estimate_tokens`` 实现自 sys/llm 迁入内核，
sys 侧留兼容别名；另提供 ``clip_text`` / ``clip_json`` 裁剪原语
与 ``clip_text_hard``/ ``guard_source``，供 sys 层做源头护栏。

纯度：本模块零依赖、零业务词；不得 import sys/plugins 树。
"""

from __future__ import annotations

import json
from typing import Any

_CJK_LO, _CJK_HI = "\u4e00", "\u9fff"   # CJK 统一表意文字主区
_CJK_TOKEN_PER_CHAR = 0.75              # 中文 ≈ 每字 0.75 token

_OMIT_MARKER = "[...省略 {n} tokens...]"


def estimate_tokens(obj: Any) -> int:
    """tokens 估算：ASCII 按 4 字符/token，CJK 按 0.75 token/字。

 旧口径 ``len//4`` 对中文系统性低估 2–3 倍，budget 预借与窗口
 裁剪一律以本函数为准。
 """
    s = json.dumps(obj, ensure_ascii=False)
    cjk = sum(1 for ch in s if _CJK_LO <= ch <= _CJK_HI)
    return max(1, int((len(s) - cjk) / 4 + cjk * _CJK_TOKEN_PER_CHAR))


def clip_text(text: str, max_tokens: int, *, head_ratio: float = 0.6) -> tuple[str, int]:
    """超预算文本裁剪原语：保头尾 + 中部省略标注。返回 ``(新文本, 省略的 token 数)``。

 - 未超限 → 原样返回 ``(text, 0)``；
 - ``max_tokens <= 0`` → 返回空串；
 - 头部占比 ``head_ratio``，其余给尾部；
 - 字符占比近似 token 占比。
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

 返回 ``(json 字符串, 省略 token 数)``。注意：裁剪后的文本不再是合法 JSON，
 结构化保留属调用方的上层职责，原语只管裁剪。
 """
    return clip_text(json.dumps(obj, ensure_ascii=False), max_tokens)


def clip_text_hard(text: str, max_tokens: int) -> tuple[str, int]:
    """定点硬裁：保证 ``estimate_tokens(结果) <= max_tokens``。

 为什么需要它：``clip_text`` 只保证"删掉超出部分"，但它拼回的省略标注
 自身占 token，故结果会略超上限。
 本函数按超出比例迭代收缩目标直至实占达标——即 S3 ``governor._clip_hard`` 的
 公共化。

 - ``max_tokens <= 0`` → ``("", estimate_tokens(text))``；
 - 未超限 → ``(text, 0)``；
 - 返回 ``(裁后文本, 省略 token 数)``，其中省略数 = 原值 − 裁后；
 - 收敛方式：每轮都从原文重裁、只递减目标。
 两个反面做法都被实测否决：① 只迭代一次不做递减；② 在已裁结果上继续裁
 。
 - 已知边界：``max_tokens`` 小于标注自身开销时无法收敛，
 返回标注本身——小预算时由调用方按需处理。
 """
    if max_tokens <= 0:
        return "", estimate_tokens(text)
    out, target = text, max_tokens
    for _ in range(8):
        out, _ = clip_text(text, target)          # 始终从原文裁
        over = estimate_tokens(out) - max_tokens
        if over <= 0:
            break
        target = max(1, target - over - 1)        # 按超出量递减，标注开销为加性项
    return out, max(0, estimate_tokens(text) - estimate_tokens(out))


# ---- 源头护栏：把一次外部返回值按 token 上限净化 ----

_CHANNELS = ("structured", "content", "text")   # 外部源头的三个内容通道


def _safe_tokens(obj: Any) -> int:
    """``estimate_tokens`` 容错版：不可 JSON 化时退化为 ``str(obj)``。"""
    try:
        return estimate_tokens(obj)
    except (TypeError, ValueError):
        return estimate_tokens(str(obj))


def _dumps(obj: Any) -> str:
    """``json.dumps`` 容错版。"""
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)


def _nonempty(x: Any) -> bool:
    """「有内容」判定：``None`` / 空串 / 空容器视为空。"""
    if x is None:
        return False
    if isinstance(x, str):
        return x.strip() != ""
    if isinstance(x, (dict, list, tuple, set)):
        return len(x) > 0
    return True


def _guard_dict(value: dict, max_tokens: int) -> Any:
    """dict 形态规则：三通道择一保留，其余清空为 None。

 - 只清空不删键；
 - 只有非空通道才被选中——故"保留通道非空才让位"天然成立，
 不会出现"``text`` 空 + ``content`` 有值却两者都清空"的内容丢失；
 - 三通道全空 → 整体 ``json.dumps`` 硬化，返回裁后字符串。
 """
    keep = next((k for k in _CHANNELS if _nonempty(value.get(k))), None)
    if keep is None:
        return clip_text_hard(_dumps(value), max_tokens)[0]
    skeleton: dict = {}
    for k, v in value.items():
        skeleton[k] = "" if k == keep else (None if k in _CHANNELS else v)
    reserve = _safe_tokens(skeleton)                      # 骨架自身占额，须先扣
    raw = value[keep] if isinstance(value[keep], str) else _dumps(value[keep])
    skeleton[keep] = clip_text_hard(raw, max(1, max_tokens - reserve))[0]
    return skeleton


def _guard_value(value: Any, max_tokens: int) -> Any:
    """单轮净化。"""
    if isinstance(value, dict):
        return _guard_dict(value, max_tokens)
    if isinstance(value, str):
        return clip_text_hard(value, max_tokens)[0]
    return clip_text_hard(_dumps(value), max_tokens)[0]


def guard_source(value: Any, max_tokens: int | None) -> tuple[Any, int]:
    """源头护栏：把单次外部源头的返回值按 token 上限净化。

 契约：

 - 未超限 → ``(value, 0)`` 原样返回；
 - ``max_tokens is None`` 或 ``<= 0`` → 不裁。这与 ``clip_text``
 相反——原语是"裁"，护栏是"防爆"；护栏宁可放过、
 不可误清空。此分歧有双向单测锁定，不得"顺手统一"；
 - dict 形态规则见 ``_guard_dict``；
 - 一律走定点硬裁，保证 ``estimate_tokens(结果) <= max_tokens``；
 不得直接用 ``clip_text`` 的返回值。

 返回 ``(净化后的值, omitted_tokens)``，``omitted_tokens = estimate_tokens(原值) -
 estimate_tokens(裁后值)``。它与 ``clip_text`` 返回的"中段 token 数"
 定义不同：净减少量已含标注开销，且涵盖"清空让位通道"的节省，故此处自行计算。

 含形态取舍、不含额度取舍：上限定多少由调用方
 决定，符合"token 语义唯一归属 context"。
 """
    if max_tokens is None or max_tokens <= 0:
        return value, 0
    before = _safe_tokens(value)
    if before <= max_tokens:
        return value, 0
    guarded = _guard_value(value, max_tokens)
    for _ in range(3):                    # 值级定点：dict 骨架开销可能致略超，再收缩
        t = _safe_tokens(guarded)
        if t <= max_tokens:
            break
        guarded = _guard_value(guarded, max(1, max_tokens - (t - max_tokens)))
    return guarded, max(0, before - _safe_tokens(guarded))


__all__ = ["estimate_tokens", "clip_text", "clip_json", "clip_text_hard", "guard_source"]
