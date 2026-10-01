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
        # profiles.py 纳入扫描（2026-10-01 复核：装配根曾直连 core.config_schema 且完全不可见）
        if (rel.parts[0] not in ("core", "sys", "plugins", "run")
                and rel.name not in ("api.py", "profiles.py")):
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
    if rel.name == "profiles.py":
        # 装配根：唯一允许 import sys 插件清单（SYS_PLUGINS / BUSINESS_PLUGINS）的地方。
        return "assembly"
    return "run"


def _package_parts(fp: Path) -> tuple[str, ...]:
    """文件所在包的模块名分段（``__init__.py`` 自身即包），供相对导入反解。

    ``agent/plugins/react/loop.py``      -> ("agent", "plugins", "react")
    ``agent/plugins/react/__init__.py``  -> ("agent", "plugins", "react")
    ``agent/api.py``                     -> ("agent",)
    """
    rel = fp.relative_to(AGENT.parent)
    return tuple(rel.parts[:-1])


def _resolve_relative(fp: Path, level: int, module: str | None) -> str | None:
    """把 ``from ..X import Y`` 反解成绝对模块名（AST 不直接给）。

    扫描器此前用 ``not n.level`` 把全部相对导入丢弃 ⇒ 插件写一句
    ``from ..other import X`` 即可横向耦合且零告警。目录插件以包形式加载
    （``agent_plugins.<pkg>`` 带 ``__path__``），这种写法语法上完全合法。
    """
    pkg = _package_parts(fp)
    base = pkg[: len(pkg) - (level - 1)] if level - 1 <= len(pkg) else ()
    parts = list(base) + (module.split(".") if module else [])
    return ".".join(parts) or None


def _imported_modules(fp: Path) -> list[str]:
    try:
        tree = ast.parse(fp.read_text(encoding="utf-8"))
    except Exception:                                    # pragma: no cover
        return []
    mods: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            if n.level:
                resolved = _resolve_relative(fp, n.level, n.module)
                if resolved:
                    mods.append(resolved)
            elif n.module:
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
            if src == "sys" and tgt == "core" and fp.name not in SYS_TO_CORE_ALLOWED_FILES:
                # 2026-10-01 复核补齐：此前只剩「插件 → core」一条，sys 层直连 core
                # 无人看守（budget/chat/llm 三处即由此长期逃逸）。
                bad.append((str(fp.relative_to(AGENT)), m, "系统层绕过 api 直连 core"))
            if src in ("run", "assembly") and tgt == "core":
                bad.append((str(fp.relative_to(AGENT)), m, "装配/传输层绕过 api 直连 core"))
            if src in ("run", "assembly") and tgt.startswith("plugins/"):
                bad.append((str(fp.relative_to(AGENT)), m, "装配/传输层依赖业务插件实现"))
            # 注：run → sys 由 test_run_layer_sys_imports_are_declared 单独登记（有已知缺口），
            #     assembly → sys 放行（装配清单本就要 import 插件类）。
    assert not bad, "分层依赖方向违规:\n" + "\n".join(f"  {f} -> {m}  ({why})" for f, m, why in bad)


# ---------------------------------------------------------------------------
# 1b) 传输层 → sys 的已知缺口登记（防静默扩张）
# ---------------------------------------------------------------------------

# sys → core 直连的**唯一豁免**：``agent/sys/audit.py`` 是 D4 把审计公共面迁到
# ``core/audit`` 后留下的兼容 shim，它自身必须 import core 才能转发。
# 该文件已无人 import（``test_decouple_guards.test_no_direct_sys_audit_imports`` 明令禁止），
# 删掉它即可摘掉这条豁免 —— **不要往里加第二个名字**。
SYS_TO_CORE_ALLOWED_FILES = {"audit.py"}

# 传输层（run/）→ sys 的**已知缺口白名单**。两条都是 sys 实现、且无法经 api 导出
# （api 受 K0 红线约束不得反向依赖 sys），故维持直连 —— 但登记在案：
# 新增任何其它 sys 直连都必须先评审再补进本表，不允许静默扩张。
#   - agent.sys.progress      ：传输桥接（tap_progress）。只依赖 agent.api（Events + ctx.scope），
#                               本质是内核原语的封装，但实现位于 sys 层。
#   - agent.sys.plugin_manager：插件启停/装卸管理（HTTP「插件」页端点）。操作对象就是
#                               sys 层的管理器本身，无内核等价物。
# 注：本白名单是在补上 run 层红线后才被清点出来的 —— 此前 run 层没有任何规则，
#     这两处直连一直是隐形的（其中 plugin_manager 还是函数内延迟导入，行首 grep 会漏）。
RUN_TO_SYS_ALLOWED = {"agent.sys.progress", "agent.sys.plugin_manager"}


def test_run_layer_sys_imports_are_declared():
    bad: list[tuple[str, str]] = []
    for fp in _py_files():
        if _layer(fp) != "run":
            continue
        for m in _imported_modules(fp):
            if m.startswith("agent.sys.") and m not in RUN_TO_SYS_ALLOWED:
                bad.append((str(fp.relative_to(AGENT)), m))
    assert not bad, (
        "传输层（run/）新增了未登记的 sys 直连 —— sys 实现不应经传输层绕过 api 取用；"
        "若确属无法经 api 导出的缺口，请先评审再补进 RUN_TO_SYS_ALLOWED:\n"
        + "\n".join(f"  {f} -> {m}" for f, m in bad))


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
    "agent_migrations": "sys",   # 迁移登记表（MigrationRegistry 防脏武校验依赖；sys/storage 管）
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


def test_every_migrated_table_has_declared_owner():
    """反向断言：建表清单里的每张表都必须有归属声明。

    只做「owner → 已建表」单向断言时，**新表漏登记 owner 是静默的** ——
    归属守卫对未登记的表直接放行（``owner is None → continue``），
    `agent_migrations` 就曾长期处于这种空转状态。
    """
    text = (AGENT / "tables.py").read_text(encoding="utf-8")
    declared = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", text))
    unowned = declared - set(TABLE_OWNER) - KERNEL_TABLES
    assert not unowned, (
        "建表清单中存在未登记归属的表（归属守卫对其空转，需补进 TABLE_OWNER 或 "
        "KERNEL_TABLES）: " + ", ".join(sorted(unowned)))
