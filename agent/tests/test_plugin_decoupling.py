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
        # run/ 纳入扫描（2026-09-26 审查 P1：run/chat.py 曾直连 agent_memories 漏检）
        if rel.parts[0] not in ("core", "sys", "plugins", "run") and rel.name != "api.py":
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

# 业务表 -> 归属插件（唯一 owner）。
# 注意：键必须是**真实表名**（tables.py 建表名）。曾误登记 `agent_memory`（实际表
# `agent_memories` 的子串）→ 子串断言空转、run 层直连漏检（2026-09-26 审查 P1）。
TABLE_OWNER = {
    "agent_messages": "plugins/short_term",
    "agent_session_summaries": "plugins/short_term",
    "agent_memories": "plugins/memory",
    "agent_memory_edges": "plugins/memory",
    "agent_entities": "plugins/memory",
    "agent_budget_log": "sys",   # owner = sys/budget（唯一写方；sys 层内互信，插件/run 不得直连）
}
# 内核审计脊柱：各层只读展示可接受（agent_events 落盘于库；agent_plugin_state 是
# **JSON 文件**不是表、agent_runs 已废弃——二者不得出现在本清单，见存在性精确断言）。
KERNEL_TABLES = {"agent_events"}
# 建表与迁移：集中登记点，正当
MIGRATION_FILES = {"tables.py", "storage.py"}

_SQL = re.compile(r"(?:FROM|INSERT\s+INTO|UPDATE|DELETE\s+FROM|JOIN)\s+(agent_\w+)", re.I)


def _sql_text(fp: Path) -> str:
    """提取文件全部字符串字面量（AST 级）拼接，供 SQL 归属比对。

    行级启发式会被跨行字符串拼接（SQL 换行书写，如 run/chat.py 的多行 SELECT）
    绕过——动词与表名不在同一物理行即漏检。AST 提取不受换行影响；docstring 亦为
    字符串常量，但比对只认「SQL 动词短语 + agent_ 表名」同现，纯叙述不触发。
    """
    try:
        tree = ast.parse(fp.read_text(encoding="utf-8"))
    except Exception:                                    # pragma: no cover
        return ""
    parts = [n.value for n in ast.walk(tree)
             if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    return "\n".join(parts)


def test_table_ownership_respected():
    bad: list[tuple[str, str, str]] = []
    for fp in _py_files():
        if fp.name in MIGRATION_FILES:
            continue
        who = _layer(fp)
        text = _sql_text(fp)
        if not text:
            continue
        for t in _SQL.findall(text):
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
    """归属声明必须与建表清单**精确**对应（防幻影表名 / 子串假阳性）。

    曾用子串断言 `t in tables_py`："agent_memory" 是 "agent_memories" 的子串 →
    幻影表名空转通过（2026-09-26 审查 P1 三层失守之一）。改为按 DDL 精确提取表名集合。
    """
    text = (AGENT / "tables.py").read_text(encoding="utf-8")
    declared = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", text))
    assert declared, "tables.py 未提取到任何建表语句（正则与 DDL 形状脱节）"
    for t in TABLE_OWNER:
        assert t in declared, f"{t} 未在 agent/tables.py 建表（幻影表名或拼写错误）"
