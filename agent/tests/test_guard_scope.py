"""guard 收口测试（2026-10-03 批次 A）—— 模式匹配 + 域限定 + 会话级配置。

背景：``docs/reform of production line/沙箱与权限管理器重叠核定-2026-10-03.md`` 认定
guard 与 sandbox 功能重叠，收口方案是**模式匹配与域限定迁入 guard**（策略唯一持有者），
sandbox 后续转隔离档位提供者。

本文件覆盖迁入的三项能力，并刻意包含**防"服务起不来"**与**纯数据口径**两类用例：

1. ``match`` 真值表（与 ``sandbox.match`` 逐位等价，由 ``.workbuddy/verify_guard_match_equiv.py`` 交叉验证）
2. ``targets`` 域限定：域外**完全不过问**（不变成"什么都拦一遍"）
3. 会话级覆盖优先（容器 deny + 会话 allow ⇒ 放行）
4. ★ ``SESSION_CFG_VIEW`` **必须调用**而非当字典索引（沙箱踩过：写错会让**所有**工具调用抛错）
5. ★ ``permissions.targets`` 必须在 ``config_schema`` 登记（严格键集，不登记 ⇒ 写了就装配失败）
6. 拒绝返回体是纯数据（过 ``core/events._assert_pure_data``）
7. ``status()`` 自述真实策略（插件页可观测，防"看起来健康"）
8. 向后兼容：精确名配置行为**逐位不变**（``targets`` 默认 ``["*"]``）

⚠️ 第 4、5 条是"会打断一切"的守卫：``_matrix`` 在每个任务调用前都跑，
接缝读法错了或 schema 没登记，都会让服务起不来 / 工具全断。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.api import (CapabilityNotImplemented, ConfigError, SESSION_CFG_VIEW,
                       coerce_config, set_current_session_key, validate_config)
from agent.profiles import build_agent
from agent.sys.guard import DEFAULT_POLICY, in_scope, match

# ``agent.ping`` 是 profile 自带工具（**已注册**）⇒ 域限定用例直接用它，不重复注册
# （``TaskRegistry.register`` 对重名抛 ``AgentError``，见 ``tasks.py:138``）。
MCP_TOOLS = ("mcp.playwright.nav", "mcp.windows.open")


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path, permissions=None):
    cfg = {"db_path": str(tmp_path / "agent.db")}
    if permissions is not None:
        cfg["agent"] = {"permissions": permissions}
    return cfg


def _register(c, names=MCP_TOOLS, risk: str = "write") -> None:
    async def h():
        return {"ok": True}

    for n in names:
        c.root.tasks.register(c.root, n, schema={}, handler=h, risk=risk, owner="test")


async def _invoke(c, name: str):
    """``tasks.invoke`` 被拒时**抛** ``CapabilityNotImplemented``（不返回 ``.ok``）⇒
    归一成 ``(ok, error)``，让用例断言更直白。"""
    try:
        await c.root.tasks.invoke(c.root, name, caller="test")
        return True, ""
    except CapabilityNotImplemented as exc:
        return False, str(exc)


def _attach_view(c, sessions: dict) -> None:
    """挂会话配置视图接缝（形状与 ``run/chat.py::_attach_session_cfg_view`` 一致：
    它是**可调用对象** ``(section, sk) -> dict``，不是字典）。"""
    c.root.provide(SESSION_CFG_VIEW,
                   lambda section, sk=None: sessions.get(sk or "", {})
                   if section == "guard" else {})


@pytest.fixture(autouse=True)
def _isolate_session_scope():
    """会话键是 ContextVar ⇒ 用例间必须复位（沙箱已踩过顺向泄漏）。"""
    set_current_session_key(None)
    yield
    set_current_session_key(None)


# ---------------------------------------------------------------------------
# ① match 真值表（与 sandbox 逐位等价）
# ---------------------------------------------------------------------------

def test_match_truth_table():
    """尾部 ``*`` 通配 + 精确名 + 前缀陷阱。真值表与 ``sandbox.test_match_truth_table`` 同源。"""
    assert match("*", "anything") is True
    assert match("mcp.*", "mcp.a.b") is True
    assert match("mcp.*", "mcp.") is True
    assert match("mcp.*", "mcpx.a") is False      # 前缀陷阱：不得误匹配
    assert match("a.b", "a.b") is True
    assert match("a.b", "a.c") is False
    assert match("a.*", "a") is False             # 尾部星吃掉前缀后仍需至少一个字符
    assert match("", "a") is False
    assert match("   ", "a") is False


def test_pattern_deny_end_to_end(tmp_path):
    """★ 端到端：``task_deny: ["mcp.*"]`` 真的按模式拒掉整族工具。
    这条杀的是"把 match 换成精确名"那个破坏——单测 ``match`` 本身抓不到，
    因为被换掉的是**调用点**。断言必须穿过真实生产读取点（``_decide`` → ``_hit``）。
    """
    c = build_agent(config=_cfg(tmp_path, {"task_deny": ["mcp.*"]}))

    async def s():
        await c.assemble()
        _register(c, names=MCP_TOOLS)
        for n in MCP_TOOLS:
            ok, err = await _invoke(c, n)
            assert not ok and "guard" in err, f"mcp.* 模式应拒 {n}，实际 ok={ok}"
        # 非 MCP 工具不受 mcp.* 模式影响（域内但模式不匹配 ⇒ default=open 放行）
        ok_ping, _ = await _invoke(c, "agent.ping")
        assert ok_ping, "mcp.* 模式不应波及非 MCP 工具"
        await c.teardown()

    _run(s())


def test_default_scope_is_everything():
    """★ 向后兼容闸：``targets`` 默认 ``["*"]`` ⇒ 收窄前行为逐位不变。"""
    assert DEFAULT_POLICY["targets"] == ["*"]
    for t in ("agent.ping", "mcp.x.y", "anything"):
        assert in_scope(DEFAULT_POLICY, t) is True


# ---------------------------------------------------------------------------
# ② targets 域限定：域外完全不过问
# ---------------------------------------------------------------------------

def test_out_of_scope_is_untouched(tmp_path):
    """域外工具**连裁决都不做**（不变成"第二个什么都拦一遍的 guard"）。"""
    # ⚠️ 断言设计要点：域外工具（agent.ping）**故意不在 task_allow 名单里**且
    # default=closed ⇒ 若没有域限定，它会被收窄矩阵拒掉。**只有真域限定才放行**。
    # （早期版本让 ping 在 allow 名单里，两条断言在无域限定时也成立 ⇒ 杀不掉该破坏。）
    c = build_agent(config=_cfg(tmp_path, {
        "default": "closed", "task_allow": [],
        "targets": ["mcp.*"],
    }))

    async def s():
        await c.assemble()
        _register(c, names=("mcp.playwright.nav",))
        # 域内 + 收窄 + 未在 allow ⇒ 拒（证明矩阵真的在生效）
        ok_mcp, err_mcp = await _invoke(c, "mcp.playwright.nav")
        assert not ok_mcp and "guard" in err_mcp, f"域内应被矩阵拦：{err_mcp}"
        # 域内裁决必须**留痕**（证明是真裁决，不是恰好没被拦）
        rows = await c.root.db.query(
            c.root, "SELECT payload FROM agent_events"
                    " WHERE event='agent/permission/decision'")
        assert rows, "域内调用必须落审计（否则无法区分裁决与漏裁决）"
        # 域外（agent.ping 是 profile 自带工具）⇒ 完全不受矩阵影响
        ok_ping, err_ping = await _invoke(c, "agent.ping")
        assert ok_ping, f"域外工具不应被拦：{err_ping}"
        await c.teardown()

    _run(s())


def test_targets_pattern_narrows_domain(tmp_path):
    """``targets`` 自身也支持尾部 ``*``：``mcp.playwright.*`` 只管该桥。"""
    c = build_agent(config=_cfg(tmp_path, {
        "default": "closed", "task_allow": [],
        "targets": ["mcp.playwright.*"],
    }))

    async def s():
        await c.assemble()
        _register(c, names=MCP_TOOLS)
        ok_pw, err_pw = await _invoke(c, "mcp.playwright.nav")
        assert not ok_pw and "guard" in err_pw
        ok_win, err_win = await _invoke(c, "mcp.windows.open")
        assert ok_win, f"非 playwright 域应放行：{err_win}"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# ③ 会话级覆盖优先
# ---------------------------------------------------------------------------

def test_session_scope_overrides_container(tmp_path):
    """容器 deny + 会话 allow ⇒ 本会话放行；另一会话仍拒（G2 按会话隔离）。"""
    c = build_agent(config=_cfg(tmp_path, {"task_deny": ["mcp.playwright.nav"]}))

    async def s():
        await c.assemble()
        _register(c, names=("mcp.playwright.nav",))
        _attach_view(c, {"alice": {"task_deny": []}})

        set_current_session_key("alice")
        ok, err = await _invoke(c, "mcp.playwright.nav")
        assert ok, f"会话级清空 deny 后应放行：{err}"

        set_current_session_key("bob")
        ok2, _ = await _invoke(c, "mcp.playwright.nav")
        assert not ok2, "另一会话不得继承 alice 的覆盖"
        await c.teardown()

    _run(s())


def test_view_seam_must_be_called_not_indexed(tmp_path):
    """★ 防"服务起不来"：接缝是**可调用对象**，当字典用会让每次调用抛 AttributeError。

    本例把接缝换成一个"被索引即抛"的哨兵，验证 guard 只调用不索引。
    """
    c = build_agent(config=_cfg(tmp_path))

    async def s():
        await c.assemble()
        _register(c, names=("mcp.playwright.nav",))

        class _Seam(dict):
            def __call__(self, section, sk=None):
                return {}

            def __getitem__(self, key):      # pragma: no cover —— 触发即失败
                raise AssertionError("SESSION_CFG_VIEW 被当字典索引了（沙箱踩过的坑）")

        c.root.provide(SESSION_CFG_VIEW, _Seam())
        set_current_session_key("alice")
        ok, err = await _invoke(c, "mcp.playwright.nav")
        assert ok, f"接缝读法错误会打断所有工具调用：{err}"
        await c.teardown()

    _run(s())


def test_view_unavailable_falls_back_to_container(tmp_path):
    """接缝不可用（未挂 / 抛错）⇒ 回落容器级，**不裸崩**。"""
    c = build_agent(config=_cfg(tmp_path, {"task_deny": ["mcp.playwright.nav"]}))

    async def s():
        await c.assemble()
        _register(c, names=("mcp.playwright.nav",))
        set_current_session_key("alice")
        # 未挂 SESSION_CFG_VIEW ⇒ _matrix 走 try/except 回落容器级
        ok, _ = await _invoke(c, "mcp.playwright.nav")
        assert not ok, "回落容器级后容器 deny 应仍生效"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# ④ 防"服务起不来"：schema 严格键集
# ---------------------------------------------------------------------------

def test_permissions_targets_is_registered():
    """★ ``targets`` 必须在 ``config_schema`` 登记——``permissions`` 是**严格键集**，
    不登记 ⇒ 用户一写就 ``ConfigError`` ⇒ 服务起不来。"""
    validate_config(coerce_config(_cfg(_Tmp(), {"targets": ["mcp.*"]})))


def test_permissions_unknown_key_still_rejected():
    """严格性不得因新增 ``targets`` 而放松：拼错键仍须 fail-fast。"""
    with pytest.raises(ConfigError):
        validate_config(coerce_config(_cfg(_Tmp(), {"target": ["mcp.*"]})))


class _Tmp:
    """最小 tmp_path 替身（``validate_config`` 只读 ``db_path``）。"""

    class _P:
        def __truediv__(self, other):
            return f"/tmp/{other}"

    def __truediv__(self, other):
        return self._P().__truediv__(other)


# ---------------------------------------------------------------------------
# ⑤ 纯数据口径 + status 自述
# ---------------------------------------------------------------------------

def test_denial_payload_is_pure_data(tmp_path):
    """拒绝返回体是纯数据（``core/events`` 的 ``_assert_pure_data`` 会拦非纯数据）。"""
    c = build_agent(config=_cfg(tmp_path, {"task_deny": ["mcp.playwright.nav"]}))

    async def s():
        await c.assemble()
        _register(c, names=("mcp.playwright.nav",))
        ok, err = await _invoke(c, "mcp.playwright.nav")
        assert not ok and "guard" in err
        rows = await c.root.db.query(
            c.root, "SELECT payload FROM agent_events"
                    " WHERE event='agent/permission/decision' ORDER BY seq DESC LIMIT 1")
        assert rows and "deny" in rows[0]["payload"]
        await c.teardown()

    _run(s())


def test_status_reports_real_policy(tmp_path):
    """★ ``status()`` 必须自述**真实**策略——插件页据此展示；
    缺字段即等于"看起来健康"，那是 DSH 纪律①要防的误导。"""
    c = build_agent(config=_cfg(tmp_path, {
        "task_deny": ["mcp.*"], "targets": ["mcp.*"], "risk": {"danger": "deny"},
    }))

    async def s():
        await c.assemble()
        st = await c.root.guard.status(c.root)
        for k in ("default", "llm", "targets", "task_allow", "task_deny", "risk", "session"):
            assert k in st, f"status 缺键 {k!r}（插件页会显示不完整信息）"
        assert st["task_deny"] == ["mcp.*"]
        assert st["targets"] == ["mcp.*"]
        assert st["risk"] == {"danger": "deny"}
        # 策略插件报策略，**不报**隔离强度（那是 sandbox 的职责，不可互相代答）
        assert "enforcement" not in st
        await c.teardown()

    _run(s())


def test_exact_name_config_unchanged(tmp_path):
    """★ 向后兼容闸：既有**精确名**配置的裁决结果逐位不变。"""
    c = build_agent(config=_cfg(tmp_path, {
        "default": "closed", "task_allow": ["agent.ping"], "task_deny": [],
    }))

    async def s():
        await c.assemble()
        _register(c, names=("agent.pong", "mcp.playwright.nav"))
        ok_ping, _ = await _invoke(c, "agent.ping")
        assert ok_ping, "allow 白名单内的精确名应放行"
        ok_pong, err_pong = await _invoke(c, "agent.pong")
        assert not ok_pong and "guard" in err_pong
        # 未设 targets ⇒ 默认 ["*"] ⇒ MCP 工具也走收窄 allow 名单（域外不再被豁免）
        ok_mcp, err_mcp = await _invoke(c, "mcp.playwright.nav")
        assert not ok_mcp and "guard" in err_mcp
        await c.teardown()

    _run(s())


def test_llm_seam_still_guards(tmp_path):
    """★ 不可误判为可弃件：guard 是 ``LLM_PRE_CALL`` 的**唯一**挂载者（sandbox 不挂）。"""
    c = build_agent(config=_cfg(tmp_path, {"llm": "deny"}))

    async def s():
        await c.assemble()
        with pytest.raises(CapabilityNotImplemented):
            await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}],
                                      schema={}, caller="t")
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# ④ 设置页链路：plugins.guard → agent.permissions（_SECTION_ALIAS 特例）
# ---------------------------------------------------------------------------

def test_section_alias_maps_guard_to_permissions():
    """★ 设置页写 ``plugins.guard`` 必须落到 ``agent.permissions``。

    通用规则是同名映射（``plugins.X`` → ``agent.X``），但 guard 的矩阵 SSOT 节叫
    ``permissions``。若无 ``_SECTION_ALIAS`` 特例，设置页写的名单会落到**不存在**的
    ``agent.guard`` ⇒ ``ConfigError``（已实测）。
    """
    from agent.run import chat as srv

    assert srv._SECTION_ALIAS["plugins.guard"] == "permissions"


def test_settings_round_trip_reaches_guard(tmp_path, monkeypatch):
    """★ 端到端：覆盖文件 → ``_merge_cfg`` → 容器 config → ``guard.policy()`` 真读到。

    走过真实设置写入链（``data/agent_plugin_config.json`` 形状是**扁平点号键**），
    断言穿过 ``guard._matrix`` 这个生产读取点。
    """
    import argparse
    import json

    from agent.run import chat as srv

    monkeypatch.setattr(srv, "_OVERRIDE_FILE", tmp_path / "cfg.json")
    srv._OVERRIDE_FILE.write_text(json.dumps({
        "plugins.guard": {"task_deny": ["mcp.*"], "targets": ["*"]},
    }, ensure_ascii=False), encoding="utf-8")

    args = argparse.Namespace(db=str(tmp_path / "a.db"), provider="deepseek",
                              model="deepseek-flash", fixture=False)
    cfg = srv._merge_cfg(args)
    node = (cfg.get("agent") or {}).get("permissions") or {}
    assert node.get("task_deny") == ["mcp.*"], f"特例映射失效：{node}"
    assert "guard" not in (cfg.get("agent") or {}), "不应产生 agent.guard 幽灵节"

    c = build_agent(config=cfg)

    async def s():
        await c.assemble()
        assert c.root.guard.policy(c.root)["task_deny"] == ["mcp.*"]
        await c.teardown()

    _run(s())


def test_setting_keys_rebound_to_guard():
    """★ 四个名单键的归属已是 ``plugins.guard``（不再挂沙箱）。

    同时固化 ``default`` 的取值域改为 ``open``/``closed``——那是 guard 的语义
    （旧沙箱的 ``allow``/``ask``/``deny`` 三值在 guard 里根本不存在）。
    """
    from agent.run import chat as srv

    for k in ("targets", "default", "allow", "deny"):
        assert srv._PLUGIN_OF_KEY[k] == "plugins.guard", f"{k} 归属未改"
    assert srv._SETTING_SCHEMA["default"] == ("choice", ["open", "closed"])
    assert srv._SETTING_DEFAULTS["targets"] == ["*"], "默认域须与 guard.DEFAULT_POLICY 同源"
    assert srv._SETTING_DEFAULTS["default"] == "open"


# ---------------------------------------------------------------------------
# ⑤ LLM 调用闸（LLM_PRE_CALL）—— 高危开关的取值收紧
# ---------------------------------------------------------------------------

def test_llm_gate_only_accepts_declared_values():
    """★ ``permissions.llm`` 的取值域收紧为 ``open`` / ``deny``（2026-10-03）。

    此前实现是 ``"allow" if m.get("llm","open") == "open" else "deny"`` ⇒
    **任何非 open 的字符串**（``"true"`` / ``"1"`` / ``"是"`` / 乱写）都会**静默锁死
    全部模型调用**（记忆提取 / 解说 / 反思全在内），而 ``config_schema`` 的 ``str``
    类型拦不住任何字符串。

    收紧后：只认两个声明值（大小写与空白容错），**未知值回落 open**。
    """
    from agent.sys.guard import _LLM_MODES, _llm_decision

    assert _LLM_MODES == ("open", "deny"), "取值域必须显式且只有两项"

    # 合法值
    assert _llm_decision({"llm": "open"}) == "allow"
    assert _llm_decision({"llm": "deny"}) == "deny"
    # 容错：大小写 + 空白（配置是人手写的）
    assert _llm_decision({"llm": "DENY"}) == "deny"
    assert _llm_decision({"llm": " deny "}) == "deny"
    assert _llm_decision({"llm": "OPEN"}) == "allow"
    # 缺省 = open
    assert _llm_decision({}) == "allow"

    # ★ 误配一律**放行**（fail-open），不再是"锁死全部模型调用"
    for bad in ("true", "1", "是", "on", "off", "乱写", "", "closed", "readonly"):
        assert _llm_decision({"llm": bad}) == "allow", f"误配 {bad!r} 不该锁死模型调用"
    assert _llm_decision({"llm": None}) == "allow"


def test_llm_deny_still_blocks_all_model_calls(tmp_path):
    """★ 收紧**没有**削弱既定能力：显式 ``deny`` 仍然拒掉全部模型调用。"""
    c = build_agent(config=_cfg(tmp_path, {"llm": "deny"}))

    async def s():
        await c.assemble()
        with pytest.raises(CapabilityNotImplemented):
            await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}],
                                      schema={}, caller="t")
        await c.teardown()

    _run(s())


def test_llm_misconfig_does_not_break_llm(tmp_path):
    """★ 端到端：把 ``llm`` 配成 ``"true"``（旧实现会锁死）现在**照常工作**。

    穿过真实生产读取点 ``_decide`` → ``_llm_decision`` → ``LLM_PRE_CALL`` waterfall。
    """
    c = build_agent(config=_cfg(tmp_path, {"llm": "true"}))

    async def s():
        await c.assemble()
        # fixture provider：能返回即说明没被闸门拒掉
        r = await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}],
                                      schema={}, caller="t")
        assert r is not None, "误配不该让模型调用全部失效"
        await c.teardown()

    _run(s())


def test_llm_misconfig_is_counted_not_silent():
    """★ 未知值必须**可观测**（计数），不是静默回落 —— 否则问题无从发现。"""
    from agent.api import counters_snapshot
    from agent.sys.guard import _llm_decision

    name = "agent_guard_llm_mode_invalid_total"
    before = counters_snapshot().get(name, 0.0)
    assert _llm_decision({"llm": "乱写"}) == "allow"
    assert counters_snapshot().get(name, 0.0) == before + 1, "误配未计数（静默回落 = 无从发现）"

    # 合法值**不**计数（计数器的意义是"有异常"，不是"有调用"）
    _llm_decision({"llm": "deny"})
    _llm_decision({"llm": "open"})
    assert counters_snapshot().get(name, 0.0) == before + 1
