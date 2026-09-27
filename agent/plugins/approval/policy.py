"""plugins.approval.policy —— 三态判定（deny > ask > allow > default）。

纯函数、零依赖（仅 stdlib fnmatch）：输入规则表 + 工具名 + 模式 + 风险级，
输出 ``"allow" | "deny" | "ask"``。**ask 的来源只有两处**——本插件自身名单
（``approval.ask`` 工具名 fnmatch）与模式（auto/plan 的风险级规则）；
guard 的 ``permissions.risk.*="ask"`` 不在本判定内（那是无 approval 时的
fail-safe 兜底配置，guard 会先短路，两者不应同时使用——见设计稿 §0-3）。

判定顺序（设计稿 §10）：
    1. ``deny`` 名单命中 → "deny"（一票否决，优先于一切）
    2. 模式硬规则：``plan`` + 非只读 → "deny"（plan 硬兜底：软约束看不见，
       硬约束必须拒绝，防幻觉写工具越权）
    3. 模式软规则：``auto`` + 非只读 → "ask"（计划已批的自动挡，写类仍要过问）
    4. ``ask`` 名单命中 → "ask"
    5. ``allow`` 名单命中 → "allow"
    6. 未命中任何名单 → ``default``（**默认 "deny"**：fail-safe；
       出厂配置以 ``allow=["*"]`` 保持「默认全放行、行为与现状一致」）
"""

from __future__ import annotations

from fnmatch import fnmatchcase

MODES = ("default", "plan", "auto", "bypass")


def hit(patterns: list[str] | None, tool: str) -> bool:
    """工具名是否命中通配名单（fnmatch，大小写敏感——工具名是内部标识符）。"""
    return any(fnmatchcase(tool, str(p)) for p in (patterns or []))


def decide(rules: dict, tool: str, *, mode: str = "default", risk: str = "") -> str:
    """三态判定。``rules`` 为 ``config["agent"]["approval"]``（已合并默认值）。"""
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
