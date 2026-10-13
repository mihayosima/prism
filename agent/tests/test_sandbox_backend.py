"""P0 分级沙箱后端骨架（core/sandbox_backend）单测。

纪律：本文件只测**纯逻辑**（dataclass / 后端选择 / 降级语义），不依赖容器装配，
与 ``test_sandbox_plugin.py`` 的插件级测试互补。

★ 两条**契约守卫**是本文件最重要的用例（由代码复核得出，防止后来者改崩桥）：
  1. ``SpawnOptions.to_dict()`` **必须**含 ``cwd`` / ``env`` —— 既有 MCP 桥
     （playwright / windows）只读这两个键（``plugins/playwright_mcp/__init__.py:99-104``）。
  2. 接缝 ``Services.CHILD_SPAWN_OPTIONS`` 的 callable 只被**单参**调用
     （``fn(current_session_key(ctx))``）⇒ 后端不能要求 ``(target, policy, session_key)``
     三参形状。本文件用"桥的调用方式"直接驱动断言，契约一改这里就红。
"""

from __future__ import annotations

import pytest

from agent.core.sandbox_backend import (BackendUnavailable, NullBackend, SpawnOptions,
                                        describe_backend, select_backend)


def _bridge_style_read(opts: dict | None):
    """**模拟桥的消费方式**：只传一个 session_key，且只读 ``cwd`` / ``env`` 两键。

    这段是照 ``plugins/playwright_mcp/__init__.py:97-104`` 抄的调用姿势，
    用来锁死接缝形状 —— 改契约 ⇒ 这里先红。
    """
    if not opts:
        return None, {}
    return opts["cwd"], opts["env"]


# ---------------------------------------------------------------------------
# SpawnOptions：形状与向后兼容
# ---------------------------------------------------------------------------

def test_to_dict_keeps_legacy_keys_bridges_depend_on():
    """★ 契约守卫①：``cwd`` / ``env`` 必须在，否则既有桥 KeyError。"""
    d = SpawnOptions(cwd="data/sandbox/s1", env={"TMP": "data/sandbox/s1"}).to_dict()
    assert "cwd" in d and "env" in d
    cwd, env = _bridge_style_read(d)
    assert cwd == "data/sandbox/s1" and env["TMP"] == "data/sandbox/s1"


def test_to_dict_omits_empty_optional_fields():
    """可选字段为空时不入字典 —— 不把一堆 None 塞给今天不消费它们的桥。"""
    d = SpawnOptions(cwd="x", env={}).to_dict()
    assert "token" not in d and "creationflags" not in d and "proxy_addr" not in d
    assert "netns" not in d and "seccomp_fd" not in d


def test_to_dict_carries_enforcement_fields_when_set():
    """后端真落地后（P1/P4）的字段能被带出去，供 mcp_client 消费。"""
    d = SpawnOptions(cwd="x", env={}, token=123, creationflags=0x00000200,
                     net_policy="proxy", proxy_addr="127.0.0.1:8899",
                     netns=True).to_dict()
    assert d["token"] == 123 and d["creationflags"] == 0x00000200
    assert d["net_policy"] == "proxy" and d["proxy_addr"] == "127.0.0.1:8899"
    assert d["netns"] is True


# ---------------------------------------------------------------------------
# NullBackend：诚实 none
# ---------------------------------------------------------------------------

def test_null_backend_is_honestly_none():
    b = NullBackend()
    assert b.name == "null" and b.strength == "none"
    assert b.capability_probe()["available"] is True
    # 只有"本就没声明隔离"才算可兑现
    assert b.can_enforce("none") is True
    assert b.can_enforce("read-only") is False
    assert b.can_enforce("workspace-write") is False


def test_null_backend_keeps_workspace_and_marks_no_enforcement():
    """落点收拢**保留**（不劣化），但强制信息如实为 none。"""
    base = SpawnOptions(cwd="data/sandbox/s1", env={"TMP": "data/sandbox/s1"},
                        mode="workspace-write")
    out = NullBackend().spawn_options(base, {"network": "allow"}, "s1")
    assert out.cwd == "data/sandbox/s1"                 # 落点没被吃掉
    assert out.env == {"TMP": "data/sandbox/s1"}
    assert out.strength == "none" and out.enforced_by == "none"
    assert out.backend == "null"


def test_null_backend_picks_up_network_policy():
    out = NullBackend().spawn_options(SpawnOptions(), {"network": "proxy"})
    assert out.net_policy == "proxy"


# ---------------------------------------------------------------------------
# select_backend：降级语义（auto 不报错 / 显式不可得可见 / strict 抛错）
# ---------------------------------------------------------------------------

def test_off_and_null_are_first_class_tiers():
    """off/null 是**一等档位**：不算降级、不算错误。"""
    for name in ("off", "null"):
        backend, info = select_backend(name)
        assert backend.name == "null"
        assert info["fell_back"] is False and info["error"] is False


def test_auto_falls_back_to_off_without_error():
    """★ 用户裁定：进程级运行时不满足 ⇒ Sandbox Runtime 默认 **off，不报错**。

    P0 里 wsl2 / win-token 尚未实现 ⇒ auto 必然回落 null，且**不得**报 error。
    """
    backend, info = select_backend("auto")
    assert backend.name == "null" and info["backend"] == "null"
    assert info["error"] is False and info["fell_back"] is False
    assert "auto" in info["reason"] or "off" in info["reason"]


def test_explicit_unavailable_backend_is_visible_not_silent():
    """显式点名却要不到 ⇒ **可见**（fell_back + error + enforced_by=backend-error）。"""
    backend, info = select_backend("wsl2")
    assert backend.name == "null"                 # 回落
    assert info["fell_back"] is True and info["error"] is True
    assert info["enforced_by"] == "backend-error"


def test_unknown_backend_name_falls_back_to_auto_semantics():
    backend, info = select_backend("乱写的后端")
    assert backend.name == "null" and info["error"] is False


def test_strict_raises_when_explicit_backend_unavailable():
    """strict=true：显式要求不可得 ⇒ 抛（fail-closed 的**告警侧**）。"""
    with pytest.raises(BackendUnavailable):
        select_backend("wsl2", strict=True)


def test_strict_does_not_affect_auto():
    """auto 的本意就是"尽力给、没有就 off"⇒ strict 不该把 auto 变成硬失败。"""
    backend, info = select_backend("auto", strict=True)
    assert backend.name == "null" and info["error"] is False


def test_describe_backend_never_raises():
    """``status()`` / 审计路径用：即使 strict 也吞异常，但要留 error 标记。"""
    info = describe_backend("wsl2", strict=True)
    assert info["error"] is True and info["enforced_by"] == "backend-error"
    assert describe_backend("auto")["error"] is False


# ---------------------------------------------------------------------------
# 与插件自述的衔接（describe 据后端真实强度，不再硬编码）
# ---------------------------------------------------------------------------

def test_plugin_describe_uses_backend_strength_when_given():
    from agent.plugins.sandbox import DEFAULT_CONFIG, describe

    # 无后端信息（纯函数穷举路径）⇒ 诚实 none，与旧硬编码天花板同值
    d = describe({**DEFAULT_CONFIG, "mode": "workspace-write"})
    assert d["enforcement"] == "none" and d["gap"] is True

    # 给了后端信息 ⇒ 由后端自报（这里用假后端证明"不再写死"）
    fake = {"backend": "wsl2", "strength": "partial", "enforced_by": "wsl2-landlock",
            "error": False, "reason": "已兑现"}
    d2 = describe({**DEFAULT_CONFIG, "mode": "workspace-write"}, "", fake)
    assert d2["enforcement"] == "partial" and d2["gap"] is False
    assert d2["backend"] == "wsl2" and d2["enforced_by"] == "wsl2-landlock"


def test_seam_contract_single_arg_callable():
    """★ 契约守卫②：接缝 callable 必须**能只用一个参数**被调用（桥就是这么调的）。"""
    def _provider(session_key: str = "") -> dict | None:
        # 这就是插件侧未来的形状：单参、返回 dict|None
        backend, _info = select_backend("auto")
        opts = backend.spawn_options(SpawnOptions(cwd="ws", env={}), {}, session_key)
        return opts.to_dict()

    out = _provider("sess-1")          # 桥只传 session_key
    cwd, env = _bridge_style_read(out)
    assert cwd == "ws" and env == {}
