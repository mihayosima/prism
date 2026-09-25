"""agent.plugins.short_term.truncate —— 两层压缩的纯逻辑（可单测，不依赖 ctx）。

- ``truncate_content`` / ``truncate_messages``：第一层**本地截短**（窗口内可逆、不改原文）。
- ``compress_summary_to_budget``：第二层 LLM 摘要返回后的**优先级预算压缩**（兜底约束）。

职责边界（与上下文治理器严格区分）：
- 本地截短 / 摘要预算都是**短期记忆对自己内容的管理**（内容层），不是调度层收窗；
- 治理器 ``pick_history`` 决定"窗口放多少条"，本模块只负责"被选中的老内容太长时怎么凝练"。
"""

from __future__ import annotations

import re

from agent.api import clip_text, estimate_tokens

# 五类优先级：低 → 高（压缩时从最低优先级裁起）
_PRIORITY_LOW_TO_HIGH = ["entity", "facts", "goal", "unresolved", "feedback"]
# 受保护区：绝不牺牲
_PROTECTED = frozenset({"feedback", "unresolved"})

# 摘要分节标题（中英文 + 编号都可解析）
_SECTION_SPLIT = re.compile(
    r"(?m)^(?:#{2,3}\s*|\s*(?:[1-5])[.、]\s*)"
    r".*(反馈|未决|待解决|目标|意图|事实|决策|实体|人名|项目|文件).*$"
)


def _looks_like_file(content: str) -> bool:
    """启发：多行且含代码/结构特征 → 视为文件内容（用更大阈值）。"""
    if content.count("\n") < 15:
        return False
    return any(k in content for k in
               ("import ", "def ", "class ", "function ", "public ", "private ",
                "package ", "module "))


def truncate_content(text: str, max_tokens: int) -> tuple[str, int]:
    """本地截短单条内容：保结构/首尾/关键行，返回 ``(新文本, 省略 token 数)``。

    - 未超阈值 → 原样；
    - 多行 → 保前 50% 行 + 后 20% 行（结构/关键行），中间省去并标注省略行数；
    - 单行大块（如工具结果 JSON）→ 退化为头尾字符保留（``clip_text``）。
    """
    total = estimate_tokens(text)
    if total <= max_tokens:
        return text, 0
    lines = text.split("\n")
    if len(lines) <= 1:
        return clip_text(text, max_tokens)        # 单行：头尾字符保留
    head_n = max(1, int(len(lines) * 0.5))
    tail_n = max(1, int(len(lines) * 0.2))
    mid_lines = lines[head_n: len(lines) - tail_n]
    omitted_lines = len(mid_lines)
    head = "\n".join(lines[:head_n])
    tail = "\n".join(lines[len(lines) - tail_n:])
    body = f"{head}\n[...省略 {omitted_lines} 行 / {estimate_tokens(chr(10).join(mid_lines))} tokens...]\n{tail}"
    return body, estimate_tokens(chr(10).join(mid_lines))


def truncate_messages(
    messages: list[dict],
    *,
    recent_rounds: int = 3,
    tool_threshold: int = 6000,
    file_threshold: int = 12000,
) -> list[dict]:
    """对消息列表做窗口内本地截短：**仅老内容**（距末尾 ≥ recent_rounds 轮）且超阈值才截。

    - 最近 ``recent_rounds * 2`` 条（≈ 3 轮）一律不截，保证进行中的推理有完整上下文；
    - 老消息：疑似文件 → ``file_threshold``，否则 ``tool_threshold``；超则 ``truncate_content``；
    - **不改原文**：返回的 content 是截短后视图，``agent_messages`` 原文由 store 独立保留。
    """
    n = len(messages)
    if n == 0:
        return []
    recent_keep = recent_rounds * 2
    out: list[dict] = []
    for i, m in enumerate(messages):
        content = m.get("content") or ""
        is_recent = i >= n - recent_keep
        if is_recent or not isinstance(content, str) or not content:
            out.append(dict(m))
            continue
        thr = file_threshold if _looks_like_file(content) else tool_threshold
        tk = m.get("token_count") or estimate_tokens(content)
        if tk <= thr:
            out.append(dict(m))
            continue
        new_content, _ = truncate_content(content, thr)
        copied = dict(m)
        copied["content"] = new_content
        out.append(copied)
    return out


# ---- 第二层：摘要优先级预算压缩（LLM 返回后的兜底约束） ----

_SECTION_KIND_MAP = {
    "反馈": "feedback", "纠正": "feedback", "修正": "feedback",
    "未决": "unresolved", "待解决": "unresolved", "问题": "unresolved",
    "目标": "goal", "意图": "goal", "目的": "goal",
    "事实": "facts", "决策": "facts", "确认": "facts",
    "实体": "entity", "人名": "entity", "项目": "entity", "文件": "entity",
}


def parse_sections(summary_text: str) -> dict[str, str]:
    """把摘要文本按五类标题切分为 ``{kind: 文本}``；无标题或散文本归为 ``misc``。"""
    if not summary_text:
        return {}
    lines = summary_text.split("\n")
    sections: dict[str, list[str]] = {}
    current = "misc"
    sections[current] = []
    for ln in lines:
        m = _SECTION_SPLIT.match(ln)
        if m:
            kw = m.group(1)
            current = _SECTION_KIND_MAP.get(kw, "misc")
            if current not in sections:
                sections[current] = []
            # 该标题行本身不计入正文
            continue
        sections[current].append(ln)
    return {k: "\n".join(v).strip() for k, v in sections.items() if v}


def _render(sections: dict[str, str]) -> str:
    """按固定顺序渲染（反馈→未决→目标→事实→实体），稳定且受保护区在前。"""
    order = ["feedback", "unresolved", "goal", "facts", "entity", "misc"]
    parts: list[str] = []
    for k in order:
        if k in sections and sections[k]:
            label = {
                "feedback": "反馈/纠正", "unresolved": "未决问题",
                "goal": "目标/意图", "facts": "事实/决策",
                "entity": "关键实体", "misc": "其他",
            }[k]
            parts.append(f"### {label}\n{sections[k]}")
    return "\n\n".join(parts)


def compress_summary_to_budget(
    summary_text: str,
    budgets: dict[str, int],
    total_soft: int,
) -> str:
    """把摘要按优先级 + 独立预算压缩到软上限。

    - 单类超自身预算 → 该类内部 clip（保头尾）；
    - 总 token 仍超软上限 → 从最低优先级（实体→事实→目标）依次裁到其独立预算（受保护区跳过）；
    - 反馈/未决受保护，绝不牺牲（即便总超也不裁）。
    """
    sections = parse_sections(summary_text)
    if not sections:
        # 无分节：整体当作可裁内容，clip 到软上限
        clipped, _ = clip_text(summary_text, total_soft)
        return clipped
    # 1) 单类超限 → 内部 clip
    for k, body in list(sections.items()):
        cap = budgets.get(k)
        if cap and estimate_tokens(body) > cap:
            clipped, _ = clip_text(body, cap)
            sections[k] = clipped
    # 2) 总超 → 从低优先级裁起
    total = sum(estimate_tokens(v) for v in sections.values())
    if total <= total_soft:
        return _render(sections)
    for k in _PRIORITY_LOW_TO_HIGH:           # entity → facts → goal → unresolved → feedback
        if k in _PROTECTED or k not in sections:
            continue
        cap = budgets.get(k, 0)
        clipped, _ = clip_text(sections[k], cap)
        sections[k] = clipped
        total = sum(estimate_tokens(v) for v in sections.values())
        if total <= total_soft:
            break
    return _render(sections)


__all__ = [
    "truncate_content", "truncate_messages",
    "parse_sections", "compress_summary_to_budget",
]
