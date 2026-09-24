"""plugins.react · 提示词与上下文治理（系统提示 / 工具目录渲染 / 结果截断）。

- 两个标记（离线 Fixture 据此路由，保证确定性）：
  ``<!--react:turn-->`` 每轮推理 · ``<!--react:final-->`` 触顶收尾。
- 环境事实块：与 chat 的规划提示同源语义（机器权威 → 禁止 ``%VAR%`` 占位符）。
  在本包内自带一份实现，避免目录插件反向 import ``agent.sys.chat`` 的内部函数。
"""

from __future__ import annotations

import json
import os

TURN_MARK = "<!--react:turn-->"
FINAL_MARK = "<!--react:final-->"

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


def build_system(catalog: list[dict]) -> str:
    return f"{REACT_SYSTEM}\n\n{render_tools(catalog)}\n\n{env_facts()}"


def as_text(value) -> str:
    """工具结果 → str（str 原样；其它 JSON 化；不可序列化则 str()）。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def truncate(value, max_chars: int) -> str:
    """结果截断（多步循环的必备上下文治理）：超长即截断并**标注**截掉多少。"""
    s = as_text(value)
    if max_chars <= 0 or len(s) <= max_chars:
        return s
    return s[:max_chars] + f"…(truncated {len(s) - max_chars} chars)"


__all__ = ["TURN_MARK", "FINAL_MARK", "REACT_SYSTEM", "FINAL_SYSTEM",
           "env_facts", "render_tools", "build_system", "as_text", "truncate"]
