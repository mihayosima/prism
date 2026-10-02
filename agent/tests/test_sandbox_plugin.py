"""G7 外部工具准入沙箱（plugins.sandbox）—— 方案《MCP沙箱准入插件-方案-2026-10-03》§6。

覆盖 11 组：
1. 默认放行 + 落审计（真容器 + 真 ``tasks.invoke``，走真实 waterfall）
2. 作用域外不过问（不变成"第二个 guard"）
3. 名称模式匹配（``mcp.windows.*`` 拒 / ``mcp.playwright.*`` 放行）
4. 白名单**不穿透**黑名单
5. risk 覆盖 + **F9 前提断言**（真实 MCP 工具全是 ``write`` 档）
6. 会话级覆盖优先（按会话隔离）
7. 纯函数穷举（``match`` / ``decide`` / ``in_scope``）
8. 拒绝返回体是纯数据（过 ``_assert_pure_data`` 口径）
9. 守卫：默认关 / 不改 MCP 桥与两个 MCP 插件
10. **防"服务起不来"**：带 ``agent.sandbox`` 的配置能装配（schema 登记是硬前提）
11. **严格键集**：``sandbox`` 节写入未知键 ⇒ ``ConfigError``（fail-fast）

★ 第 6 例曾救下一个**会打断所有工具调用**的 bug：``SESSION_CFG_VIEW`` 接缝是
  **可调用对象** ``(section, sk) -> dict``，当字典用（多套一层 ``section_of``）会让
  每次 ``tasks.invoke`` 抛 ``AttributeError``。接入面（会话级覆盖）必须真跑。
"""

from __future__ import annotations

import asyncio
import json
import re

from pathlib import Path

import pytest

from agent.api import (CapabilityNotImplemented, ConfigError, SESSION_CFG_VIEW,
                       coerce_config, set_current_session_key,
                       validate_config)
from agent.plugins.sandbox import DEFAULT_CONFIG, decide, in_scope, match
from agent.profiles import BASE_AGENT_CONFIG, build_agent
from agent.sys.plugin_manager import PluginManager

AGENT_DIR = Path(__file__).resolve().parent.parent
PLUGIN_DIR = AGENT_DIR / "plugins"

MCP_TOOLS = ("mcp.windows.open", "mcp.playwright.nav")


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path, *, enable_sandbox: bool = True) -> PluginManager:
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "plugin_state.json"))
    if enable_sandbox:
        pm.set_enabled("plugins.sandbox", True)
    return pm


def _container(tmp_path, *, enable_sandbox: bool = True, config_agent: dict | None = None):
    agent = {"llm": {"provider": "fixture"}}
    agent.update(config_agent or {})
    return build_agent(config={"db_path": str(tmp_path / "agent.db"), "agent": agent},
                       plugin_mgr=_pm(tmp_path, enable_sandbox=enable_sandbox))


def _register(c, names=MCP_TOOLS, risk: str = "write") -> None:
    async def _h():
        return {"ok": True}

    for n in names:
        c.root.tasks.register(c.root, n, schema={}, handler=_h, risk=risk, owner="test")


def _attach_view(c, sessions: dict) -> None:
    """挂会话配置视图接缝（形状与 ``run/chat.py::_attach_session_cfg_view`` **完全一致**：
    它是**可调用对象**，不是字典——踩过这个坑，见模块 docstring）。"""
    c.root.provide(SESSION_CFG_VIEW,
                   lambda section, sk=None: sessions.get(sk or "", {})
                   if section == "sandbox" else {})


async def _sandbox_rows(c):
    """沙箱审计行（须在事件循环内 await —— db 是异步接口）。"""
    return await c.root.db.query(
        c.root, "SELECT payload FROM agent_events"
                " WHERE event='agent/sandbox/decision' ORDER BY seq")


@pytest.fixture(autouse=True)
def _isolate_session_scope():
    """会话键是 ContextVar ⇒ 用例间必须复位，否则顺着用例泄漏（见 MEMORY §3 纪律）。"""
    set_current_session_key(None)
    yield
    set_current_session_key(None)


# ---------------------------------------------------------------------------
# ① 默认放行 + 审计
# ---------------------------------------------------------------------------

def test_default_allows_and_audits(tmp_path):
    """默认姿态（allow + 审计）：MCP 工具照常通过，但每次裁决都留痕。"""
    c = _container(tmp_path)
    result = {}

    async def s():
        await c.assemble()
        _register(c)
        for n in MCP_TOOLS:
            result[n] = await c.root.tasks.invoke(c.root, n, caller="test")
        rows = await _sandbox_rows(c)
        result["rows"] = [json.loads(r["payload"]) for r in rows]
        await c.teardown()

    _run(s())
    assert all(v == {"ok": True} for k, v in result.items() if k.startswith("mcp")), result
    assert len(result["rows"]) == 2, "每个在作用域内的调用都应留一行审计"
    first = result["rows"][0]
    for key in ("who", "target", "risk", "decision", "by", "session", "reason"):
        assert key in first, f"审计缺字段 {key}: {first}"
    assert first["by"] == "sandbox" and first["decision"] == "allow"


# ---------------------------------------------------------------------------
# ② 作用域外不过问
# ---------------------------------------------------------------------------

def test_out_of_scope_is_untouched(tmp_path):
    """非 ``mcp.*`` 的工具**完全不产生**沙箱审计行（不得变成第二个 guard）。"""
    c = _container(tmp_path)

    async def s():
        await c.assemble()
        _register(c, names=("t.write", "t.read"), risk="write")
        for n in ("t.write", "t.read"):
            assert (await c.root.tasks.invoke(c.root, n, caller="test")) == {"ok": True}
        rows = await _sandbox_rows(c)
        await c.teardown()
        return rows

    assert _run(s()) == [], "作用域外的调用不该留下沙箱审计行"


# ---------------------------------------------------------------------------
# ③ 名称模式匹配
# ---------------------------------------------------------------------------

def test_pattern_match_denies_only_matching_prefix(tmp_path):
    """``deny: ["mcp.windows.*"]`` 只拦该前缀；同前缀之外的桥不受影响。"""
    c = _container(tmp_path, config_agent={"sandbox": {"deny": ["mcp.windows.*"]}})
    got = {}

    async def s():
        await c.assemble()
        _register(c)
        for n in MCP_TOOLS:
            try:
                await c.root.tasks.invoke(c.root, n, caller="test")
                got[n] = "allow"
            except CapabilityNotImplemented as exc:
                got[n] = (exc.denied_by, str(exc))
        await c.teardown()

    _run(s())
    assert got["mcp.windows.open"] != "allow", "命中黑名单的工具未被拒"
    assert got["mcp.windows.open"][0] == "sandbox",         f"拒绝者应为 sandbox（denied_by 属性）：{got['mcp.windows.open']}"
    assert "mcp.windows.*" in got["mcp.windows.open"][1],         f"拒绝理由应点名命中的模式：{got}"
    assert got["mcp.playwright.nav"] == "allow", "非命中前缀被误拒"


# ---------------------------------------------------------------------------
# ④ 白名单不穿透黑名单
# ---------------------------------------------------------------------------

def test_allow_cannot_punch_through_deny(tmp_path):
    """``deny:["mcp.*"] + allow:["mcp.windows.*"]`` ⇒ 仍拒（避免两份配置互相打洞）。"""
    c = _container(tmp_path, config_agent={
        "sandbox": {"deny": ["mcp.*"], "allow": ["mcp.windows.*"]}})

    async def s():
        await c.assemble()
        _register(c, names=("mcp.windows.open",))
        with pytest.raises(Exception) as ei:
            await c.root.tasks.invoke(c.root, "mcp.windows.open", caller="test")
        await c.teardown()
        return str(ei.value)

    msg = _run(s())
    assert "拒绝名单" in msg, msg


# ---------------------------------------------------------------------------
# ⑤ risk 覆盖 + F9 前提断言
# ---------------------------------------------------------------------------

def test_risk_override_and_the_mcp_write_premise(tmp_path):
    """risk 覆盖按档位生效；**并把 F9 钉成断言**：真实 MCP 工具全是 ``write`` 档。

    F9：``Task.risk`` 默认 ``"write"``，而两个 MCP 插件注册时都没传 risk ⇒ 按风险级
    分级对 MCP 只能整体生效。谁将来给 MCP 补了 risk 标注，本例会提醒同步方案文档。
    """
    c = _container(tmp_path, config_agent={
        "sandbox": {"risk": {"write": "deny", "read": "allow"}}})
    got = {}

    async def s():
        await c.assemble()
        _register(c, names=("mcp.windows.open",), risk="write")
        _register(c, names=("mcp.nav",), risk="read")
        for n in ("mcp.windows.open", "mcp.nav"):
            try:
                await c.root.tasks.invoke(c.root, n, caller="test")
                got[n] = "allow"
            except Exception:                            # noqa: BLE001
                got[n] = "deny"
        await c.teardown()

    _run(s())
    assert got == {"mcp.windows.open": "deny", "mcp.nav": "allow"}, got

    for rel in ("plugins/playwright_mcp/__init__.py", "plugins/windows_mcp/__init__.py"):
        src = (AGENT_DIR / rel).read_text(encoding="utf-8")
        calls = re.findall(r"tasks\.register\((?:[^()]|\([^()]*\))*\)", src, re.S)
        assert calls, f"{rel} 未找到注册调用（源码结构变了？）"
        assert not any("risk" in c0 for c0 in calls), (
            f"{rel} 的 tools.register 现在传了 risk ⇒ F9 前提变了，"
            f"请同步更新方案 §1 的 F9 与本用例")


# ---------------------------------------------------------------------------
# ⑥ 会话级覆盖优先
# ---------------------------------------------------------------------------

def test_session_scope_overrides_container(tmp_path):
    """会话级覆盖只作用于**该会话**（本例同时是接缝形状的回归：接缝是可调用对象）。"""
    c = _container(tmp_path)
    got = {}

    async def s():
        await c.assemble()
        _register(c)
        _attach_view(c, {"u:A": {"deny": ["mcp.windows.*"]}})
        for sk in ("u:A", "u:B"):
            set_current_session_key(sk)
            for n in MCP_TOOLS:
                try:
                    await c.root.tasks.invoke(c.root, n, caller="test")
                    got[(sk, n)] = "allow"
                except Exception:                        # noqa: BLE001
                    got[(sk, n)] = "deny"
        await c.teardown()

    _run(s())
    assert got[("u:A", "mcp.windows.open")] == "deny", "会话 A 的拒绝名单未生效"
    assert got[("u:A", "mcp.playwright.nav")] == "allow"
    assert got[("u:B", "mcp.windows.open")] == "allow", "会话 B 被 A 的覆盖牵连了"


def test_view_seam_must_be_called_not_indexed(tmp_path):
    """若把接缝当**字典**用（多套一层 ``section_of``），每次工具调用都会抛错。

    这条是 §6 第 6 例的**针对性回归**：曾经真错，且错法隐蔽（会话级覆盖静默失效
    + 作用域判定前就抛 ⇒ 全面打断）。
    """
    c = _container(tmp_path)

    async def s():
        await c.assemble()
        _register(c, names=("mcp.windows.open",))
        # 故意挂一个**字典**形态的错误接缝 ⇒ 插件必须容错（回落容器级）而不是崩
        c.root.provide(SESSION_CFG_VIEW, {"sandbox": {"deny": ["mcp.*"]}})
        out = await c.root.tasks.invoke(c.root, "mcp.windows.open", caller="test")
        await c.teardown()
        return out

    assert _run(s()) == {"ok": True}, "接缝形状不对时应回落容器配置，而不是打断调用"


# ---------------------------------------------------------------------------
# ⑦ 纯函数穷举
# ---------------------------------------------------------------------------

def test_match_truth_table():
    assert match("*", "anything") is True
    assert match("mcp.*", "mcp.a.b") is True
    assert match("mcp.*", "mcp.") is True
    assert match("mcp.*", "mcpx.a") is False
    assert match("a.b", "a.b") is True
    assert match("a.b", "a.c") is False
    assert match("a.*", "a") is False
    assert match("", "a") is False
    assert match("   ", "a") is False


def test_decide_precedence_and_fail_open():
    # deny 名单优先于 allow（白名单不能穿透黑名单）
    assert decide({"deny": ["mcp.*"], "allow": ["mcp.*"]},
                  target="mcp.x")[0] == "deny"
    # allow 名单优先于 risk 覆盖
    assert decide({"allow": ["mcp.*"], "risk": {"write": "deny"}},
                  target="mcp.x", risk="write")[0] == "allow"
    # risk 覆盖优先于 default
    assert decide({"risk": {"write": "ask"}}, target="mcp.x", risk="write")[0] == "ask"
    # 非法值一律 fail-open（配置写错不该把工具全锁死）
    assert decide({"default": "乱写"}, target="mcp.x")[0] == "allow"
    assert decide({"risk": {"write": "乱写"}}, target="mcp.x", risk="write")[0] == "allow"
    assert decide({}, target="mcp.x") == ("allow", "默认裁决")
    # 理由必须点名命中的模式（排查"为什么被拦"的唯一线索）
    assert "mcp.windows.*" in decide({"deny": ["mcp.windows.*"]},
                                    target="mcp.windows.open")[1]


def test_in_scope_default_targets_only_mcp():
    assert in_scope(DEFAULT_CONFIG, "mcp.x.y") is True
    assert in_scope(DEFAULT_CONFIG, "approval.ask_user") is False
    assert in_scope({"targets": []}, "mcp.x") is False
    assert in_scope({"targets": ["*"]}, "anything") is True


# ---------------------------------------------------------------------------
# ⑧ 拒绝返回体是纯数据
# ---------------------------------------------------------------------------

def test_denial_payload_is_pure_data(tmp_path):
    """拒绝返回体必须过纯数据校验（总线对 payload 有 strict 约束）。"""
    from agent.core.events import _assert_pure_data

    c = _container(tmp_path, config_agent={"sandbox": {"deny": ["mcp.*"]}})

    async def s():
        await c.assemble()
        _register(c, names=("mcp.windows.open",))
        with pytest.raises(Exception) as ei:
            await c.root.tasks.invoke(c.root, "mcp.windows.open", caller="test")
        await c.teardown()
        return str(ei.value)

    msg = _run(s())
    # 还原 tasks.invoke 传给 CapabilityNotImplemented 的那个体做纯数据校验
    body = {"denied": True, "denied_by": "sandbox", "reason": msg}
    _assert_pure_data(body, "sandbox 拒绝体")   # 不抛即通过
    assert body["denied_by"] == "sandbox"


# ---------------------------------------------------------------------------
# ⑨ 守卫
# ---------------------------------------------------------------------------

def test_plugin_is_off_by_default(tmp_path):
    """目录插件默认关：不在 state 文件里就**完全不过问**（零回归的前提）。"""
    c = _container(tmp_path, enable_sandbox=False)

    async def s():
        await c.assemble()
        _register(c, names=("mcp.windows.open",))
        out = await c.root.tasks.invoke(c.root, "mcp.windows.open", caller="test")
        rows = await _sandbox_rows(c)
        await c.teardown()
        return out, rows

    out, rows = _run(s())
    assert out == {"ok": True}
    assert rows == [], "未启用却产生了沙箱审计行（默认关的语义被破坏）"


def test_untouched_surfaces_stay_untouched():
    """守卫：本批**不改** MCP 桥与两个 MCP 插件（注册处一行不动）。"""
    bridge = (AGENT_DIR / "core" / "mcp_client.py").read_text(encoding="utf-8")
    assert "sandbox" not in bridge.lower(), "MCP 桥不应知道沙箱（准入在 waterfall 层）"
    for rel in ("plugins/playwright_mcp/__init__.py", "plugins/windows_mcp/__init__.py"):
        assert "sandbox" not in (AGENT_DIR / rel).read_text(encoding="utf-8").lower(), \
            f"{rel} 不应改动（沙箱在准入层，不进桥）"


# ---------------------------------------------------------------------------
# ⑩⑪ 防"服务起不来" + 严格键集
# ---------------------------------------------------------------------------

def test_config_with_sandbox_section_assembles(tmp_path):
    """★ 防"服务起不来"：写了 ``agent.sandbox`` 的配置**必须能装配**。

    ``_SCHEMA["agent"]`` 是严格字典且 ``validate_config`` 在每次装配都跑 ⇒ 若漏登记
    这一节，用户一写配置就是 ``ConfigError`` ⇒ 服务起不来（同今日已修的迁移陷阱同类）。
    """
    c = _container(tmp_path, config_agent={
        "sandbox": {"targets": ["mcp.*"], "default": "deny", "deny": ["mcp.windows.*"]}})

    async def s():
        await c.assemble()
        ok = c.root is not None
        await c.teardown()
        return ok

    assert _run(s()) is True


def test_sandbox_section_is_strict_not_any():
    """严格键集：拼错键 fail-fast（``_any`` 会静默吞掉配置写错的意图）。"""
    spec = coerce_config({"agent": {"sandbox": {"targts": ["mcp.*"]}}},
                         defaults=BASE_AGENT_CONFIG)
    with pytest.raises(ConfigError) as ei:
        validate_config(spec)
    assert "sandbox" in str(ei.value)
    ok = coerce_config({"agent": {"sandbox": {
        "targets": ["mcp.*"], "default": "allow", "allow": [],
        "deny": [], "risk": {"write": "ask"}}}}, defaults=BASE_AGENT_CONFIG)
    validate_config(ok)          # 五个键齐备 ⇒ 不抛


def test_plugin_defaults_match_settings_defaults():
    """设置页展示的默认值必须与插件的 ``DEFAULT_POLICY`` 一致（两处默认值不许漂移）。"""
    from agent.run.chat import _PLUGIN_OF_KEY, _SETTING_DEFAULTS

    for k in ("targets", "default", "allow", "deny"):
        assert _SETTING_DEFAULTS[k] == DEFAULT_CONFIG[k], (
            f"设置页默认 {_SETTING_DEFAULTS[k]!r} 与插件默认 {DEFAULT_CONFIG[k]!r} 不一致（键 {k}）")
        assert _PLUGIN_OF_KEY[k] == "plugins.sandbox", f"键 {k} 的归属插件不对"
