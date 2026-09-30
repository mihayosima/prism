"""plugins.react · 提示词与上下文治理（系统提示 / 工具目录渲染 / 结果截断）。

- 两个标记（离线 Fixture 据此路由，保证确定性）：react:turn 每轮推理 ·
  react:final 触顶收尾。常量值以 ``agent.api`` 的 ``MARK_REACT_TURN / MARK_REACT_FINAL``
  为单一真相源（D4）。
- 环境事实块：与 chat 的规划提示同源语义（机器权威 → 禁止 ``%VAR%`` 占位符）。
  在本包内自带一份实现，避免目录插件反向 import ``agent.sys.chat`` 的内部函数。
"""

from __future__ import annotations

import json
import os
from typing import Mapping

from agent.api import MARK_REACT_FINAL, MARK_REACT_TURN

TURN_MARK = MARK_REACT_TURN
FINAL_MARK = MARK_REACT_FINAL

REACT_SYSTEM = (
    TURN_MARK + "\n"
    "你是能多步使用工具的 Agent（ReAct 循环）。每一轮只做一件事：\n"
    "1) 判断已有信息是否足以回答用户：若足够 → 直接输出最终答复，且**不要**调用任何工具；\n"
    "2) 若不足 → 调用「可用工具」里的工具获取信息或执行动作（一次可调用多个）。\n"
    "纪律：\n"
    "- 只能调用下方列出的工具；参数必须符合声明的类型与必填项；\n"
    "- 工具会返回结果或错误，请据此调整下一步（参数写错就改正后重试，不要重复同一个错误调用）；\n"
    "- 不得编造工具未返回的信息；无法完成时如实说明缺什么。"
)

FINAL_SYSTEM = (
    FINAL_MARK + "\n"
    "你已到达本轮的执行上限（步数/时延/预算）。请**只**依据上面的对话与工具观察结果，"
    "给用户一段简洁诚实的答复：说明已完成什么、还缺什么、为何停止。不要调用工具，不要编造。"
)

# D1：预算预警提示（budget 置 BUDGET_WARNING 旗标后，react 每步窗口注入一次；零直连）
BUDGET_TIGHT_SYSTEM = (
    "[预算提示] token 预算即将耗尽（已越过预警阈值）。请立即收束：\n"
    "- 不再发起大参数工具调用（如整段长文写入），改用最小必要操作；\n"
    "- 尽快基于已有观察输出最终答复；确有未竟事项，如实列出，不要硬撑。"
)


def env_facts(environ: dict | None = None) -> str:
    """运行环境事实块（纯函数，可注入 environ 便于离线快照测试）。"""
    environ = os.environ if environ is None else environ
    home = environ.get("USERPROFILE") or environ.get("HOME") or ""
    desktop = os.path.join(home, "Desktop") if home else ""
    return (
        "运行环境（构造路径时必须使用下列真实值，禁止输出 %VAR% 或 ~ 占位符）:\n"
        f"- 用户名: {environ.get('USERNAME') or environ.get('USER') or ''}\n"
        f"- 用户主目录: {home}\n"
        f"- 桌面: {desktop}"
    )


def render_params(params: Mapping[str, dict] | None, *, max_enum: int = 8) -> str:
    """参数摘要：``名:类型``；枚举展开成 ``{a|b|…}``（``tool_search`` 目录卡用）。

    S6/P2 的检索侧配套：域裁剪下 ``tool_search`` 是隐藏域**唯一**的入口，只给参数名
    会让模型对着 ``mode`` 这类枚举瞎猜（实测猜成 ``action``，实际要 ``mode`` 且取值
    只能是 ``read|write|copy|move|delete|list|search|info``）。此处把类型与取值一并
    给出，仅在**按需检索**时付出少量 token，基础提示词不受影响。
    """
    if not params:
        return "-"
    out: list[str] = []
    for name, spec in params.items():
        spec = spec or {}
        anyof = spec.get("anyOf")
        if isinstance(anyof, list) and anyof:
            type_txt = "|".join(str(x) for x in anyof)
        else:
            type_txt = str(spec.get("type") or "object")
        seg = f"{name}:{type_txt}"
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            vals = [str(v) for v in enum[:max_enum]]
            if len(enum) > max_enum:
                vals.append("…")
            seg += "{" + "|".join(vals) + "}"
        out.append(seg)
    return ",".join(out)


def render_tools(catalog: list[dict]) -> str:
    """工具目录 → 提示词文本（截断描述，控制提示词体积）。"""
    if not catalog:
        return "可用工具：（无）"
    lines = ["可用工具（仅可调用下列之一/多个）:"]
    for t in catalog:
        req = ",".join(t.get("required") or []) or "-"
        params = ",".join((t.get("params") or {}).keys()) or "-"
        desc = (t.get("description") or "")[:80]
        lines.append(f"- {t.get('name')}（{t.get('risk', 'write')}）：{desc}；"
                     f"参数 {params}；必填 {req}")
    return "\n".join(lines)


def render_domains(hidden: dict[str, int] | None) -> str:
    """被隐藏域的「存在性线索」（纯函数，无隐藏域返回空串）。

    域级裁剪（路线 2）的**强制配套项**：域对模型不可见时，若提示层连「外面还有
    哪些域、各有多少工具」都不提，模型就不会去 ``tool_search`` 检索——「隐藏」
    会退化成「不可得」。此处只给「域 + 数量」，**不给关键词**（关键词仍是模型的
    检索自由，命中率由灰度期观察）。

    无隐藏域 ⇒ 返回 ``""``，调用方据此跳过整块 ⇒ **默认态系统提示逐字节不变**。
    """
    if not hidden:
        return ""
    lines = ["其它工具域（当前未展开，可用 tool_search 检索）:"]
    for d, n in sorted(hidden.items()):
        lines.append(f"- {d or '（未归属）'}（{n} 个）")
    return "\n".join(lines)


def build_system(catalog: list[dict], system_memory: str = "",
                 hidden_domains: dict[str, int] | None = None) -> str:
    # 系统记忆：静态底座（声明层），前置注入；动态块（工具/环境）按原模式裁剪追加
    base = (system_memory + "\n\n" + REACT_SYSTEM) if system_memory else REACT_SYSTEM
    parts = [base, render_tools(catalog)]
    domains = render_domains(hidden_domains)
    if domains:
        parts.append(domains)          # 无隐藏域 ⇒ 不插入（默认输出逐字节不变）
    parts.append(env_facts())
    return "\n\n".join(parts)


def as_text(value) -> str:
    """工具结果 → str（str 原样；其它 JSON 化；不可序列化则 str()）。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


# 注：旧 ``truncate(value, max_chars)``（字符口径 + ``…(truncated N chars)`` 标注）已随
# S4 删除 —— 结果体积上限统一归**源头护栏**（``core.tokens.guard_source``，token 口径），
# Actor 只做 ``as_text``。旧字样由 ``tests/test_source_guard.py`` 静态守卫禁回。


__all__ = ["TURN_MARK", "FINAL_MARK", "REACT_SYSTEM", "FINAL_SYSTEM",
           "BUDGET_TIGHT_SYSTEM",
           "env_facts", "render_tools", "render_domains", "build_system", "as_text"]
