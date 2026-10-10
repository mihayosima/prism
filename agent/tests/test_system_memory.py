"""系统记忆（第 1 层）测试：文件加载 + 各阶段注入 + 外部覆盖 + 优雅降级。

覆盖用户敲定决策：
- A 显式排序、扁平不递归；B 外部目录完全覆盖（非合并）；C 仅启动加载（无热重载）；
- D 静态底座一致注入 chat/react，动态块按原模式裁剪。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.api import Services
from agent.plugins.react.prompt import build_system
from agent.profiles import build_agent
from agent.plugins.system_memory import SystemMemory, load_system_memory


def _run(coro):
    return asyncio.run(coro)


# ---- 加载器原语 ----

def test_load_packaged_nonempty_and_ordered():
    """默认目录含样本文件，且顺序为显式文件名序（00 在 01 前）。"""
    text = load_system_memory()
    assert text.strip()
    i_id = text.find("身份")
    i_bd = text.find("边界")
    assert i_id != -1 and i_bd != -1
    assert i_id < i_bd                       # 00-identity 在 01-boundaries 前


def test_load_explicit_order_not_fs_order(tmp_path: Path):
    """显式按文件名排序，不依赖文件系统枚举顺序（乱序写入也应归正）。"""
    for name in ("10-b.md", "02-y.md", "01-z.md"):
        (tmp_path / name).write_text(f"[{name}]", encoding="utf-8")
    text = load_system_memory(str(tmp_path))
    assert text.index("[01-z.md]") < text.index("[02-y.md]") < text.index("[10-b.md]")


def test_load_flat_no_recursion(tmp_path: Path):
    """扁平：子目录内的 md 不纳入，仅顶层 md。"""
    (tmp_path / "top.md").write_text("TOP", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("NESTED", encoding="utf-8")
    text = load_system_memory(str(tmp_path))
    assert "TOP" in text
    assert "NESTED" not in text


def test_load_missing_dir_empty():
    """目录不存在 → 空串（优雅降级）。"""
    assert load_system_memory("/nonexistent_dir_xyz") == ""


def test_external_override_full_replace(tmp_path: Path):
    """外部目录配置 → 完全覆盖默认目录（非合并）。"""
    ext = tmp_path / "ext"
    ext.mkdir()
    (ext / "custom.md").write_text("EXTERNAL_ONLY_MARKER", encoding="utf-8")
    text = load_system_memory(str(ext))
    assert "EXTERNAL_ONLY_MARKER" in text
    # 完全覆盖：外部目录不含默认身份文件内容
    assert "身份" not in text


# ---- 插件装配 ----

def test_plugin_provides_text(tmp_path: Path):
    """SystemMemory 提供 system_memory 服务，内容 = 默认目录加载结果。"""
    async def _coro():
        c = build_agent(config={"db_path": str(tmp_path / "agent.db")},
                        extra_plugins=(SystemMemory,))     # 目录插件：显式 opt-in
        await c.assemble()
        assert c.root.has(Services.SYSTEM_MEMORY)
        text = c.root.get(Services.SYSTEM_MEMORY)
        assert text == load_system_memory()
        assert text.strip()
    _run(_coro())


def test_chat_plan_injects_system_memory(tmp_path: Path):
    """chat 规划阶段把系统记忆前置注入 system 提示（静态底座 + 阶段指令）。"""
    async def _coro():
        c = build_agent(config={"db_path": str(tmp_path / "agent.db")},
                        extra_plugins=(SystemMemory,))     # 目录插件：显式 opt-in
        await c.assemble()
        ctx = c.root
        driver = ctx.get(Services.CHAT)
        sm = ctx.get(Services.SYSTEM_MEMORY) or ""
        captured: dict = {}

        async def _cap(_ctx, messages, **kw):
            captured["system"] = messages[0]["content"]
            return await ctx.llm.complete(_ctx, messages, **kw)

        ctx.llm.complete = _cap
        await driver._plan(ctx, "你好", prior=[])
        sys_text = captured["system"]
        assert sm and sm in sys_text
        assert "任务规划器" in sys_text                # PLAN_SYSTEM 常量内容
    _run(_coro())


def test_react_build_system_includes_sm():
    """react 多步循环同样把系统记忆作静态底座（与 chat 一致）。"""
    sm = load_system_memory()
    out = build_system([], system_memory=sm)
    assert sm in out
    assert "ReAct 循环" in out                    # REACT_SYSTEM 常量内容


def test_planning_discipline_injected():
    """P0-2 静态守卫：规划纪律（02-planning.md）必须进入注入后的系统提示词。

    防止该声明文件被误删/改名后悄悄失效——届时 build_system 输出将不再含
    「规划纪律 / plan 围栏 / 重复 3 次」标记，本测试即失败。
    """
    sm = load_system_memory()
    out = build_system([], system_memory=sm)
    assert "规划纪律" in out, "02-planning.md 未进入系统记忆装配"
    assert "plan" in out, "规划纪律协议缺少 plan 围栏块"
    assert "3 次" in out, "规划纪律缺少「重复 3 次即死循环」提示"


def test_disabled_graceful_when_dir_missing(tmp_path: Path):
    """外部目录指向不存在路径 → 提供空串，chat 仍可正常工作（不注入、不报错）。"""
    async def _coro():
        c = build_agent(config={"db_path": str(tmp_path / "agent.db")},
                        patch={"agent": {"system_memory": {"dir": "/no_such_dir_abc"}}},
                        extra_plugins=(SystemMemory,))
        await c.assemble()
        assert c.root.get(Services.SYSTEM_MEMORY) == ""
        ctx = c.root
        driver = ctx.get(Services.CHAT)
        captured: dict = {}

        async def _cap(_ctx, messages, **kw):
            captured["system"] = messages[0]["content"]
            return await ctx.llm.complete(_ctx, messages, **kw)

        ctx.llm.complete = _cap
        await driver._plan(ctx, "你好", prior=[])
        assert "身份" not in captured["system"]       # 未注入
        assert "任务规划器" in captured["system"]      # 阶段指令仍在
    _run(_coro())


# ---- 目录插件语义（与短期记忆同级：opt-in，非系统层硬装配） ----

def test_directory_plugin_opt_in(tmp_path: Path):
    """插件管理器可发现；未启用不装配（消费方软降级），启用后提供记忆文本。"""
    from agent.sys.plugin_manager import PluginManager

    async def _coro():
        pdir = str(Path(__file__).resolve().parent.parent / "plugins")
        pm = PluginManager(plugin_dir=pdir, state_file=str(tmp_path / "state.json"))
        cands = [c for c in pm.discovered() if c["name"] == "plugins.system_memory"]
        assert cands, "plugins.system_memory 应可被发现（目录插件）"
        assert cands[0]["provides"] == [Services.SYSTEM_MEMORY]

        # 未启用 → 不装配（不裸崩）
        c1 = build_agent(config={"db_path": str(tmp_path / "a.db")}, plugin_mgr=pm)
        await c1.assemble()
        assert not c1.root.has(Services.SYSTEM_MEMORY)
        await c1.teardown()

        # 启用 → 装配并提供记忆文本
        pm.set_enabled("plugins.system_memory", True)
        c2 = build_agent(config={"db_path": str(tmp_path / "b.db")}, plugin_mgr=pm)
        await c2.assemble()
        assert c2.root.get(Services.SYSTEM_MEMORY) == load_system_memory()
        await c2.teardown()

    _run(_coro())
