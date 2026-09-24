"""插件管理测试 —— 发现/启停/新增/移除 + 装配启用集（引擎式语义）。"""

from __future__ import annotations

import asyncio
import sys

import pytest

from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path):
    return PluginManager(plugin_dir=str(tmp_path / "plugins"),
                         state_file=str(tmp_path / "state.json"))


def _drop_module(name: str) -> None:
    sys.modules.pop(f"agent_plugins.{name}", None)


def test_scaffold_discover_enable_load_remove(tmp_path):
    pm = _pm(tmp_path)
    assert pm.discovered() == []
    info = pm.scaffold("greeter", provides=["greet"])
    assert info["name"] == "greeter"
    assert (tmp_path / "plugins" / "greeter" / "__init__.py").is_file()
    _drop_module("greeter")
    try:
        assert "plugins.greeter" in pm.enabled()          # 新增即自动启用（引擎语义）
        cands = pm.discovered()
        assert any(c["name"] == "plugins.greeter" and c["provides"] == ["greet"]
                   and c["runnable"] for c in cands)
        loaded = pm.load()
        assert any(p.id == "plugins.greeter" for p in loaded)
        # 停用 → 不再装配
        pm.set_enabled("plugins.greeter", False)
        assert "plugins.greeter" not in pm.enabled()
        assert not any(p.id == "plugins.greeter" for p in pm.load())
        # 移除 → 目录删除 + removed 标记
        out = pm.remove("plugins.greeter")
        assert out["removed"] is True
        assert not (tmp_path / "plugins" / "greeter").exists()
    finally:
        _drop_module("greeter")


def test_scaffold_duplicate_name_rejected(tmp_path):
    pm = _pm(tmp_path)
    pm.scaffold("dup", provides=["a"])
    _drop_module("dup")
    try:
        with pytest.raises(FileExistsError):
            pm.scaffold("dup", provides=["b"])
    finally:
        _drop_module("dup")


def test_build_agent_assembles_only_enabled_dir_plugin(tmp_path):
    pm = _pm(tmp_path)
    pm.scaffold("hello_probe", provides=["hello"])
    _drop_module("hello_probe")

    async def s(enabled: bool, db: str):
        if not enabled:
            pm.set_enabled("plugins.hello_probe", False)
        c = build_agent(config={"db_path": str(tmp_path / db)}, plugin_mgr=pm)
        await c.assemble()
        assert c.root.has("hello") is enabled
        await c.teardown()

    try:
        _run(s(enabled=True, db="a.db"))
        _run(s(enabled=False, db="b.db"))
    finally:
        _drop_module("hello_probe")
        pm.remove("plugins.hello_probe")


def test_package_plugin_with_relative_import_loads(tmp_path):
    """插件包（以 ``__init__.py`` 为入口）支持包内相对导入 —— 多文件插件的加载前提。"""
    pm = _pm(tmp_path)
    pkg = tmp_path / "plugins" / "pkgprobe"
    pkg.mkdir(parents=True)
    (pkg / "helper.py").write_text("VALUE = 'ok'\n", encoding="utf-8")
    (pkg / "__init__.py").write_text(
        '"""插件包探针（多文件 + 相对导入）。"""\n'
        "from agent.api import plugin\n"
        "from .helper import VALUE\n"
        "\n"
        "\n"
        "@plugin\n"
        "class PkgProbe:\n"
        "    name = 'plugins.pkgprobe'\n"
        "    provides = ['pkgprobe']\n"
        "    value = VALUE\n"
        "\n"
        "    async def apply(self, ctx):\n"
        "        ctx.provide('pkgprobe', self)\n",
        encoding="utf-8")
    pm.set_enabled("plugins.pkgprobe", True)
    _drop_module("pkgprobe")
    _drop_module("pkgprobe.helper")
    try:
        assert any(c["name"] == "plugins.pkgprobe" for c in pm.discovered())
        loaded = pm.load()
        p = next(x for x in loaded if x.id == "plugins.pkgprobe")
        assert p._obj().value == "ok"          # 相对导入成功（子模块已执行）

        async def s():
            c = build_agent(config={"db_path": str(tmp_path / "p.db")}, plugin_mgr=pm)
            await c.assemble()
            assert c.root.has("pkgprobe")
            await c.teardown()

        _run(s())
    finally:
        _drop_module("pkgprobe")
        _drop_module("pkgprobe.helper")