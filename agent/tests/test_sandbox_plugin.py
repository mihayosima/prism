"""G7 外部工具隔离档位自述（plugins.sandbox）—— 2026-10-03 转型后的行为契约。

转型前本插件是「准入网关 + 审计」，转型后**只做隔离档位自述**（准入判定归 guard，
问人归 approval）。详见 ``docs/reform of production line/沙箱与权限管理器重叠核定-2026-10-03.md``。

覆盖 11 组（★ 标的都是转型后**新增或改写**的）：
1. 恒放行 + 落 ``agent/sandbox/confinement`` 审计（真容器 + 真 ``tasks.invoke``，真实 waterfall）
2. 作用域外不过问（不变成"第二个 guard"）
3. ★ 模式匹配（``match`` 仍在本插件用于 ``targets``；**准入判定**已迁 guard）
4. ★ 废弃键**不再有裁决效果**，但会被检出并计数（迁移期可见，不静默）
5. ★ ``describe()`` 穷举：非法 mode 回落、``gap`` 标记落差、``enforcement`` **不谎报**
6. 会话级覆盖优先（按会话隔离）+ ★ ``mode`` 可会话级切换
7. 纯函数穷举（``match`` / ``describe`` / ``in_scope``）
8. ★ 审计载荷是纯数据（过 ``_assert_pure_data``）且字段齐全
9. 守卫：默认关 / 不改 MCP 桥与两个 MCP 插件
10. **防"服务起不来"**：带 ``agent.sandbox`` 的配置能装配（schema 登记是硬前提）
11. **严格键集**：``sandbox`` 节写入未知键 ⇒ ``ConfigError``（fail-fast）

★ **本插件恒放行**（转型后不再拥有准入裁决权）—— 用例 ① 直接断言这一点。
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
from agent.plugins.sandbox import (DEFAULT_CONFIG, ENFORCEMENTS, MODES, describe,
                                in_scope, match, workspace_for)
from agent.profiles import BASE_AGENT_CONFIG, build_agent
from agent.sys.plugin_manager import PluginManager

AGENT_DIR = Path(__file__).resolve().parent.parent
PLUGIN_DIR = AGENT_DIR / "plugins"

MCP_TOOLS = ("mcp.windows.open", "mcp.playwright.nav")


def _expected_enforcement() -> str:
    """**真实后端**驱动下的强度（本机有 WSL2+bwrap ⇒ partial；否则 none）。

    P4 落地 wsl2 后，声明了隔离档位就**真的**可能兑现 ⇒ 断言不能再写死 "none"，
    否则等于把"有隔离"谎报成"没隔离"。两种环境都靠这个函数保持诚实。
    """
    from agent.core.sandbox_backend import Wsl2Backend
    return "partial" if Wsl2Backend().capability_probe().get("available") else "none"


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
    """沙箱隔离自述行（须在事件循环内 await —— db 是异步接口）。"""
    return await c.root.db.query(
        c.root, "SELECT payload FROM agent_events"
                " WHERE event='agent/sandbox/confinement' ORDER BY seq")


# ---------------------------------------------------------------------------
# ① 默认放行 + 审计
# ---------------------------------------------------------------------------

def test_always_allows_and_reports_confinement(tmp_path):
    """★ 转型核心契约：本插件**恒放行**，只落隔离自述行。

    转型前这里断言 ``decision == "allow"``；转型后本插件**不再有准入裁决权**，
    所以断言对象换成"调用成功 + 审计记录档位与强度"。
    """
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
    assert len(result["rows"]) == 2, "每个在作用域内的调用都应留一行隔离自述"
    first = result["rows"][0]
    for key in ("who", "target", "risk", "by", "session", "reason",
                "mode", "enforcement", "ceiling", "gap", "enforced_by", "bridge",
                "deprecated_used"):
        assert key in first, f"隔离自述缺字段 {key}: {first}"
    assert first["by"] == "sandbox"
    # 默认档位 none ⇒ 强度 none（**不谎报**）
    assert first["mode"] == "none" and first["enforcement"] == "none"
    assert first["gap"] is False, "mode=none 时不该报落差"
    # 审计里**不再有** decision 字段（裁决权已交还 guard）
    assert "decision" not in first, "本插件不该再产裁决结果"


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

def test_targets_narrows_which_bridges_are_reported(tmp_path):
    """★ ``targets`` 收窄后，只有命中的桥产生隔离自述行（域外连审计都不落）。

    转型前这里断言"``deny: mcp.windows.*`` 只拦该前缀"；转型后**准入判定归 guard**，
    本插件只管"自述哪些桥"。模式匹配能力本身（``match``）仍在本插件用于 ``targets``。
    """
    c = _container(tmp_path, config_agent={
        "sandbox": {"targets": ["mcp.windows.*"]}})
    got = {}

    async def s():
        await c.assemble()
        _register(c)
        for n in MCP_TOOLS:
            got[n] = await c.root.tasks.invoke(c.root, n, caller="test")
        rows = [json.loads(r["payload"]) for r in await _sandbox_rows(c)]
        await c.teardown()
        return rows

    rows = _run(s())
    assert all(v == {"ok": True} for v in got.values()), "本插件恒放行"
    assert [r["target"] for r in rows] == ["mcp.windows.open"], rows
    assert rows[0]["bridge"] == "windows", "审计须带桥名才追得到是哪个桥没隔离"


# ---------------------------------------------------------------------------
# ④ 白名单不穿透黑名单
# ---------------------------------------------------------------------------

def test_deprecated_keys_have_no_decision_effect(tmp_path):
    """★ 迁移期语义：旧四键（``deny``/``allow``/``default``/``risk``）**不再有任何裁决效果**。

    这条替代转型前的"白名单不穿透黑名单"用例。契约从"黑名单优先"变成
    "**这两个键在准入上完全无效**"—— 准入归 ``sys/guard.py``。
    但它们**仍会被检出**（``deprecated_used``）⇒ 迁移期可见，不静默。
    """
    c = _container(tmp_path, config_agent={
        "sandbox": {"deny": ["mcp.*"], "allow": ["mcp.*"], "default": "deny"}})

    async def s():
        await c.assemble()
        _register(c, names=("mcp.windows.open",))
        out = await c.root.tasks.invoke(c.root, "mcp.windows.open", caller="test")
        rows = [json.loads(r["payload"]) for r in await _sandbox_rows(c)]
        await c.teardown()
        return out, rows

    out, rows = _run(s())
    assert out == {"ok": True}, "废弃键不得再有裁决效果（deny 不该拦得住）"
    assert rows and set(rows[0]["deprecated_used"]) == {"deny", "allow", "default"}, (
        f"废弃键未被检出，迁移期不可见：{rows[0]}")


# ---------------------------------------------------------------------------
# ⑤ risk 覆盖 + F9 前提断言
# ---------------------------------------------------------------------------

def test_describe_truth_table_and_f9_premise():
    """★ ``describe()`` 穷举：非法 mode 回落、``gap`` 标记落差、``enforcement`` **不谎报**。

    这条替代转型前的 ``risk`` 覆盖用例（risk 裁决已归 guard）。核心断言是
    **"配置了档位但兑现不了"必须被标成落差** —— 报成 ``full`` 才是缺陷。
    """
    # 默认：无档位 ⇒ 无落差
    d = describe(DEFAULT_CONFIG)
    assert d["mode"] == "none" and d["enforcement"] == "none" and d["gap"] is False

    # 声明了档位但**本插件无任何进程级隔离** ⇒ enforcement 必须是 none + gap=True
    for m in ("read-only", "workspace-write"):
        d = describe({**DEFAULT_CONFIG, "mode": m})
        assert d["mode"] == m
        assert d["enforcement"] == "none", f"{m} 谎报了隔离强度（当前无隔离实现）"
        assert d["ceiling"] == "none"
        assert d["gap"] is True, f"{m} 配置了却兑现不了 ⇒ 落差必须可见"
        assert "未实现" in d["reason"], d["reason"]

    # 非法 mode 回落 none（配置写错不该让服务起不来）
    assert describe({**DEFAULT_CONFIG, "mode": "乱写"})["mode"] == "none"

    # 取值域固化：强度只能是声明的三个值
    for m in MODES:
        assert describe({**DEFAULT_CONFIG, "mode": m})["enforcement"] in ENFORCEMENTS

    # ★ F9 前提断言（保留）：真实 MCP 工具全是 ``write`` 档。
    # 这条前提是"按 risk 分级对 MCP 无效"的根因，仍需钉住。
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
    """★ 会话级 ``mode`` 覆盖只作用于**该会话**（本例同时是接缝形状的回归：接缝是可调用对象）。

    转型后本插件恒放行，所以"覆盖生效"体现在**审计行里的 mode**，
    而不再是"放行/拒绝"—— 这也更贴近 DSH（档位是逐调用解析的，不是进程级一次性设置）。
    """
    c = _container(tmp_path)
    got = {}

    async def s():
        await c.assemble()
        _register(c, names=("mcp.windows.open",))
        _attach_view(c, {"u:A": {"mode": "workspace-write"}})
        for sk in ("u:A", "u:B"):
            set_current_session_key(sk)
            await c.root.tasks.invoke(c.root, "mcp.windows.open", caller="test")
            rows = [json.loads(r["payload"]) for r in await _sandbox_rows(c)]
            got[sk] = [r["mode"] for r in rows if r["session"] == sk]
        await c.teardown()

    _run(s())
    assert got["u:A"] == ["workspace-write"], f"会话 A 的档位未生效：{got}"
    assert got["u:B"] == ["none"], f"会话 B 被 A 的覆盖牵连了：{got}"


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


def test_describe_never_claims_unearned_isolation():
    """★ ``describe()`` 的**诚实性**契约（转型后最关键的一条）。

    这条替代转型前的 ``decide`` 优先级穷举：裁决优先级不再属于本插件，
    但"绝不谎报隔离强度"成了本插件**唯一的核心义务**。
    """
    # 逐档穷举：声明了什么，都不能报出兑现不了的强度
    for m in MODES:
        d = describe({**DEFAULT_CONFIG, "mode": m})
        if m == "none":
            assert d["enforcement"] == "none" and d["gap"] is False
        else:
            # 声明了但无实现 ⇒ 强度必须为 none 且落差可见
            assert d["enforcement"] == "none", f"{m} 谎报强度"
            assert d["gap"] is True, f"{m} 的落差被隐藏了"

    # 理由必须能解释"为什么没隔离"（排查"为什么没保护"的唯一线索）
    d = describe({**DEFAULT_CONFIG, "mode": "workspace-write"})
    # 理由必须**同时**说清"声明了什么"与"实际是什么"（排查"为什么没保护"的唯一线索）
    assert "workspace-write" in d["reason"], d["reason"]
    assert "未实现" in d["reason"] and "无隔离" in d["reason"], d["reason"]

    # 废弃键检出：空/默认值不算"在用"，非空才算
    assert describe(DEFAULT_CONFIG)["deprecated_used"] == []
    assert describe({**DEFAULT_CONFIG, "deny": []})["deprecated_used"] == []
    assert describe({**DEFAULT_CONFIG, "deny": ["mcp.*"]})["deprecated_used"] == ["deny"]


def test_in_scope_default_targets_only_mcp():
    assert in_scope(DEFAULT_CONFIG, "mcp.x.y") is True
    assert in_scope(DEFAULT_CONFIG, "approval.ask_user") is False
    assert in_scope({"targets": []}, "mcp.x") is False
    assert in_scope({"targets": ["*"]}, "anything") is True


# ---------------------------------------------------------------------------
# ⑧ 拒绝返回体是纯数据
# ---------------------------------------------------------------------------

def test_confinement_payload_is_pure_data(tmp_path):
    """★ 隔离自述载荷必须过纯数据校验（durable 写入 ``agent_events``，有 strict 约束）。

    这条替代转型前的"拒绝体纯数据"：转型后本插件不再短路，
    但审计载荷**更值得守** —— 它是插件页与事件视图的唯一数据源，
    混进不可序列化对象会直接让整条审计落库失败。
    """
    from agent.core.events import _assert_pure_data

    c = _container(tmp_path, config_agent={
        "sandbox": {"mode": "workspace-write", "targets": ["mcp.*"]}})

    async def s():
        await c.assemble()
        _register(c, names=("mcp.windows.open",))
        await c.root.tasks.invoke(c.root, "mcp.windows.open", caller="test")
        rows = await _sandbox_rows(c)
        await c.teardown()
        return [json.loads(r["payload"]) for r in rows]

    rows = _run(s())
    assert rows, "隔离自述行未产生"
    _assert_pure_data(rows[0], "sandbox 隔离自述体")   # 不抛即通过
    # 布尔字段必须是真 bool（JSON 往返后仍是 bool，不是 "true" 字符串）
    assert isinstance(rows[0]["gap"], bool)
    assert isinstance(rows[0]["enforcement"], str)


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
    """守卫：桥**不参与准入裁决**（裁决权在 guard/approval），两个 MCP 插件一行不动。

    ★ P4 修订：跨边界隔离（WSL2/容器）必须作用在**命令本身**，光给 cwd/env 挡不住
    ⇒ ``core/mcp_client.py`` 现在会引用 ``apply_wrap``（**只应用包装、不做判断**）。
    这是机制层面的必要耦合；被守住的边界是"**裁决**不进桥"——桥不为允许/拒绝负责。
    """
    bridge = (AGENT_DIR / "core" / "mcp_client.py").read_text(encoding="utf-8")
    assert "apply_wrap" in bridge, "跨边界隔离必须作用在命令上（P4：光 cwd/env 挡不住）"
    # 守住的边界是"**裁决**不进桥"：选择哪个后端、是否允许调用，都是插件/guard 的活。
    for banned in ("select_backend", "describe_backend", "decision", "risk"):
        assert banned not in bridge, f"MCP 桥不得做后端选择/准入裁决：{banned}"
    for rel in ("plugins/playwright_mcp/__init__.py", "plugins/windows_mcp/__init__.py"):
        assert "sandbox" not in (AGENT_DIR / rel).read_text(encoding="utf-8").lower(), \
            f"{rel} 不应改动（沙箱在准入层，不进桥）"


# ---------------------------------------------------------------------------
# ⑫ 落点收拢（2026-10-04）：把外部子进程的写入落点挪出项目根
# ---------------------------------------------------------------------------
# 动机（可验证的事实，不是感觉）：``core/mcp_client.py`` spawn 时不设 cwd ⇒ 子进程
# 继承宿主 cwd = 项目根，那里有 ``local_secrets.json`` 与 ``data/*.db``。

def test_workspace_for_is_opt_in_default_off():
    """``mode="none"`` ⇒ **不重定向**（保持现状，不劣化）。这是 opt-in 的硬保证。"""
    assert workspace_for({"mode": "none"}) is None
    assert workspace_for({}) is None
    assert workspace_for({"mode": "瞎写的档位"}) is None      # 非法档位回落 none
    assert workspace_for({"targets": ["mcp.*"]}) is None       # 未声明档位


def test_workspace_for_redirects_known_cache_dirs():
    """声明档位 ⇒ 给出 cwd + 一整套缓存/临时目录重定向。"""
    for mode in ("read-only", "workspace-write"):
        ws = workspace_for({"mode": mode}, "u:A")
        assert ws is not None, f"{mode} 应给出落点"
        cwd = ws["cwd"]
        assert cwd.startswith("data/sandbox/u_A-"), f"{cwd} 应落在根下且带会话前缀"
        assert ws["env"]["TMP"] == cwd and ws["env"]["TEMP"] == cwd
        # 两个桥各自的缓存家目录必须被收进去（否则 npx/uvx 仍往宿主家目录写）
        assert ws["env"]["npm_config_cache"] == cwd
        assert ws["env"]["UV_CACHE_DIR"] == cwd
        assert ws["env"]["PLAYWRIGHT_BROWSERS_PATH"] == cwd


def test_workspace_seg_is_path_traversal_safe():
    """会话键进路径前必须净化：``..`` 不得原样保留（否则逃出根目录）。"""
    for bad in ("../../etc", "..", ".", "a/../b"):
        ws = workspace_for({"mode": "workspace-write"}, bad)
        assert ws is not None
        seg = ws["cwd"].split("/")[-1]
        assert ".." not in seg, f"{bad!r} 产生了可穿越的目录段 {seg!r}"
        assert "." not in seg, f"{bad!r} 目录段仍含点 {seg!r}"


def test_workspace_segments_do_not_collide():
    """★ 净化不可逆（``.``/``:`` 都被替换）⇒ 必须靠哈希后缀防碰撞。

    碰撞的后果是**两个不同会话落进同一个目录** —— 那正是本次要修的串味，
    只是从"记忆串味"换成"文件落点串味"。
    """
    a = workspace_for({"mode": "workspace-write"}, "u.a")["cwd"]
    b = workspace_for({"mode": "workspace-write"}, "u_a")["cwd"]
    c = workspace_for({"mode": "workspace-write"}, "u:a")["cwd"]
    assert len({a, b, c}) == 3, f"落点碰撞：{a} / {b} / {c}"


def test_confinement_audit_carries_workspace(tmp_path):
    """审计行带 ``cwd`` / ``env_redirected`` ⇒ 「东西落在哪」可查。

    默认档位下两者为空/False（不谎报成已收拢）。
    """
    c = _container(tmp_path, config_agent={"sandbox": {"mode": "workspace-write"}})

    async def s():
        await c.assemble()
        set_current_session_key("u:A")
        _register(c, names=("mcp.windows.open",))
        await c.root.tasks.invoke(c.root, "mcp.windows.open", caller="test")
        rows = await _sandbox_rows(c)
        await c.teardown()
        return rows

    rows = _run(s())
    assert rows, "应落一条隔离自述"
    payload = json.loads(rows[0]["payload"])
    assert payload["cwd"].startswith("data/sandbox/u_A-")
    assert payload["env_redirected"] is True
    # ★ 关键：落点收拢**不是**强制 —— enforcement 不得因此抬升（抬了就是谎报）
    expected = _expected_enforcement()
    assert payload["enforcement"] == expected, "强度必须由后端真实能力驱动"
    assert payload["gap"] is (expected == "none")


def test_audit_workspace_empty_when_mode_none(tmp_path):
    """未声明档位 ⇒ 审计里 cwd 为空、env_redirected=False（诚实）。"""
    c = _container(tmp_path)

    async def s():
        await c.assemble()
        _register(c, names=("mcp.windows.open",))
        await c.root.tasks.invoke(c.root, "mcp.windows.open", caller="test")
        rows = await _sandbox_rows(c)
        await c.teardown()
        return rows

    rows = _run(s())
    payload = json.loads(rows[0]["payload"])
    assert payload["cwd"] == ""
    assert payload["env_redirected"] is False


def test_status_reports_workspace(tmp_path):
    """``status()`` 暴露落点 ⇒ 插件页可展示"当前是否把子进程挪出项目根"。"""
    c = _container(tmp_path, config_agent={"sandbox": {"mode": "read-only"}})

    async def s():
        await c.assemble()
        set_current_session_key("u:B")
        st = {s["id"]: s["status"] for s in await c.plugin_statuses()}
        await c.teardown()
        return st

    st = _run(s())
    sb = st.get("plugins.sandbox") or {}
    assert (sb.get("workspace") or "").startswith("data/sandbox/u_B-")
    assert sb.get("env_redirected") is True
    # ★ P4 后：声明了档位且本机 wsl2 可用 ⇒ 真的兑现了 partial；不可得才回落 none。
    # 两种都属"不谎报"，关键是**由真实后端能力驱动**，不是硬编码。
    expected = _expected_enforcement()
    assert sb.get("enforcement") == expected, "强度必须由后端真实能力驱动"
    assert bool(sb.get("gap")) is (expected == "none")


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
    """设置页展示的默认值必须与**策略唯一持有者**的默认值一致（两处默认值不许漂移）。

    ★ 2026-10-03 策略收口后，这四个键的归属插件从 ``plugins.sandbox`` 改为
    ``plugins.guard``（``run/chat.py::_PLUGIN_OF_KEY``）—— 因此**同源对象也换了**：
    现在必须对齐 ``sys.guard.DEFAULT_POLICY``，而不是本插件的 ``DEFAULT_CONFIG``。

    沙箱侧仍保留同名四键（``DEFAULT_CONFIG``）是为了**向后兼容既有配置**，它们已标废弃；
    但设置页不再展示沙箱的默认值 ⇒ 本用例断言"设置页 ↔ guard"，不是"设置页 ↔ 沙箱"。
    """
    from agent.run.chat import _PLUGIN_OF_KEY, _SETTING_DEFAULTS
    from agent.sys.guard import DEFAULT_POLICY as GUARD_POLICY

    # 设置页归属已是 guard
    for k in ("targets", "default", "allow", "deny"):
        assert _PLUGIN_OF_KEY[k] == "plugins.guard", f"键 {k} 的归属插件不对"

    # 设置页默认值与 guard 的 SSOT 同源（防两处漂移）
    assert _SETTING_DEFAULTS["targets"] == GUARD_POLICY["targets"]
    assert _SETTING_DEFAULTS["default"] == GUARD_POLICY["default"]
    assert _SETTING_DEFAULTS["allow"] == GUARD_POLICY["task_allow"]
    assert _SETTING_DEFAULTS["deny"] == GUARD_POLICY["task_deny"]
