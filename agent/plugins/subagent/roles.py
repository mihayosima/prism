"""子 Agent 角色定义（MVP：内置三角色；YAML 扩展点预留，阶段 3 启用）。

设计稿 v0.3 §3.4：角色是**安全下界**——``tools`` 白名单语义为「交集」，
只能比父可见集更窄（审查意见 #2 / #R3：role.tools 是安全下界，父权限裁剪仍生效）。

MVP 不读 YAML（扩展点预留），只内置 ``default`` / ``explorer`` / ``worker`` 三档；
三者差异化以 ``system_note``（注入到子任务提示）体现，真正的工具白名单收窄
（列具体工具名/域）在阶段 3 经 ``agent.react.subagents`` YAML 表达。
"""

from __future__ import annotations

from typing import Iterable

# 内置角色。``tools`` 为 ``None`` 时表示「不额外收窄」（仍受父权限裁剪）；
# 为具体名单时表示在该角色下**只允许**这些工具名/域（与父可见集取交集）。
BUILTIN_ROLES: dict[str, dict] = {
    "default": {
        "description": "通用子 Agent：继承父的全部可见工具（不额外收窄）；适合任何独立子任务。",
        "tools": None,
        "system_note": "",
    },
    "explorer": {
        "description": (
            "调研型子 Agent：面向检索/读取场景。MVP 仅以提示约束其只读倾向；"
            "真正的只读硬约束（仅放行 risk=read 工具）由父会话 plan 档位或"
            "阶段 3 角色白名单落实，本档不擅自放宽。"
        ),
        "tools": None,
        "system_note": (
            "你是调研子 Agent：只做检索与读取，归纳事实、给出依据；"
            "不要执行任何写操作，也不要臆造来源。"
        ),
    },
    "worker": {
        "description": (
            "执行/审查型子 Agent：在 default 基础上可承载写操作，并对结果做自检。"
        ),
        "tools": None,
        "system_note": (
            "你是执行子 Agent：按指令完成任务，完成后对结果做一遍自检"
            "（正确性/边界/明显错误），在答复中说明自检结论。"
        ),
    },
}


class Role:
    """解析后的角色（不可变视图）。"""

    __slots__ = ("name", "description", "tools", "system_note")

    def __init__(self, name: str, description: str, tools, system_note: str) -> None:
        self.name = name
        self.description = description
        self.tools = tools            # list[str] | None
        self.system_note = system_note


def get_role(name: str) -> Role:
    """按名取角色；未知名回落 ``default``（诚实降级，不抛异常，避免父循环崩溃）。"""
    name = (name or "default").strip() or "default"
    spec = BUILTIN_ROLES.get(name)
    if spec is None:
        spec = BUILTIN_ROLES["default"]
        name = "default"
    return Role(name=name, description=spec.get("description", ""),
                tools=spec.get("tools"), system_note=spec.get("system_note", "") or "")


def list_roles() -> list[str]:
    """已注册角色名（目录 / 调试用）。"""
    return sorted(BUILTIN_ROLES)


def combine_tools(role_tools: Iterable[str] | None,
                  param_tools: Iterable[str] | None) -> list[str] | None:
    """角色白名单与请求白名单取**交集**（纯函数，便于单测）。

    语义（v0.3 §3.4 / §3.2）：两层任一都只能再收窄，不能放宽。
    - 任一层为 ``None``/空 ⇒ 不收窄该层（返回另一层；两层皆空 ⇒ ``None``）；
    - 含 ``"*"`` 的层 ⇒ 不额外收窄（返回另一层）；
    - 两层皆为具体名单 ⇒ 取**同时出现在两层**者（交集）；
    - 两层具体名单无交集 ⇒ 抛 ``ValueError``（配置错误，交由调用方转 AgentError，
      绝不让「无交集」退化成「全放行」——``_apply_whitelist`` 对空列表的语义是「不收窄」）。

    返回 ``None`` 表示「不收窄」，交由工厂再做父权限裁剪；返回具体列表即收窄结果。
    """
    rt = list(role_tools or [])
    pt = list(param_tools or [])
    if not rt:
        return pt or None
    if not pt:
        return rt or None
    sa, sb = set(rt), set(pt)
    if "*" in sa:
        return pt or None
    if "*" in sb:
        return rt or None
    common = [x for x in rt if x in sb]
    if not common:
        raise ValueError(
            f"角色工具白名单 {sorted(rt)} 与请求工具 {sorted(pt)} 无交集")
    return common
