"""K1 契约测试（AST 静态守卫）—— 服务键常量化（值不变，零行为改动）。

与 P1 事件名（Events）、P2 配置键、P3 接线守卫同构：把「服务键」这一最后一类隐式契约显式化。
扫描**生产代码**（agent/core、agent/sys、agent/plugins、agent/run、agent/api、profiles），断言：

① ``provides=`` / ``inject=``（类级属性 + ``@plugin`` 装饰器参数）的字面量 ∈ Services ∪ ALLOWLIST；
② ``ctx.get/provide/has/set(...)`` 若首参字面量恰好是 11 个核心服务键之一，则必须用 ``Services`` 常量
   （其余字符串——插件扩展键如 ``react.reasoner``、config 段键、intent、测试键——不在本契约范围，放行）；
③ 每个 ``Services`` 常量都有提供方（防死常量）。

插件自管键（如 ``migrations``）不进 Services，由 ALLOWLIST 放行；内部键（以 ``_`` 开头）跳过。
"""

from __future__ import annotations

import ast
import pathlib

from agent.api import Services
from agent.core.services_catalog import ALL as SERVICE_KEYS

AGENT = pathlib.Path(__file__).resolve().parent.parent          # agent/
# 仅扫生产代码：tests 与 fixtures 故意使用任意键（如 test_kernel 用 'aaa' 测容器机制），不在契约内
_SCAN_ROOTS = [
    AGENT / "core",
    AGENT / "sys",
    AGENT / "plugins",
    AGENT / "run",
    AGENT / "api.py",
    AGENT / "profiles.py",
]
# 插件自管键（非内核服务，按需增删；不在 Services 内，仅此处放行）
ALLOWLIST = {"migrations"}
# 内核服务键值集合（契约范围）
SERVICE_VALUES = set(SERVICE_KEYS)
# context 类接收者：Name ∈ 这些，或 Attribute.attr 以 root 结尾 / 等于 ctx
_CONTEXT_NAMES = {"ctx", "self", "root", "c"}


def _py_files():
    out: list[pathlib.Path] = []
    for root in _SCAN_ROOTS:
        if root.is_file():
            if "__pycache__" not in root.parts:
                out.append(root)
        else:
            out.extend(sorted(root.rglob("*.py")))
    return [p for p in out if "__pycache__" not in p.parts]


def _receiver_is_context(node: ast.AST) -> bool:
    """判断调用接收者是否为 context 类（用于区分 ctx.get 与 config.get / dict.get）。"""
    if isinstance(node, ast.Name):
        return node.id in _CONTEXT_NAMES
    if isinstance(node, ast.Attribute):
        return node.attr.endswith("root") or node.attr == "ctx"
    return False


def _list_elements_bad(lst: ast.AST, allowed: set[str]) -> list[str]:
    """校验列表字面量元素：str 字面量必须 ∈ allowed；Services.<CONST> / 变量放行。返回不合格项。"""
    bad: list[str] = []
    if not isinstance(lst, (ast.List, ast.Tuple)):
        return bad
    for el in lst.elts:
        if isinstance(el, ast.Attribute) and isinstance(el.value, ast.Name) \
                and el.value.id == "Services":
            continue
        if isinstance(el, ast.Constant) and isinstance(el.value, str):
            if el.value not in allowed:
                bad.append(el.value)
    return bad


def _module_imports(path: pathlib.Path) -> set[str]:
    """收集单个模块的全部 import 目标（``ast.Import`` 的 name / ``ImportFrom`` 的 module）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_provides_inject_literals_belong_to_services():
    """① provides=/inject= 的字面量必须 ∈ Services ∪ ALLOWLIST。"""
    allowed = SERVICE_VALUES | ALLOWLIST
    bad: list[tuple[str, str, str]] = []
    for fp in _py_files():
        tree = ast.parse(fp.read_text(encoding="utf-8"), filename=str(fp))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                    and isinstance(node.targets[0], ast.Name) \
                    and node.targets[0].id in ("provides", "inject"):
                for k in _list_elements_bad(node.value, allowed):
                    bad.append((fp.name, node.targets[0].id, k))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "plugin":
                for kw in node.keywords:
                    if kw.arg in ("provides", "inject") and kw.value is not None:
                        for k in _list_elements_bad(kw.value, allowed):
                            bad.append((fp.name, f"@{kw.arg}", k))
    assert not bad, f"provides/inject 含非常量化服务键: {bad}"


def test_ctx_service_key_literals_use_constants():
    """② ctx.get/provide/has/set 若首参是核心服务键，则必须用 Services 常量（零行为改动前提下）。"""
    bad: list[tuple[str, str, str]] = []
    for fp in _py_files():
        tree = ast.parse(fp.read_text(encoding="utf-8"), filename=str(fp))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in (
                    "get", "provide", "has", "set"):
                continue
            if not _receiver_is_context(func.value):
                continue
            if not node.args:
                continue
            arg0 = node.args[0]
            # Services.<CONST> 形式放行
            if isinstance(arg0, ast.Attribute) and isinstance(arg0.value, ast.Name) \
                    and arg0.value.id == "Services":
                continue
            # 仅当字面量恰好是核心服务键时才要求用常量；其余（扩展键/config/intent/test）放行
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                if arg0.value in SERVICE_VALUES:
                    bad.append((fp.name, func.attr, arg0.value))
    assert not bad, f"ctx.{ 'get/provide/has/set' } 用裸串而非 Services 常量: {bad}"


def test_every_service_constant_has_provider():
    """③ 每个 Services 常量都有提供方（防死常量）。"""
    provided: set[str] = set()
    for fp in _py_files():
        tree = ast.parse(fp.read_text(encoding="utf-8"), filename=str(fp))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "provide":
                continue
            if not _receiver_is_context(func.value):
                continue
            if not node.args:
                continue
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                provided.add(arg0.value)
            elif isinstance(arg0, ast.Attribute) and isinstance(arg0.value, ast.Name) \
                    and arg0.value.id == "Services":
                provided.add(getattr(Services, arg0.attr, None))
    missing = [name for name in dir(Services)
               if not name.startswith("_")
               and isinstance(getattr(Services, name), str)
               and getattr(Services, name) not in provided]
    assert not missing, f"以下 Services 常量无提供方（死常量）: {missing}"


# ---- 技能 MCP 适配器（只读对外暴露）零耦合守卫 ----

_ADAPTER = AGENT / "run" / "skills_mcp_adapter.py"
_SKILLS_MCP_BRIDGE = AGENT / "plugins" / "skills_mcp" / "__init__.py"


def test_skills_mcp_adapter_import_purity():
    """④ 只读技能 MCP 适配器：不得依赖工具表 / 反应器子系统（零耦合）。

    适配器是**独立子进程**，只应从磁盘重建技能目录；一旦 import ``sys.tasks`` 或
    ``plugins.react``，就会把技能与工具两个子系统焊死——正是当初绕开
    ``register_meta_tool``、改走 ``Services.SKILLS`` 反向读取要杜绝的事。
    """
    if not _ADAPTER.exists():
        return
    banned_prefixes = ("agent.sys.tasks", "agent.plugins.react")
    mods = _module_imports(_ADAPTER)
    bad = sorted(m for m in mods if m.startswith(banned_prefixes))
    assert not bad, f"技能 MCP 适配器不得 import 工具/reactor 子系统: {bad}"


def test_skills_mcp_bridge_import_purity():
    """⑤ 技能 MCP 桥插件：只经 ``agent.api`` 接线，不得直连技能/工具/反应器的实现模块。

    桥的职责是「起子进程 + 注册工具」，与 ``playwright_mcp`` / ``windows_mcp`` 同形态；
    它不该知道技能目录怎么建、工具表怎么管。
    """
    if not _SKILLS_MCP_BRIDGE.exists():
        return
    # 技能目录已下沉到 core，故禁的是「绕过 api 直连 core」：桥应只 import agent.api。
    banned_prefixes = ("agent.sys.tasks", "agent.core.skills",
                       "agent.plugins.react", "agent.plugins.skills")
    mods = _module_imports(_SKILLS_MCP_BRIDGE)
    # 排除自身包路径（agent.plugins.skills_mcp 不是 agent.plugins.skills）
    bad = sorted(m for m in mods
                 if m.startswith(banned_prefixes) and not m.startswith("agent.plugins.skills_mcp"))
    assert not bad, f"技能 MCP 桥插件不得直连这些子系统: {bad}"


def test_skills_mcp_service_key_registered():
    """⑥ ``Services.SKILLS_MCP`` 必须登记进 ``ALL``（漏登记 ⇒ 契约守卫与装配期静默失效）。"""
    assert Services.SKILLS_MCP in SERVICE_KEYS, \
        "Services.SKILLS_MCP 未登记进 services_catalog.ALL"


__all__ = [
    "test_provides_inject_literals_belong_to_services",
    "test_ctx_service_key_literals_use_constants",
    "test_every_service_constant_has_provider",
    "test_skills_mcp_adapter_import_purity",
    "test_skills_mcp_bridge_import_purity",
    "test_skills_mcp_service_key_registered",
]
