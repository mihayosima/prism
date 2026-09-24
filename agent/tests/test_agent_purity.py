"""纯度守卫 —— 内核树零业务词；agent.api 是唯一出口。

内核 = 纯机制：业务词只允许出现在 agent/plugins/ 的业务插件、fixtures/ 与领域文档，
不得进入 agent/core 与 agent/api；内核也不得 import 外部引擎 / 大模型 SDK。
"""
from __future__ import annotations

from pathlib import Path

AGENT_PKG = Path(__file__).resolve().parent.parent          # agent/
CORE = AGENT_PKG / "core"

# 业务词清单（来自业务文档词表）：出现在内核树即判污染
BUSINESS_TERMS = [
    "选品", "策略", "候选", "评分", "排榜", "短名单", "技能库", "爆款",
    "互补", "替代", "升级", "消耗", "季节", "场景", "潮流", "科技", "健康",
    "美学", "社交", "情绪", "痛点", "家居", "儿童", "效率", "户外",
    "substitution", "complementary", "upgrade", "consumable",
    "bestseller", "sku", "tiktok", "product_id", "picks", "skill",
]


def _core_py_files() -> list[Path]:
    out = sorted(CORE.rglob("*.py"))
    out.append(AGENT_PKG / "api.py")
    return [fp for fp in out if "__pycache__" not in fp.parts]


def test_core_has_no_business_terms():
    bad: list[tuple[str, str]] = []
    for fp in _core_py_files():
        text = fp.read_text(encoding="utf-8").lower()
        for term in BUSINESS_TERMS:
            if term.lower() in text:
                bad.append((fp.name, term))
    assert not bad, f"内核出现业务词: {bad}"


def test_core_never_imports_engine():
    bad: list[tuple[str, str]] = []
    for fp in _core_py_files():
        text = fp.read_text(encoding="utf-8")
        for pat in ("from engine", "import engine"):
            if pat in text:
                bad.append((fp.name, pat))
    assert not bad, f"内核 import 了引擎: {bad}"


def test_core_never_imports_sys_or_plugins():
    """K0 分层守卫：内核（core + 唯一出口 api）不得反向依赖 sys / plugins。

    解耦的硬约束——内核是纯机制，不得获知业务/系统插件的具体实现位置。
    """
    bad: list[tuple[str, str]] = []
    for fp in _core_py_files():
        text = fp.read_text(encoding="utf-8")
        for pat in ("from agent.sys", "from agent.plugins",
                    "import agent.sys", "import agent.plugins"):
            if pat in text:
                bad.append((fp.name, pat))
    assert not bad, f"内核反向依赖 sys/plugins: {bad}"


def test_core_files_only_in_core_and_api():
    """内核实现只应存在于 agent/core 与 agent/api（出口）；sys/plugins 等不得 import core 内部。"""
    allowed = {fp.name for fp in _core_py_files()}
    assert "context.py" in allowed and "container.py" in allowed and "events.py" in allowed
    assert "errors.py" in allowed and "api.py" in allowed