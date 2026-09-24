"""agent.sys.plugin_manager —— 插件管理（发现/启停/新增/卸载），语义对齐引擎 PluginManager。

- 约定目录：``agent/plugins/<业务名>/__init__.py``（或 ``plugin.py``）内含 ``@plugin`` 类。
  以 ``__init__.py`` 为入口的插件是**插件包**：可含子模块并用包内相对导入（``from .loop import …``）。
- 期望态：``data/agent_plugin_state.json``（enabled / removed）。
- 诚实边界：装配在启动时一次性完成 → 启停为「重启/重建后生效」（不热插拔）；卸载 = 停用 + 删目录 + removed 标记。

独立实现：零 import 引擎（与 agent 运行时同一纪律）。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

from agent.api import Plugin

logger = logging.getLogger("agent.plugin_manager")

_MARKERS = ("__init__.py", "plugin.py")

_NS_PKG = "agent_plugins"        # 目录插件的命名空间父包（使插件可作为「包」被加载）

_PLUGIN_TEMPLATE = '''"""插件包：{name}（一个业务 = 一个插件）。

用 ``@plugin`` 提供一类能力（provides），声明依赖（inject）；apply 装配期注册 service/effect。
"""

from agent.api import plugin


@plugin
class {cls}:
    """{name} 业务插件。"""

    name = "plugins.{name}"
    version = "0.1.0"
    layer = "business"
    provides = {provides_repr}
    inject = []

    async def apply(self, ctx):
        # 装配期：注册服务 / 建表(ctx.migrations.apply(...)) / 订阅(ctx.on(...))
        ctx.provide("{svc0}", self)
        ctx.effect(self._teardown)

    async def start(self, ctx):
        pass

    async def run(self, ctx, **params):
        # 主任务：供「运行」调度；把业务动作写这里
        pass

    async def _teardown(self):
        pass
'''


class PluginManager:
    """agent 侧插件发现 / 启停 / 新增 / 卸载。"""

    def __init__(self, plugin_dir: str | Path = "agent/plugins",
                 state_file: str | Path | None = None):
        self._dir = Path(plugin_dir)
        self._state_file = Path(state_file or "data/agent_plugin_state.json")

    # ---------- 发现 ----------

    def discovered(self) -> list[dict]:
        """扫描约定目录下的候选业务插件（轻量元数据）。"""
        out: list[dict] = []
        if not self._dir.is_dir():
            return out
        for child in sorted(self._dir.iterdir()):
            if not child.is_dir():
                continue
            locator = _find_locator(child)
            if locator is None:
                continue
            try:
                module = _load_module(locator)
            except Exception as exc:  # noqa: BLE001 —— 单插件损坏不拖垮发现
                out.append({"name": child.name, "package": child.name,
                            "loadable": False, "error": str(exc)})
                continue
            for p in _collect_plugins(module):
                out.append({"name": p.id, "package": child.name, "loadable": True,
                            "layer": getattr(p, "layer", "business"),
                            "provides": list(p.provides), "inject": list(p.inject),
                            "version": p.version,
                            "tables": list(p.tables),
                            "description": p.description,
                            "runnable": _candidate_runnable(p)})
        return out

    # ---------- 启停（持久化期望态） ----------

    def enabled(self) -> list[str]:
        return self._read_state().get("enabled", [])

    def set_enabled(self, name: str, enable: bool) -> list[str]:
        data = self._read_state()
        enabled = set(data.get("enabled", []))
        if enable:
            enabled.add(name)
            data["enabled"] = sorted(enabled)
        else:
            data["enabled"] = sorted(enabled - {name})
        self._write_state(data)
        return data["enabled"]

    def removed(self) -> list[str]:
        return self._read_state().get("removed", [])

    # ---------- 新增 / 卸载 ----------

    def scaffold(self, name: str, provides: list[str] | None = None) -> dict:
        name = (name or "").strip().lower()
        if not name or not name.isidentifier():
            raise ValueError(f"插件名必须为合法标识符: {name!r}")
        provides = provides or ["svc"]
        if not all(isinstance(p, str) and p.isidentifier() for p in provides):
            raise ValueError("provides 需为非空合法标识符列表")
        root = self._dir / name
        if root.exists():
            raise FileExistsError(f"已存在: {root}")
        root.mkdir(parents=True)
        cls = "".join(p.title() for p in name.split("_")) or "MyPlugin"
        (root / "__init__.py").write_text(
            _PLUGIN_TEMPLATE.format(name=name, cls=cls, svc0=provides[0],
                                    provides_repr=str(provides)),
            encoding="utf-8")
        self.set_enabled(f"plugins.{name}", True)   # 新增即自动启用（引擎语义）
        return {"path": str(root), "name": name, "provides": provides, "cls": cls}

    def remove(self, name_or_id: str) -> dict:
        """停用 + 删目录 + removed 标记。输入可为插件 id 或包名。"""
        pkg = self.package(name_or_id)
        self.set_enabled(name_or_id, enable=False)
        data = self._read_state()
        removed = set(data.get("removed", []))
        removed.add(name_or_id)
        data["removed"] = sorted(removed)
        self._write_state(data)
        target = self._dir / pkg
        removed_ok = False
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed_ok = True
        return {"removed": removed_ok, "package": pkg}

    def package(self, name_or_id: str) -> str:
        """插件 id/包名 → 目录名（优先候选 package 字段）。"""
        try:
            for c in self.discovered():
                if c.get("name") == name_or_id or c.get("package") == name_or_id:
                    return c.get("package") or name_or_id
        except Exception:  # noqa: BLE001
            pass
        return name_or_id.rsplit(".", 1)[-1] if "." in name_or_id else name_or_id

    # ---------- 加载（装配前） ----------

    def load(self) -> list[Plugin]:
        """加载「启用 id」对应的目录插件 Plugin 对象。"""
        out: list[Plugin] = []
        if not self._dir.is_dir():
            return out
        enabled = set(self.enabled())
        for child in sorted(self._dir.iterdir()):
            if not child.is_dir():
                continue
            locator = _find_locator(child)
            if locator is None:
                continue
            try:
                module = _load_module(locator)
            except Exception as exc:  # noqa: BLE001 —— 缺失/损坏：记录并跳过
                logger.warning("插件 %s 无法导入: %s", child.name, exc)
                continue
            out.extend(p for p in _collect_plugins(module) if p.id in enabled)
        return out

    # ---------- 内部 ----------

    def _read_state(self) -> dict:
        if not self._state_file.exists():
            return {"enabled": [], "removed": []}
        try:
            return json.loads(self._state_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"enabled": [], "removed": []}

    def _write_state(self, data: dict) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._state_file)


def _find_locator(pkg_dir: Path) -> Path | None:
    for marker in _MARKERS:
        candidate = pkg_dir / marker
        if candidate.is_file():
            return candidate
    return None


def _ensure_namespace_pkg() -> None:
    """登记命名空间父包 ``agent_plugins``（只建一次）。

    有了它，插件包内的相对导入（``from .loop import X``）才能解析 —— 否则
    ``agent_plugins.react`` 没有父包，`__path__` 子模块查找会失败。
    """
    if _NS_PKG in sys.modules:
        return
    import types

    ns = types.ModuleType(_NS_PKG)
    ns.__path__ = []              # 命名空间包：子模块由 sys.modules 直接登记
    sys.modules[_NS_PKG] = ns


def _load_module(locator: Path) -> Any:
    """加载插件模块。

    - ``__init__.py``（插件**包**）：以包方式加载（带 ``__path__``）→ 包内相对导入可用，
      按设计可分多文件（prompt / components / loop / __init__）。
    - ``plugin.py``（单文件插件）：普通模块加载。
    """
    _ensure_namespace_pkg()
    module_name = f"{_NS_PKG}.{locator.parent.name}"
    if locator.name == "__init__.py":
        spec = importlib.util.spec_from_file_location(
            module_name, locator, submodule_search_locations=[str(locator.parent)])
    else:
        spec = importlib.util.spec_from_file_location(module_name, locator)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin module {locator}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    if locator.name == "__init__.py":
        setattr(sys.modules[_NS_PKG], locator.parent.name, module)
    spec.loader.exec_module(module)
    return module


def _collect_plugins(module: Any) -> list[Plugin]:
    return [v for v in vars(module).values() if isinstance(v, Plugin)]


def _candidate_runnable(p: Plugin) -> bool:
    import inspect
    if not isinstance(getattr(p, "_raw", None), type):
        return False
    return bool(inspect.iscoroutinefunction(getattr(p._raw, "run", None)))


__all__ = ["PluginManager"]