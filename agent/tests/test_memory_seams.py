"""D2 · 接缝 Protocol 化测试。

- ReactResultLike 契约对齐：react.ReactResult 字段面与 core.contract.ReactResultLike
  成员一一对应（react 改字段名 → 本测试即红，chat 不再运行期才断）；
- memory 三组件开缝：ctx.get("memory.extractor") 注入假实现 → remember 走注入组件
  （证明容器级可换，与 react 四组件同构）。
"""

from __future__ import annotations

import asyncio
import dataclasses
from pathlib import Path

from agent.api import plugin
from agent.core.contract import ReactResultLike
from agent.plugins.memory import MemoryExtractorLike, MemorySearchLike, MemoryStoreLike
from agent.plugins.react import ReactResult
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


# ---- ReactResultLike 结构守卫 ----

def test_react_result_matches_protocol():
    members = {"ok", "text", "reason", "tokens_in", "tokens_out"}
    field_names = {f.name for f in dataclasses.fields(ReactResult)}
    missing = members - field_names
    assert not missing, f"ReactResult 缺少契约成员: {missing}"
    assert callable(getattr(ReactResult, "as_evidence", None)), \
        "ReactResult 缺少契约方法 as_evidence()"
    # Protocol 侧反向核对（防 Protocol 自身漂移）
    proto_annots = ReactResultLike.__annotations__
    assert set(proto_annots) == members, \
        f"ReactResultLike 注解面漂移: {set(proto_annots) ^ members}"


# ---- memory 三组件接缝注入 ----

class FakeExtractor:
    """注入用假提取器：不走 LLM，返回确定性结果（证明接缝生效）。"""

    async def extract(self, ctx, payload: dict) -> dict:
        return {"facts": ["注入提取的事实：用户喜欢蓝色"], "intentions": [],
                "entities": [], "relations": []}


@plugin
class FakeExtractorPlugin:
    """在目录插件 memory 之前装配，预置 "memory.extractor" 接缝。

    注意：容器按插件名排序装配（``ready.sort()``），名字须排在 ``plugins.memory`` 之前
    才能保证 apply 顺序——这正是接缝注入的时序前提。
    """

    name = "aaa.fake_extractor"
    layer = "business"
    provides = []
    inject = []

    async def apply(self, ctx) -> None:
        ctx.provide("memory.extractor", FakeExtractor())


def _build(tmp_path):
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "plugin_state.json"))
    pm.set_enabled("plugins.memory", True)
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       plugin_mgr=pm, extra_plugins=(FakeExtractorPlugin,))


def test_memory_extractor_seam_injection(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        out = await c.root.memory.remember(c.root, {"user": "随便说点什么"})
        assert out["ok"], f"remember 应成功: {out}"
        hits = await c.root.memory.recall(c.root, "注入 事实")
        assert any("注入提取的事实" in h["content"] for h in hits), \
            "提取结果应来自注入的 FakeExtractor（接缝生效）"

    _run(s())


def test_memory_protocols_cover_driver_consumed_surface():
    """三组件 Protocol 覆盖 Driver 消费面（防 Protocol 与实现漂移）。"""
    from agent.plugins.memory import MemoryDriver, MemoryExtractor, MemorySearch, MemoryStore

    surface = {
        MemoryStoreLike: (["upsert", "touch", "valid", "delete"], MemoryStore),
        MemoryExtractorLike: (["extract"], MemoryExtractor),
        MemorySearchLike: (["score"], MemorySearch),
    }
    for proto, (names, impl) in surface.items():
        for name in names:
            assert callable(getattr(proto, name, None)), \
                f"{proto.__name__} 缺少契约成员 {name!r}（Protocol 自身漂移）"
            assert callable(getattr(impl, name, None)), \
                f"{impl.__name__} 缺少 {proto.__name__} 契约成员 {name!r}"
    assert MemoryDriver is not None
