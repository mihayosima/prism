"""子 Agent 接缝（阶段 0）检测器型测试。

设计文档《子agent插件设计方案-2026-10-09.md》v0.3 §3.2 / §7：
- 不传 override 时，``resolve_tools`` 决策路径回落 ``visible_tools``、``resolve_cfg`` 回落
  ``self._cfg``、``_resolve_governance`` 回落容器现取 —— **逐值等价**（非逐字节，LLM 非确定）。
- 传 override 时：工具集按白名单**交集**收窄（只能收窄不能放宽，审查意见 #2/#R3）；
  ``cfg_overrides`` 合并已知字段、忽略未知键；``governance_override`` 覆盖容器治理器；
  ``components_override`` 按 key 替换组件（缺省回落）。

均为纯函数 / 轻量 fake ctx，不触发 DB/Redis/uvicorn，避免重夹具。
"""

from __future__ import annotations

from unittest.mock import patch

from agent.plugins.react.components import (
    DefaultActor, DefaultFinalizer, DefaultObserver, DefaultReasoner,
)
from agent.plugins.react.loop import (
    ReactConfig, ReactLoop, _apply_whitelist, visible_tools,
)


class _FakeTasks:
    def __init__(self, catalog):
        self._catalog = catalog

    def catalog(self):
        return self._catalog


class _FakeCtx:
    """最小 ctx：仅支撑 visible_tools / _resolve_governance 的读取路径。"""

    def __init__(self, catalog, gov=None):
        self._tasks = _FakeTasks(catalog)
        self._gov = gov

    @property
    def tasks(self):
        return self._tasks

    def get(self, key, default=None):
        if key == "context":
            return self._gov
        return default

    def has(self, key):
        if key == "context":
            return self._gov is not None
        return False


def _catalog():
    return [
        {"name": "agent.read", "risk": "read", "owner": "plugins.fs",
         "description": "read a file"},
        {"name": "agent.write", "risk": "write", "owner": "plugins.fs",
         "description": "write a file"},
        {"name": "web.search", "risk": "read", "owner": "plugins.web",
         "description": "search the web"},
    ]


def _loop(**kw):
    return ReactLoop(DefaultReasoner(), DefaultActor(caller="t"), DefaultObserver(),
                     DefaultFinalizer(), ReactConfig(), **kw)


# ---------------- 决策路径等价（无 override） ----------------

@patch("agent.plugins.react.loop.current_permission_mode", lambda ctx, d="": "open")
def test_no_override_resolve_tools_equals_visible_tools():
    ctx = _FakeCtx(_catalog())
    loop = _loop()
    got = loop.resolve_tools(ctx)
    expected = visible_tools(ctx)
    assert [t["name"] for t in got] == [t["name"] for t in expected]


def test_no_override_resolve_cfg_returns_same_object():
    loop = _loop()
    # 无 override ⇒ 返回 self._cfg 同一对象（不拷贝、不新建），逐值等价
    assert loop.resolve_cfg() is loop.config


def test_no_override_governance_falls_through():
    ctx = _FakeCtx(_catalog(), gov=None)
    loop = _loop()
    assert loop._resolve_governance(ctx) is None


def test_no_override_components_fall_through():
    loop = _loop()
    assert isinstance(loop._reasoner, DefaultReasoner)
    assert isinstance(loop._actor, DefaultActor)


# ---------------- tools_override：交集收窄（只能收窄不能放宽） ----------------

@patch("agent.plugins.react.loop.current_permission_mode", lambda ctx, d="": "open")
def test_tools_override_by_name_intersection():
    ctx = _FakeCtx(_catalog())
    loop = _loop(tools_override=["agent.read"])
    assert [t["name"] for t in loop.resolve_tools(ctx)] == ["agent.read"]


@patch("agent.plugins.react.loop.current_permission_mode", lambda ctx, d="": "open")
def test_tools_override_by_domain():
    ctx = _FakeCtx(_catalog())
    loop = _loop(tools_override=["plugins.fs"])
    assert {t["name"] for t in loop.resolve_tools(ctx)} == {"agent.read", "agent.write"}


@patch("agent.plugins.react.loop.current_permission_mode", lambda ctx, d="": "open")
def test_tools_override_star_is_noop():
    ctx = _FakeCtx(_catalog())
    loop = _loop(tools_override=["*"])
    assert {t["name"] for t in loop.resolve_tools(ctx)} == {
        "agent.read", "agent.write", "web.search"}


@patch("agent.plugins.react.loop.current_permission_mode", lambda ctx, d="": "open")
def test_tools_override_cannot_widen_beyond_visible():
    """白名单给一个不存在的工具名 —— 不能凭空造出工具，只能收窄。"""
    ctx = _FakeCtx(_catalog())
    loop = _loop(tools_override=["agent.read", "agent.nonexistent_superpower"])
    assert [t["name"] for t in loop.resolve_tools(ctx)] == ["agent.read"]


@patch("agent.plugins.react.loop.current_permission_mode", lambda ctx, d="": "open")
def test_tools_override_empty_is_noop():
    ctx = _FakeCtx(_catalog())
    loop = _loop(tools_override=[])
    assert {t["name"] for t in loop.resolve_tools(ctx)} == {
        "agent.read", "agent.write", "web.search"}


def test_apply_whitelist_pure_and_idempotent():
    base = _catalog()
    out = _apply_whitelist(base, ["agent.read"])
    assert [t["name"] for t in out] == ["agent.read"]
    assert out is not base                      # 不改入参
    assert _apply_whitelist(base, None) is base  # None ⇒ 原对象


# ---------------- cfg_overrides：合并已知字段、忽略未知 ----------------

def test_resolve_cfg_merges_known_fields():
    loop = _loop(cfg_overrides={"max_steps": 3, "deadline_s": 5.0})
    cfg = loop.resolve_cfg()
    assert cfg.max_steps == 3
    assert cfg.deadline_s == 5.0
    assert cfg.history_keep == ReactConfig().history_keep   # 未覆盖字段保持默认


def test_resolve_cfg_ignores_unknown_key():
    loop = _loop(cfg_overrides={"nonexistent_field": 99})
    cfg = loop.resolve_cfg()
    assert cfg.max_steps == ReactConfig().max_steps        # 未炸、未改


# ---------------- governance_override：覆盖容器治理器 ----------------

def test_governance_override_wins_over_container():
    fake = object()
    ctx = _FakeCtx(_catalog(), gov=object())   # 容器里另有一个 gov
    loop = _loop(governance_override=fake)
    assert loop._resolve_governance(ctx) is fake


# ---------------- components_override：按 key 替换（Q6 接缝） ----------------

def test_components_override_replaces_and_falls_through():
    custom = DefaultReasoner()
    loop = _loop(components_override={"reasoner": custom})
    assert loop._reasoner is custom
    assert isinstance(loop._actor, DefaultActor)   # 未覆盖 ⇒ 回落


# ---------------- 递归护栏深度 ----------------

def test_depth_default_and_set():
    assert _loop().depth == 0
    assert _loop(depth=2).depth == 2
