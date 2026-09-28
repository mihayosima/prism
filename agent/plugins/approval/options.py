"""plugins.approval.options —— 问询候选项的形态归一（纯函数，无 IO、无状态）。

模型交出的 ``options`` 形态天然是多样的：可能是 JSON 数组字符串
（``'["A","B"]'``）、可能是对象数组（带说明与分组）、也可能是逗号分隔的纯文本
（``"A, B"``）。本模块把它统一成**选择规格**：

    [{"label": "...", "value": "...", "desc": "...", "group": "..."}]

前端据此渲染形态匹配的选择栏（单选/多选、带说明的整行选项、按维度分区），
而不是把所有候选压进同一个扁平选择栏。

三条规则：
1. **JSON 解析优先** —— 避免选项文本里含逗号时被误切；
2. **换行/逗号兜底** —— 旧提示词与历史调用零破坏；
3. **逐项清洗** —— 剥掉 JSON 残渣引号与括号（历史事故：回传的 ``choice``
   值带着引号，``"电影主题曲"``，正是逗号硬切留下的碎片）。
"""

from __future__ import annotations

import json

# 对象型候选项的等价键名（模型用词不统一，做同义归并）
_LABEL_KEYS = ("label", "title", "text", "name", "option")
_DESC_KEYS = ("desc", "description", "detail", "hint", "note", "why")
_VALUE_KEYS = ("value", "id", "key")
_GROUP_KEYS = ("group", "category", "section", "dimension")

_MAX_ITEMS = 24          # 单卡片候选上限（防模型倾倒，逐次截断）
_MAX_CHARS = 200         # 单项文本上限


def clean(text) -> str:
    """剥掉 JSON 残渣：成对引号与首尾括号（可多层，如 ``[\\"方案A\\"`` → ``方案A``）。"""
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
    """把任意入参拆成候选项原始列表（只负责拆，不解析语义）。"""
    if raw is None:
        return []
    if isinstance(raw, dict):
        # 模型偶发把候选写成映射 {方案: 说明} —— 视为「带说明的候选」
        if raw and all(isinstance(v, (str, int, float)) or v is None
                       for v in raw.values()):
            return [{"label": k, "desc": v} for k, v in raw.items()]
        return [raw]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    text = str(raw).strip()
    if not text:
        return []
    if text[0] in "[{":                          # 1) JSON 优先（选项含逗号亦安全）
        try:
            got = json.loads(text)
        except (ValueError, TypeError):
            got = None
        if isinstance(got, dict):
            return [got]
        if isinstance(got, list):
            return list(got)
    # 2) 兜底：换行 / 逗号 / 顿号 / 分号 统一折算为分隔
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
    """归一为选择规格 ``[{"label", "value", "desc", "group"}]``（label 去重保序）。"""
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
