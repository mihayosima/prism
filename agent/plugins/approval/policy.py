"""三态判定：纯函数，输入规则表 + 工具名 + 模式 + 风险级，输出 ``"allow"|"deny"|"ask"``。"""

from __future__ import annotations

from fnmatch import fnmatchcase

MODES = ("default", "plan", "auto", "bypass")


def hit(patterns: list[str] | None, tool: str) -> bool:
    """工具名是否命中通配名单。"""
    return any(fnmatchcase(tool, str(p)) for p in (patterns or []))


def decide(rules: dict, tool: str, *, mode: str = "default", risk: str = "") -> str:
    """三态判定。``rules`` 为 ``config["agent"]["approval"]``。"""
    if hit(rules.get("deny"), tool):
        return "deny"
    if mode == "plan" and risk != "read":
        return "deny"                       # plan 硬兜底：写类/未标风险一律拒
    if (mode == "auto" and risk in ("write", "danger")) or hit(rules.get("ask"), tool):
        return "ask"
    if hit(rules.get("allow"), tool):
        return "allow"
    return str(rules.get("default", "deny"))


__all__ = ["MODES", "hit", "decide"]
