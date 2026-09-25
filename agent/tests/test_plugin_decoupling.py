"""插件解耦守卫 —— 分层依赖方向 · 装配耦合 · 数据表归属。

一次性审计容易做完就忘，故固化为守卫：任何一次改动若重新引入下列耦合，测试即红。

三条红线：
1. **分层方向**：内核（core/api）不得反向依赖 sys/plugins；sys 不得依赖业务插件；
   插件之间**零横向 import**；插件不得依赖 sys 实现；插件不得绕过 ``agent.api``
   直连 core 内部。
2. **装配耦合**：``inject`` 只允许声明**内核服务**。若声明了由另一插件提供的服务，
   该插件未启用时容器直接 ``MissingDependencyError`` —— opt-in 语义就被破坏了。
3. **数据表归属**：每张业务表只有一个 owner 插件，非归属方不得直连读写
   （建表/迁移文件豁免；内核审计脊柱表只读豁免）。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from agent.api import Services

AGENT = Path(__file__).resolve().parent.parent          # agent/


# ---------------------------------------------------------------------------
# 扫描工具
# ---------------------------------------------------------------------------

def _py_files() -> list[Path]:
    out = []
    for fp in AGENT.rglob("*.py"):
        parts = fp.parts
        if "__pycache__" in parts or "tests" in parts:
            continue
        rel = fp.relative_to(AGENT)
        if rel.parts[0] not in ("core", "sys", "plugins") and rel.name != "api.py":
            continue
        out.append(fp)
    return sorted(out)


def _layer(fp: Path) -> str:
    rel = fp.relative_to(AGENT)
    if rel.parts[0] == "core" or rel.name == "api.py":
        return "core"
    if rel.parts[0] == "sys":
        return "sys"
    if rel.parts[0] == "plugins":
        return f"plugins/{rel.parts[1]}"
    return rel.parts[0]


def _imported_modules(fp: Path) -> list[str]:
    try:
        tree = ast.parse(fp.read_text(encoding="utf-8"))
    except Exception:                                    # pragma: no cover
        return []
    mods: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and not n.level and n.module:
            mods.append(n.module)
    return mods


# ---------------------------------------------------------------------------
# 1) 分层依赖方向
# ---------------------------------------------------------------------------

def test_layered_import_direction():
    bad: list[tuple[str, str, str]] = []
    for fp in _py_files():
        src = _layer(fp)
        for m in _imported_modules(fp):
            if not m.startswith("agent."):
                continue
            t = m[len("agent."):].split(".")
            if t[0] == "core":
                tgt = "core"
            elif t[0] == "sys":
                tgt = "sys"
            elif t[0] == "plugins":
                tgt = f"plugins/{t[1]}" if len(t) > 1 else "plugins/*"
            else:
                continue
            if tgt == src:
                continue
            if src == "core" and tgt in ("sys", "plugins/*") or (src == "core" and tgt.startswith("plugins/")):
                bad.append((str(fp.relative_to(AGENT)), m, "内核反向依赖 sys/plugins"))
            if src == "sys" and tgt.startswith("plugins/"):
                bad.append((str(fp.relative_to(AGENT)), m, "系统层依赖业务插件"))
            if src.startswith("plugins/") and tgt.startswith("plugins/") and tgt != src:
                bad.append((str(fp.relative_to(AGENT)), m, "插件横向耦合"))
            if src.startswith("plugins/") and tgt == "sys":
                bad.append((str(fp.relative_to(AGENT)), m, "插件依赖 sys 实现"))
            if src.startswith("plugins/") and tgt == "core":
                bad.append((str(fp.relative_to(AGENT)), m, "插件绕过 api 直连 core"))
    assert not bad, "分层依赖方向违规:\n" + "\n".join(f"  {f} -> {m}  ({why})" for f, m, why in bad)


# ---------------------------------------------------------------------------
# 2) inject 装配耦合
# ---------------------------------------------------------------------------

def _plugin_specs() -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """(provides_map: 服务值 -> [provider], inject_map: 插件名 -> [服务值])。"""
    provides: dict[str, list[str]] = {}
    inject: dict[str, list[str]] = {}

    def _vals(node: ast.AST) -> list[str]:
        out = []
        for e in getattr(node, "elts", []):
            if isinstance(e, ast.Attribute):
                out.append(getattr(Services, e.attr, e.attr))
            elif isinstance(e, ast.Constant) and isinstance(e.value, str):
                out.append(e.value)
        return out

    for fp in _py_files():
        if _layer(fp) == "core":
            continue
        try:
            tree = ast.parse(fp.read_text(encoding="utf-8"))
        except Exception:                                # pragma: no cover
            continue
        for n in ast.walk(tree):
            if not isinstance(n, ast.ClassDef):
                continue
            name, prov, inj = None, [], []
            for st in n.body:
                if not (isinstance(st, ast.Assign) and len(st.targets) == 1
                        and isinstance(st.targets[0], ast.Name)):
                    continue
                key = st.targets[0].id
                if key == "name" and isinstance(st.value, ast.Constant):
                    name = st.value.value
                elif key == "provides" and isinstance(st.value, (ast.List, ast.Tuple)):
                    prov = _vals(st.value)
                elif key == "inject" and isinstance(st.value, (ast.List, ast.Tuple)):
                    inj = _vals(st.value)
            if not name:
                continue
            for s in prov:
                provides.setdefault(s, []).append(name)
            inject[name] = inj
    return provides, inject


def test_inject_never_couples_two_plugins():
    """内核服务由 SYS_PLUGINS 保证常驻，可声明；插件提供的服务一律走软依赖。"""
    from agent.profiles import SYS_PLUGINS

    kernel = set()
    for p in SYS_PLUGINS:
        kernel.update(getattr(p, "provides", []) or [])

    provides, inject = _plugin_specs()
    bad = []
    for plug, deps in sorted(inject.items()):
        for svc in deps:
            if svc in kernel:
                continue
            owners = [o for o in provides.get(svc, []) if o != plug]
            if owners:
                bad.append((plug, svc, owners))
    assert not bad, (
        "inject 声明了由其它插件提供的服务（未启用即装配失败，破坏 opt-in）:\n"
        + "\n".join(f"  {p} inject {s} ← 必须由 {o} 提供" for p, s, o in bad))


# ---------------------------------------------------------------------------
# 3) 数据表归属
# ---------------------------------------------------------------------------

# 业务表 -> 归属插件（唯一 owner）
TABLE_OWNER = {
    "agent_messages": "plugins/short_term",
    "agent_session_summaries": "plugins/short_term",
    "agent_memory": "plugins/memory",
    "agent_memory_edges": "plugins/memory",
    "agent_entities": "plugins/memory",
}
# 内核表：审计脊柱 / 运行状态 / 插件状态，各层只读可接受
KERNEL_TABLES = {"agent_events", "agent_runs", "agent_plugin_state"}
# 建表与迁移：集中登记点，正当
MIGRATION_FILES = {"tables.py", "storage.py"}

_SQL = re.compile(r"(?:FROM|INSERT\s+INTO|UPDATE|DELETE\s+FROM|JOIN)\s+(agent_\w+)", re.I)


def test_table_ownership_respected():
    bad: list[tuple[str, str, str]] = []
    for fp in _py_files():
        if fp.name in MIGRATION_FILES:
            continue
        who = _layer(fp)
        try:
            lines = fp.read_text(encoding="utf-8").splitlines()
        except Exception:                                # pragma: no cover
            continue
        for ln in lines:
            s = ln.strip()
            if s.startswith("#") or s.startswith('"""') or s.startswith("'''"):
                continue                                  # 注释/docstring 里的表名不算访问
            if not re.search(r"SELECT|INSERT|UPDATE|DELETE", ln, re.I):
                continue
            for t in _SQL.findall(ln):
                owner = TABLE_OWNER.get(t)
                if owner is None:
                    continue
                if t in KERNEL_TABLES:
                    continue
                if who == owner:
                    continue
                bad.append((str(fp.relative_to(AGENT)), t, owner))
    assert not bad, (
        "数据表被非归属方直连（数据层耦合，应经 owner 插件的接口）:\n"
        + "\n".join(f"  {f} 直连 {t}（归 {o}）" for f, t, o in bad))


def test_every_owned_table_exists_in_migrations():
    """归属声明必须与建表清单对得上（防止 owner 表写错名字 / 表被删后残留声明）。"""
    tables_py = (AGENT / "tables.py").read_text(encoding="utf-8")
    for t in TABLE_OWNER:
        assert t in tables_py, f"{t} 未出现在 agent/tables.py 建表清单中"
