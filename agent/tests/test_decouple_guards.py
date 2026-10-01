"""D3/D4 · SSOT 字面量守卫（AST 静态扫描生产代码）。

- StateKeys 值字面量只允许出现在 core/state_keys.py（ctx.state 共享键单一真相源）；
- `<!--` 标记字面量只允许出现在 agent/api.py（提示词标记单一真相源）；
- 生产代码禁止 `from agent.sys.audit`（审计公共面已转正 agent.api / core/audit）。

与 test_services_contract（K1）同风格：静态扫描、零运行期开销、违例即红。
"""

from __future__ import annotations

import ast
import pathlib

AGENT = pathlib.Path(__file__).resolve().parent.parent

_SCAN_ROOTS = [
    AGENT / "core",
    AGENT / "sys",
    AGENT / "plugins",
    AGENT / "run",
    AGENT / "fixtures",
    AGENT / "api.py",
    AGENT / "profiles.py",
]

from agent.api import StateKeys

# 由 StateKeys **反射生成**，不再手写常量清单。
# 此前手写 5 个值、漏掉 budget_warning / llm_cost_ratio / react_run_id ——
# 新增共享键时守卫会静默失效（写裸字面量不报错），这是最危险的失效模式。
# 模板键（``{bridge}_tools`` / ``{bridge}_error``）含 ``{``，无法字面量比对，合理豁免。
_STATE_KEYS = {
    v for k, v in vars(StateKeys).items()
    if not k.startswith("_") and isinstance(v, str) and "{" not in v
}
_STATE_KEYS_SSOT = AGENT / "core" / "state_keys.py"
_MARKER_SSOT = AGENT / "api.py"


def _py_files():
    out: list[pathlib.Path] = []
    for root in _SCAN_ROOTS:
        if root.is_file():
            out.append(root)
        else:
            out.extend(sorted(root.rglob("*.py")))
    return [p for p in out if "__pycache__" not in p.parts]


def _string_constants(tree: ast.AST) -> list[str]:
    """字符串常量；**豁免 dict 字面量的键**（status() 返回体的字段名属 API 出参，非 state 键）。"""
    dict_keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for k in node.keys:
                if isinstance(k, ast.Constant) and isinstance(k.value, str):
                    dict_keys.add(k.value)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in dict_keys]


def test_state_key_literals_only_in_state_keys():
    bad: list[tuple[str, str]] = []
    for fp in _py_files():
        if fp.resolve() == _STATE_KEYS_SSOT.resolve():
            continue
        tree = ast.parse(fp.read_text(encoding="utf-8"), filename=str(fp))
        for s in _string_constants(tree):
            if s in _STATE_KEYS:
                bad.append((fp.name, s))
    assert not bad, f"ctx.state 共享键裸字面量（应改用 agent.api.StateKeys）: {bad}"


def test_marker_literals_only_in_api():
    bad: list[tuple[str, str]] = []
    for fp in _py_files():
        if fp.resolve() == _MARKER_SSOT.resolve():
            continue
        tree = ast.parse(fp.read_text(encoding="utf-8"), filename=str(fp))
        for s in _string_constants(tree):
            if "<!--" in s:
                bad.append((fp.name, s[:40]))
    assert not bad, f"提示词标记字面量（应引用 agent.api 的 MARK_* 常量）: {bad}"


def test_no_direct_sys_audit_imports():
    """审计公共面已转正（agent.api / core/audit），生产代码不得再 import agent.sys.audit。"""
    bad: list[str] = []
    for fp in _py_files():
        tree = ast.parse(fp.read_text(encoding="utf-8"), filename=str(fp))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "agent.sys.audit":
                bad.append(fp.name)
            if isinstance(node, ast.Import):
                if any(a.name == "agent.sys.audit" for a in node.names):
                    bad.append(fp.name)
    assert not bad, f"生产代码仍直接 import agent.sys.audit: {bad}"
