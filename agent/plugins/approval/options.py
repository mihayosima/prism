"""候选项形态归一：把模型给出的 options 统一为选择规格 ``[{"label","value","desc","group"}]``。"""

from __future__ import annotations

import json

# 模型用词不统一，做同义键归并
_LABEL_KEYS = ("label", "title", "text", "name", "option")
_DESC_KEYS = ("desc", "description", "detail", "hint", "note", "why")
_VALUE_KEYS = ("value", "id", "key")
_GROUP_KEYS = ("group", "category", "section", "dimension")

_MAX_ITEMS = 24          # 单卡片候选上限
_MAX_CHARS = 200         # 单项文本上限


def clean(text) -> str:
    """剥掉 JSON 残渣，并截断到 _MAX_CHARS。"""
    s = str(text if text is not None else "").strip()
    for _ in range(2):
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
            s = s[1:-1].strip()
    if s[:1] in "[{":
        s = s[1:].strip()
    if s[-1:] in "]}":
        s = s[:-1].strip()
    for _ in range(2):
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
            s = s[1:-1].strip()
    return s[:_MAX_CHARS]


def _as_items(raw) -> list:
    """把任意入参拆成候选项原始列表。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        # 模型偶发把候选写成映射 {方案: 说明} —— 视为「带说明的候选」
        # 映射写法 {方案: 说明} 视为带说明的候选
        if raw and all(isinstance(v, (str, int, float)) or v is None
                       for v in raw.values()):
            return [{"label": k, "desc": v} for k, v in raw.items()]
        return [raw]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    text = str(raw).strip()
    if not text:
        return []
    if text[0] in "[{":                          # 先试 JSON 解析
        try:
            got = json.loads(text)
        except (ValueError, TypeError):
            got = None
        if isinstance(got, dict):
            return [got]
        if isinstance(got, list):
            return list(got)
    # 非 JSON：按换行/逗号/顿号/分号切分
    for sep in ("\n", "，", ",", "、", "；", ";"):
        text = text.replace(sep, "\n")
    return [line for line in (p.strip() for p in text.split("\n")) if line]


def _pick(src: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        v = src.get(k)
        if v is not None and str(v).strip():
            return clean(v)
    return ""


def normalize_options(raw) -> list[dict]:
    """归一为选择规格 ``[{"label", "value", "desc", "group"}]``。"""
    out: list[dict] = []
    seen: set[str] = set()
    for item in _as_items(raw):
        if isinstance(item, dict):
            label = _pick(item, _LABEL_KEYS) or _pick(item, _VALUE_KEYS)
            desc = _pick(item, _DESC_KEYS)
            value = _pick(item, _VALUE_KEYS) or label
            group = _pick(item, _GROUP_KEYS)
        else:
            label = clean(item)
            desc, group, value = "", "", label
        if not label or label in seen:
            continue
        seen.add(label)
        out.append({"label": label, "value": value or label,
                    "desc": desc, "group": group})
        if len(out) >= _MAX_ITEMS:
            break
    return out


__all__ = ["clean", "normalize_options"]
