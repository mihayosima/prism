"""plugins.approval 第 1 期验收测试（设计稿 v1.2 §12 第 1 期用例）。

覆盖：默认配置零回归 · 三态判定/通配符 · auto-deny fail-safe · auto-allow 审计 ·
guard 硬否决优先（行为级锁定挂点顺序）· 模式（auto/plan/bypass）· 待答表
（超时/去重/resolve/关闭）· 卸载零残留 · 插件管理器发现与加载。
"""

from __future__ import annotations

import asyncio

import pytest

from agent.api import AgentError, CapabilityNotImplemented, Events, StateKeys
from agent.plugins.approval import Approval
from agent.plugins.approval.pending import PendingTable
from agent.plugins.approval.policy import decide
from agent.plugins.react.components import ReasonOutput, ToolCall, ToolResult
from agent.plugins.react.loop import ReactConfig, ReactLoop, visible_tools
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager


def _run(coro):
    return asyncio.run(coro)


def _cfg(tmp_path, approval=None, permissions=None):
    agent: dict = {}
    if approval is not None:
        agent["approval"] = approval
    if permissions is not None:
        agent["permissions"] = permissions
    cfg: dict = {"db_path": str(tmp_path / "agent.db")}
    if agent:
        cfg["agent"] = agent
    return cfg


def _build(tmp_path, approval=None, permissions=None):
    return build_agent(config=_cfg(tmp_path, approval, permissions),
                       extra_plugins=(Approval,))


def _register(c):
    async def h():
        return {"ok": True}

    c.root.tasks.register(c.root, "t.read", schema={}, handler=h, risk="read", owner=None)
    c.root.tasks.register(c.root, "t.write", schema={}, handler=h, risk="write", owner=None)
    c.root.tasks.register(c.root, "t.danger", schema={}, handler=h, risk="danger", owner=None)


async def _events(c, name: str) -> list[dict]:
    """读取 durable 审计事件（须在事件循环内 await 调用）。"""
    return await c.root.db.query(
        c.root, "SELECT payload FROM agent_events WHERE event=? ORDER BY seq",
        (name,))


# ---------------------------------------------------------------------------
# policy 纯函数：三态 + 通配符 + 模式
# ---------------------------------------------------------------------------

def test_policy_tri_state_order_and_wildcards():
    rules = {"deny": ["mcp.windows.PowerShell"], "ask": ["mcp.windows.*"],
             "allow": ["t.*"], "default": "deny"}
    assert decide(rules, "mcp.windows.PowerShell") == "deny"      # deny 优先于 ask
    assert decide(rules, "mcp.windows.Registry") == "ask"         # 通配命中 ask
    assert decide(rules, "t.write") == "allow"                    # 名单放行
    assert decide(rules, "unknown.tool") == "deny"                # 未命中 → default 拒
    assert decide({"default": "deny"}, "any.tool") == "deny"      # fail-safe 缺省


def test_policy_modes():
    rules = {"allow": ["*"]}
    assert decide(rules, "t.write", mode="plan", risk="write") == "deny"   # plan 硬兜底
    assert decide(rules, "t.read", mode="plan", risk="read") == "allow"    # 只读放行
    assert decide(rules, "t.write", mode="auto", risk="write") == "ask"    # auto 写类要问
    assert decide(rules, "t.read", mode="auto", risk="read") == "allow"    # auto 只读不问
    assert decide(rules, "t.write", mode="bypass", risk="danger") == "allow"  # bypass 不问


# ---------------------------------------------------------------------------
# 装配行为：默认零回归 / ask / 审计 / guard 优先
# ---------------------------------------------------------------------------

def test_default_config_zero_regression(tmp_path):
    """默认配置（allow=["*"], ask=[]）→ 纯透明放行，行为与无 approval 完全一致。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        _register(c)
        for name in ("t.read", "t.write", "t.danger"):
            assert (await c.root.tasks.invoke(c.root, name, caller="t")) == {"ok": True}
        assert await _events(c, "agent/approval/asked") == []
        await c.teardown()

    _run(s())


def test_ask_auto_deny_failsafe(tmp_path):
    """ask 命中 + auto-deny → 拒绝（fail-safe）；asked/answered 落审计。"""
    c = _build(tmp_path, approval={"ask": ["t.write"], "provider": "auto-deny"})

    async def s():
        await c.assemble()
        _register(c)
        assert (await c.root.tasks.invoke(c.root, "t.read", caller="t")) == {"ok": True}
        try:
            await c.root.tasks.invoke(c.root, "t.write", caller="t")
            raise AssertionError("auto-deny 下 ask 应被拒")
        except Exception as exc:
            assert "未获批准" in str(exc)
        asked = await _events(c, "agent/approval/asked")
        answered = await _events(c, "agent/approval/answered")
        assert len(asked) == 1 and '"t.write"' in asked[0]["payload"]
        assert len(answered) == 1 and '"approved": false' in answered[0]["payload"].lower()
        await c.teardown()

    _run(s())


def test_ask_auto_allow_records_audit(tmp_path):
    """auto-allow：放行，但 asked/answered 审计照记（可追责）。"""
    c = _build(tmp_path, approval={"ask": ["t.*"], "provider": "auto-allow"})

    async def s():
        await c.assemble()
        _register(c)
        assert (await c.root.tasks.invoke(c.root, "t.write", caller="t")) == {"ok": True}
        answered = await _events(c, "agent/approval/answered")
        assert len(answered) == 1 and '"approved": true' in answered[0]["payload"].lower()
        assert '"auto-allow"' in answered[0]["payload"]
        await c.teardown()

    _run(s())


def test_policy_deny_list(tmp_path):
    """deny 名单一票否决（guard 默认 open，拒绝来自 approval）。"""
    c = _build(tmp_path, approval={"deny": ["t.danger"], "provider": "auto-allow"})

    async def s():
        await c.assemble()
        _register(c)
        assert (await c.root.tasks.invoke(c.root, "t.read", caller="t")) == {"ok": True}
        try:
            await c.root.tasks.invoke(c.root, "t.danger", caller="t")
            raise AssertionError("deny 名单应拒绝")
        except Exception as exc:
            assert "approval" in str(exc) and "deny" in str(exc)
        await c.teardown()

    _run(s())


def test_unmatched_default_deny(tmp_path):
    """收窄名单（allow=[]）→ 未命中任务一律拒绝（fail-safe 缺省）。"""
    c = _build(tmp_path, approval={"allow": []})

    async def s():
        await c.assemble()
        _register(c)
        for name in ("t.read", "t.write"):
            try:
                await c.root.tasks.invoke(c.root, name, caller="t")
                raise AssertionError(f"{name} 未命中名单应被拒")
            except Exception as exc:
                assert "approval" in str(exc)
        await c.teardown()

    _run(s())


def test_guard_deny_beats_approval_ask(tmp_path):
    """挂点顺序行为级锁定：guard 硬否决短路 ⇒ approval 不被问、无 asked 审计。"""
    c = _build(tmp_path,
               approval={"ask": ["*"], "provider": "auto-allow"},
               permissions={"task_deny": ["t.write"]})

    async def s():
        await c.assemble()
        _register(c)
        try:
            await c.root.tasks.invoke(c.root, "t.write", caller="t")
            raise AssertionError("guard task_deny 应先拒绝")
        except Exception as exc:
            assert "guard" in str(exc) or "未授权" in str(exc)
        assert await _events(c, "agent/approval/asked") == [], \
            "guard 硬否决的任务不应触发问人（AC-7）"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 模式（mode 的唯一持有者）
# ---------------------------------------------------------------------------

def test_mode_auto_asks_and_plan_denies(tmp_path):
    c = _build(tmp_path, approval={"provider": "auto-deny"})

    async def s():
        await c.assemble()
        _register(c)
        await c.root.approval.set_mode(c.root, "auto", by="test")
        assert (await c.root.tasks.invoke(c.root, "t.read", caller="t")) == {"ok": True}
        try:                                   # auto：写类要问 → auto-deny 拒绝
            await c.root.tasks.invoke(c.root, "t.write", caller="t")
            raise AssertionError("auto 模式下写类应触发问人并被 auto-deny 拒绝")
        except Exception as exc:
            assert "未获批准" in str(exc)
        await c.root.approval.set_mode(c.root, "plan", by="test")
        try:                                   # plan：非只读硬拒
            await c.root.tasks.invoke(c.root, "t.danger", caller="t")
            raise AssertionError("plan 模式下非只读应被拒")
        except Exception as exc:
            assert "plan" in str(exc)
        await c.teardown()

    _run(s())


def test_mode_bypass_skips_asking(tmp_path):
    c = _build(tmp_path, approval={"ask": ["*"], "provider": "auto-deny"})

    async def s():
        await c.assemble()
        _register(c)
        await c.root.approval.set_mode(c.root, "bypass", by="test")
        assert (await c.root.tasks.invoke(c.root, "t.write", caller="t")) == {"ok": True}
        assert await _events(c, "agent/approval/asked") == []
        await c.teardown()

    _run(s())


def test_set_mode_audit_and_validation(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        try:
            await c.root.approval.set_mode(c.root, "bogus")
            raise AssertionError("非法 mode 应报错")
        except AgentError:
            pass
        await c.root.approval.set_mode(c.root, "auto", by="tester")
        changed = await _events(c, "agent/approval/mode_changed")
        assert len(changed) == 1 and '"auto"' in changed[0]["payload"]
        assert c.root.approval.mode() == "auto"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 待答表（交互式 provider 基座）
# ---------------------------------------------------------------------------

def test_pending_timeout_failsafe():
    async def s():
        table = PendingTable(timeout_s=0.05)
        ans, call_id = await table.wait("t.write:{}", {"tool": "t.write"})
        assert ans == {"approved": False, "by": "timeout"} and call_id
        assert table.size() == 0

    _run(s())


def test_pending_resolve_and_dedup():
    async def s():
        table = PendingTable(timeout_s=5.0)
        t1 = asyncio.ensure_future(table.wait("t.write:{}", {"tool": "t.write"}))
        t2 = asyncio.ensure_future(table.wait("t.write:{}", {"tool": "t.write"}))
        await asyncio.sleep(0.01)
        assert table.size() == 1, "同 key 在途应去重合并"
        call_id = list(table._by_call.keys())[0]
        assert table.resolve(call_id, {"approved": True, "by": "web"})
        ans1, cid1 = await t1
        ans2, cid2 = await t2
        assert cid1 == cid2, "去重合并的等待者应共享同一 call_id"
        assert ans1 == ans2 == {"approved": True, "by": "web"}
        assert table.size() == 0

    _run(s())


def test_pending_resolve_by_call_id():
    async def s():
        table = PendingTable(timeout_s=5.0)
        waiter = asyncio.ensure_future(table.wait("k", {"tool": "t"}))
        await asyncio.sleep(0.01)
        # 从在途表反查 call_id（模拟通道侧拿 pending 信号后应答）
        entry_call = list(table._by_call.keys())[0]
        assert table.resolve(entry_call, {"approved": True, "by": "web"})
        assert not table.resolve(entry_call, {"approved": False}), "重复应答幂等"
        ans, _ = await waiter
        assert ans == {"approved": True, "by": "web"}

    _run(s())


def test_pending_aclose_denies_waiters():
    async def s():
        table = PendingTable(timeout_s=30.0)
        waiter = asyncio.ensure_future(table.wait("k", {"tool": "t"}))
        await asyncio.sleep(0.01)
        await table.aclose()
        ans, _ = await waiter
        assert ans == {"approved": False, "by": "shutdown"}
        assert table.size() == 0

    _run(s())


# ---------------------------------------------------------------------------
# 可管理性
# ---------------------------------------------------------------------------

def test_teardown_no_residue(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        assert c.root.has("approval")
        await c.teardown()
        assert not c.root.has("approval"), "卸载零残留（AC-14）"

    _run(s())


def test_status_reports_mode_and_provider(tmp_path):
    c = _build(tmp_path, approval={"ask": ["a.*"], "provider": "auto-allow"})

    async def s():
        await c.assemble()
        statuses = {d["id"]: d["status"] for d in await c.plugin_statuses()}
        st = statuses["plugins.approval"]
        assert st["mode"] == "default" and st["provider"] == "auto-allow"
        assert st["ask_rules"] == 1 and st["pending"] == 0
        await c.teardown()

    _run(s())


def test_plugin_manager_discovers_and_loads(tmp_path):
    """PluginManager 路径：发现 + 期望态启用 + load 产出（双命名空间语义下按 id 断言）。"""
    pm = PluginManager(plugin_dir="agent/plugins", state_file=str(tmp_path / "state.json"))
    assert any(d["name"] == "plugins.approval" and d["loadable"]
               for d in pm.discovered())
    pm.set_enabled("plugins.approval", True)
    loaded = pm.load()
    assert [p.id for p in loaded] == ["plugins.approval"]


# ---------------------------------------------------------------------------
# 第 2 期：web 交互式 provider（弹窗信号 + 入站应答 + fail-safe）
# ---------------------------------------------------------------------------

def test_pending_wait_explicit_call_id_and_snapshot():
    """wait 透传 call_id（弹窗信号与应答同标识）；snapshot 提供在途快照。"""
    async def s():
        table = PendingTable(timeout_s=5.0)
        waiter = asyncio.ensure_future(table.wait("k", {"tool": "t"}, call_id="fixed-cid"))
        await asyncio.sleep(0.01)
        snap = table.snapshot()
        assert len(snap) == 1 and snap[0]["call_id"] == "fixed-cid"
        assert snap[0]["req"] == {"tool": "t"}
        assert table.resolve("fixed-cid", {"approved": True, "by": "user"})
        ans, cid = await waiter
        assert ans == {"approved": True, "by": "user"} and cid == "fixed-cid"

    _run(s())


def test_build_provider_web_without_table_falls_back():
    """web 无待答表 / 未知名字 → fail-safe 落 auto-deny（不裸崩）。"""
    from agent.plugins.approval.providers import build_provider
    assert build_provider("web").name == "auto-deny"
    assert build_provider("nonsense").name == "auto-deny"


def test_web_provider_interactive_flow(tmp_path):
    """ask + web：弹窗信号（总线，同 call_id）→ 应答 resolve → 放行；审计 by=user。"""
    c = _build(tmp_path, approval={"ask": ["t.write"], "provider": "web", "timeout_s": 5})

    async def s():
        await c.assemble()
        _register(c)
        seen: list[dict] = []
        c.root.on("agent/approval/pending", lambda p: seen.append(p))
        invoke = asyncio.ensure_future(c.root.tasks.invoke(c.root, "t.write", caller="t"))
        await asyncio.sleep(0.05)
        snap = c.root.approval.pending.snapshot()
        assert len(snap) == 1 and snap[0]["req"]["tool"] == "t.write"
        call_id = snap[0]["req"]["call_id"]
        assert seen and seen[0]["call_id"] == call_id, "弹窗信号与应答须同 call_id"
        assert c.root.approval.pending.resolve(call_id, {"approved": True, "by": "user"})
        assert await invoke == {"ok": True}
        answered = await _events(c, "agent/approval/answered")
        assert len(answered) == 1 and '"user"' in answered[0]["payload"]
        await c.teardown()

    _run(s())


def test_web_provider_reject_then_timeout_failsafe(tmp_path):
    """web：人拒绝 → 拒；不应答 → 超时默认拒（fail-safe），timeout 审计落盘。"""
    c = _build(tmp_path, approval={"ask": ["t.write"], "provider": "web", "timeout_s": 0.1})

    async def s():
        await c.assemble()
        _register(c)
        invoke = asyncio.ensure_future(c.root.tasks.invoke(c.root, "t.write", caller="t"))
        await asyncio.sleep(0.05)
        call_id = c.root.approval.pending.snapshot()[0]["req"]["call_id"]
        c.root.approval.pending.resolve(call_id, {"approved": False, "by": "user"})
        try:
            await invoke
            raise AssertionError("人拒绝后应被拒")
        except Exception as exc:
            assert "未获批准" in str(exc)
        timeout_inv = asyncio.ensure_future(c.root.tasks.invoke(c.root, "t.write", caller="t"))
        await asyncio.sleep(0.3)                    # > timeout_s=0.1
        try:
            await timeout_inv
            raise AssertionError("超时应默认拒绝")
        except Exception as exc:
            assert "未获批准" in str(exc)
        timeouts = await _events(c, "agent/approval/timeout")
        assert len(timeouts) == 1
        await c.teardown()

    _run(s())


def test_noninteractive_provider_emits_no_pending_frame(tmp_path):
    """非交互 provider 不发弹窗信号（防假弹窗）：auto-deny 下总线无 pending 帧。"""
    c = _build(tmp_path, approval={"ask": ["t.write"], "provider": "auto-deny"})

    async def s():
        await c.assemble()
        _register(c)
        seen: list[dict] = []
        c.root.on("agent/approval/pending", lambda p: seen.append(p))
        try:
            await c.root.tasks.invoke(c.root, "t.write", caller="t")
            raise AssertionError("auto-deny 应拒绝")
        except Exception:
            pass
        assert seen == [], "非交互 provider 不应产生弹窗信号"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 第 3 期：updated_input / 连拒语义 / plan 可见集 / submit_plan / 批量确认 / cli
# ---------------------------------------------------------------------------

def test_invoke_kwargs_readback_unit(tmp_path):
    """§9-1 updated_input：决策回读改写后的 kwargs（须替换 dict 才被识别）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        seen: list = []

        async def h(v: str = ""):
            seen.append(v)
            return {"got": v}

        c.root.tasks.register(c.root, "t.echo",
                              schema={"v": {"type": "str", "required": False}},
                              handler=h, risk="read", owner=None)

        async def rw(payload, next):                # 挂在 approval 之后改写参数
            d = await next()
            return {**d, "kwargs": {**d["kwargs"], "v": "rewritten"}}

        c.root.on(Events.TASKS_PRE_EXECUTE, rw)
        assert await c.root.tasks.invoke(c.root, "t.echo", caller="t", v="orig") \
            == {"got": "rewritten"}
        assert seen == ["rewritten"]
        await c.teardown()

    _run(s())


class _AlwaysCallsReasoner:
    """每步都发同一工具调用（永不收敛），用于测连拒闸。"""

    async def reason(self, ctx, messages, tools):
        return ReasonOutput(text="", tool_calls=[ToolCall(name="t.write", arguments={}, id="c")])


class _FixedDenyActor:
    """一律返回 denied 结果（denied_by 可控）。"""

    def __init__(self, denied_by: str):
        self.denied_by = denied_by

    async def act(self, ctx, calls):
        return [ToolResult(x.name, "被拒绝", is_error=True, denied=True,
                           denied_by=self.denied_by, tool_call_id=x.id) for x in calls]


class _PassthroughObserver:
    async def observe(self, ctx, results):
        return {"role": "user", "content": {"tool_results": [r.as_dict() for r in results]}}


def test_react_policy_reject_still_counts(tmp_path):
    """策略类拒绝（denied_by != "user"）照旧计入连拒闸 → 反复被拒终止。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        loop = ReactLoop(_AlwaysCallsReasoner(), _FixedDenyActor("approval"),
                         _PassthroughObserver(), None,        # type: ignore[arg-type]
                         ReactConfig(max_steps=6, deadline_s=30, max_rejects=1,
                                     finalize_on_exceed=False))
        result = await loop.run(c.root, "go")
        assert result.exhausted and "反复被拒" in (result.reason or "")
        await c.teardown()

    _run(s())


def test_react_user_reject_not_counted(tmp_path):
    """denied_by=="user"（用户本轮主动拒）不计连拒 → 不终止，跑到步数上限兜底。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        loop = ReactLoop(_AlwaysCallsReasoner(), _FixedDenyActor("user"),
                         _PassthroughObserver(), None,        # type: ignore[arg-type]
                         ReactConfig(max_steps=4, deadline_s=30, max_rejects=1,
                                     finalize_on_exceed=False))
        result = await loop.run(c.root, "go")
        assert result.exhausted and "步数上限" in (result.reason or ""), \
            "用户拒绝应读理由换方案继续，而非按工具坏循环终止"
        await c.teardown()

    _run(s())


def test_plan_visible_tools(tmp_path):
    """§6.1 plan 可见集（软约束）：只保留 risk=="read"；其余模式恒全量。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        _register(c)
        assert {t["name"] for t in visible_tools(c.root)} >= {"t.read", "t.write"}
        await c.root.approval.set_mode(c.root, "plan", by="test")
        vis = visible_tools(c.root)
        # ask_user 为常驻 read 级工具（澄清问询恰恰发生在规划期），plan 下理应可见
        assert {t["name"] for t in vis} == {"t.read", "approval.submit_plan",
                                            "approval.ask_user"}, \
            "plan 模式只可见 read + submit_plan + ask_user"
        assert all(t["risk"] == "read" for t in vis)
        await c.root.approval.set_mode(c.root, "default", by="test")
        assert {t["name"] for t in visible_tools(c.root)} >= {"t.read", "t.write"}
        await c.teardown()

    _run(s())


def test_submit_plan_approve_switches_mode(tmp_path):
    """§6.2/§6.3：进 plan 动态注册 submit_plan；批准 → 切 auto 并注销。"""
    c = _build(tmp_path, approval={"provider": "web", "timeout_s": 5})

    async def s():
        await c.assemble()
        _register(c)
        assert not c.root.tasks.has("approval.submit_plan")
        await c.root.approval.set_mode(c.root, "plan", by="test")
        assert c.root.tasks.has("approval.submit_plan")
        names = {t["name"] for t in visible_tools(c.root)}
        assert "approval.submit_plan" in names and "t.write" not in names, \
            "submit_plan 只读可见，写工具被裁"
        invoke = asyncio.ensure_future(c.root.tasks.invoke(
            c.root, "approval.submit_plan", caller="t", plan="计划A"))
        await asyncio.sleep(0.05)
        call_id = c.root.approval.pending.snapshot()[0]["req"]["call_id"]
        c.root.approval.pending.resolve(call_id, {"approved": True, "by": "user"})
        out = await invoke
        assert out["approved"] is True and out["mode"] == "auto"
        assert c.root.approval.mode() == "auto"
        assert not c.root.tasks.has("approval.submit_plan"), "离开 plan 应注销"
        await c.teardown()

    _run(s())


def test_submit_plan_reject_keeps_plan_with_note(tmp_path):
    """拒绝 → 留在 plan，用户反馈（note）回灌给模型；submit_plan 保持可用。"""
    c = _build(tmp_path, approval={"provider": "web", "timeout_s": 5})

    async def s():
        await c.assemble()
        await c.root.approval.set_mode(c.root, "plan", by="test")
        invoke = asyncio.ensure_future(c.root.tasks.invoke(
            c.root, "approval.submit_plan", caller="t", plan="计划B"))
        await asyncio.sleep(0.05)
        call_id = c.root.approval.pending.snapshot()[0]["req"]["call_id"]
        c.root.approval.pending.resolve(
            call_id, {"approved": False, "by": "user", "note": "先做只读部分"})
        out = await invoke
        assert out["approved"] is False and "只读部分" in out["hint"]
        assert c.root.approval.mode() == "plan"
        assert c.root.tasks.has("approval.submit_plan")
        await c.teardown()

    _run(s())


def test_session_allow_batch(tmp_path):
    """批量确认：应答带 always ⇒ 会话级记忆，同类请求不再问（allow 与 deny 两向）。"""
    c = _build(tmp_path, approval={"ask": ["t.write"], "provider": "web", "timeout_s": 5})

    async def s():
        await c.assemble()
        _register(c)
        inv1 = asyncio.ensure_future(c.root.tasks.invoke(c.root, "t.write", caller="t"))
        await asyncio.sleep(0.05)
        call_id = c.root.approval.pending.snapshot()[0]["req"]["call_id"]
        c.root.approval.pending.resolve(
            call_id, {"approved": True, "by": "user", "always": True})
        assert await inv1 == {"ok": True}
        inv2 = asyncio.ensure_future(c.root.tasks.invoke(c.root, "t.write", caller="t"))
        await asyncio.sleep(0.05)
        assert c.root.approval.pending.size() == 0, "session-allow 不应再问"
        assert await inv2 == {"ok": True}
        inv3 = asyncio.ensure_future(c.root.tasks.invoke(c.root, "t.write", caller="t"))
        c.root.approval._session_allow.clear()
        c.root.approval._session_deny.add("t.write")
        await asyncio.sleep(0.05)
        assert c.root.approval.pending.size() == 0, "session-deny 不应再问"
        try:
            await inv3
            raise AssertionError("session-deny 应拒绝")
        except Exception as exc:
            assert "本会话拒绝" in str(exc)
        await c.teardown()

    _run(s())


def test_updated_input_via_answer(tmp_path):
    """updated_input 端到端：弹窗应答带 kwargs ⇒ 人修订参数回读执行。"""
    c = _build(tmp_path, approval={"ask": ["t.echo"], "provider": "web", "timeout_s": 5})

    async def s():
        await c.assemble()

        async def h(v: str = ""):
            return {"got": v}

        c.root.tasks.register(c.root, "t.echo",
                              schema={"v": {"type": "str", "required": False}},
                              handler=h, risk="write", owner=None)
        invoke = asyncio.ensure_future(
            c.root.tasks.invoke(c.root, "t.echo", caller="t", v="orig"))
        await asyncio.sleep(0.05)
        call_id = c.root.approval.pending.snapshot()[0]["req"]["call_id"]
        c.root.approval.pending.resolve(
            call_id, {"approved": True, "by": "user", "kwargs": {"v": "user-edited"}})
        assert await invoke == {"got": "user-edited"}
        await c.teardown()

    _run(s())


def test_cli_provider_answers(monkeypatch):
    """cli provider：y=批准、a=批准+always、空/EOF=拒绝（fail-safe）。"""
    from agent.plugins.approval.providers import CliProvider

    async def s():
        p = CliProvider()
        monkeypatch.setattr("builtins.input", lambda *a: "y")
        assert await p.ask(None, {"tool": "t", "risk": "write", "mode": "default"}) \
            == {"approved": True, "by": "cli"}
        monkeypatch.setattr("builtins.input", lambda *a: "a")
        ans = await p.ask(None, {"tool": "t"})
        assert ans["approved"] is True and ans.get("always") is True
        monkeypatch.setattr("builtins.input", lambda *a: "")
        assert (await p.ask(None, {"tool": "t"}))["approved"] is False

        def _eof(*a):
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        assert (await p.ask(None, {"tool": "t"}))["approved"] is False

    _run(s())


def test_denied_by_propagates_to_invoke_error(tmp_path):
    """denied_by 经决策 → CapabilityNotImplemented.denied_by 全链透传：
    auto-deny（策略）→ "approval"；用户主动拒 → "user"。"""
    c = _build(tmp_path, approval={"ask": ["t.write"], "provider": "auto-deny"})

    async def s():
        await c.assemble()
        _register(c)
        try:
            await c.root.tasks.invoke(c.root, "t.write", caller="t")
            raise AssertionError("auto-deny 应拒绝")
        except CapabilityNotImplemented as exc:
            assert exc.denied_by == "approval"
        await c.teardown()

    _run(s())

    c2 = _build(tmp_path, approval={"ask": ["t.write"], "provider": "web", "timeout_s": 5})

    async def s2():
        await c2.assemble()
        _register(c2)
        invoke = asyncio.ensure_future(c2.root.tasks.invoke(c2.root, "t.write", caller="t"))
        await asyncio.sleep(0.05)
        call_id = c2.root.approval.pending.snapshot()[0]["req"]["call_id"]
        c2.root.approval.pending.resolve(call_id, {"approved": False, "by": "user"})
        try:
            await invoke
            raise AssertionError("用户拒绝后应被拒")
        except CapabilityNotImplemented as exc:
            assert exc.denied_by == "user", "用户主动拒须标 user（react 连拒闸豁免依据）"
        await c2.teardown()

    _run(s2())


# ---------------------------------------------------------------------------
# 澄清问询（ask_user）：不止二元审批——选项点选 + 自由文字指示
# ---------------------------------------------------------------------------

def test_ask_user_choice_roundtrip(tmp_path):
    """选项问询：模型调用 → 卡片信号入待答表 → 用户点选 → 回灌 choice。"""
    c = _build(tmp_path, approval={"provider": "web", "timeout_s": 5})

    async def s():
        await c.assemble()
        inv = asyncio.ensure_future(c.root.tasks.invoke(
            c.root, "approval.ask_user", caller="t",
            question="数据库选 MySQL 还是 SQLite？", options="MySQL, SQLite"))
        await asyncio.sleep(0.05)
        snap = c.root.approval.pending.snapshot()
        assert snap and snap[0]["req"]["kind"] == "ask_user"
        assert snap[0]["req"]["question"] == "数据库选 MySQL 还是 SQLite？"
        # 第 4 期起 options 归一为选择规格（label/value/desc/group），前端据此定形态
        assert [o["label"] for o in snap[0]["req"]["options"]] == ["MySQL", "SQLite"]
        call_id = snap[0]["req"]["call_id"]
        assert c.root.approval.pending.resolve(
            call_id, {"answered": True, "choice": "MySQL", "by": "user"})
        out = await inv
        assert out["answered"] is True and out["choice"] == "MySQL"
        evs = await _events(c, Events.APPROVAL_ANSWERED)
        assert any("approval.ask_user" in str(e) for e in evs), "问询应落 durable 审计"
        await c.teardown()

    _run(s())


def test_ask_user_free_text(tmp_path):
    """自由文本：用户不打选项、直接输入文字指示，模型收到 text。"""
    c = _build(tmp_path, approval={"provider": "web", "timeout_s": 5})

    async def s():
        await c.assemble()
        inv = asyncio.ensure_future(c.root.tasks.invoke(
            c.root, "approval.ask_user", caller="t", question="接下来做什么？"))
        await asyncio.sleep(0.05)
        call_id = c.root.approval.pending.snapshot()[0]["req"]["call_id"]
        c.root.approval.pending.resolve(
            call_id, {"answered": True, "text": "先做导出功能，样式以后再说", "by": "user"})
        out = await inv
        assert out["answered"] is True
        assert out["text"] == "先做导出功能，样式以后再说"
        await c.teardown()

    _run(s())


def test_ask_user_timeout_failsafe(tmp_path):
    """超时未答 → answered=False + 假设提示（不挂死循环，fail-safe 语义）。"""
    c = _build(tmp_path, approval={"provider": "web", "timeout_s": 1})

    async def s():
        await c.assemble()
        out = await c.root.tasks.invoke(
            c.root, "approval.ask_user", caller="t", question="在吗？")
        assert out["answered"] is False
        assert "超时" in out["reason"]
        assert "假设" in out["reason"]
        await c.teardown()

    _run(s())


def test_ask_user_non_interactive_degrades(tmp_path):
    """非交互通道（auto-deny）→ 立即降级返回，不等人、不发卡片信号。"""
    c = _build(tmp_path, approval={"provider": "auto-deny"})

    async def s():
        await c.assemble()
        out = await c.root.tasks.invoke(
            c.root, "approval.ask_user", caller="t", question="选哪个？")
        assert out["answered"] is False
        assert "非交互" in out["reason"]
        assert c.root.approval.pending.size() == 0, "非交互不应产生在途等待"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 第 4 期：选择规格（结构化选项 + 多选 + 分组）——卡片形态随决策而定
# ---------------------------------------------------------------------------

def test_normalize_options_shapes():
    """归一纯函数：JSON 数组 / 对象数组 / 逗号兜底 / 引号残渣 四类形态。"""
    from agent.plugins.approval.options import normalize_options

    assert [o["label"] for o in normalize_options('["A","B"]')] == ["A", "B"]
    objs = normalize_options('[{"label":"方案A","desc":"改动最小","value":"a","group":"技术"},'
                             '{"label":"方案B","desc":"能力最强","group":"技术"}]')
    assert objs[0] == {"label": "方案A", "value": "a", "desc": "改动最小", "group": "技术"}
    assert objs[1]["value"] == "方案B" and objs[1]["group"] == "技术"
    # 选项文本内含逗号：JSON 优先 ⇒ 不被切碎（旧实现按逗号硬切）
    assert [o["label"] for o in normalize_options('["先做A，因为快","后做B"]')] \
        == ["先做A，因为快", "后做B"]
    # 兜底路径：逗号分隔 + 剥引号/括号残渣（历史事故：回传值带着引号）
    got = normalize_options('"电影主题曲", "电视剧主题曲"')
    assert [o["label"] for o in got] == ["电影主题曲", "电视剧主题曲"]
    # 映射写法 {方案: 说明} 视作带说明的候选；空值一律为空表
    mapped = normalize_options({"先做导出": "用户最急", "先做统计": "看数据"})
    assert mapped[0]["label"] == "先做导出" and mapped[0]["desc"] == "用户最急"
    assert normalize_options("") == [] and normalize_options(None) == []


def test_ask_user_options_structured(tmp_path):
    """结构化候选项：卡片收到 label/desc/group 规格，答复回灌单项取值。"""
    c = _build(tmp_path, approval={"provider": "web", "timeout_s": 5})

    async def s():
        await c.assemble()
        inv = asyncio.ensure_future(c.root.tasks.invoke(
            c.root, "approval.ask_user", caller="t", question="走哪条路？",
            options='[{"label":"方案A","desc":"改动最小","group":"技术"},'
                    '{"label":"方案B","desc":"能力最强","group":"产品"}]'))
        await asyncio.sleep(0.05)
        req = c.root.approval.pending.snapshot()[0]["req"]
        assert [o["label"] for o in req["options"]] == ["方案A", "方案B"]
        assert req["options"][0]["desc"] == "改动最小"
        assert req["options"][1]["group"] == "产品"
        assert req["multi"] is False
        c.root.approval.pending.resolve(
            req["call_id"], {"answered": True, "choice": "方案A", "by": "user"})
        out = await inv
        assert out["answered"] is True
        assert out["choice"] == "方案A" and out["choices"] == ["方案A"]
        await c.teardown()

    _run(s())


def test_ask_user_multi_choices_roundtrip(tmp_path):
    """多选：用户勾选多项一次提交 → 模型收到 choices 列表。"""
    c = _build(tmp_path, approval={"provider": "web", "timeout_s": 5})

    async def s():
        await c.assemble()
        inv = asyncio.ensure_future(c.root.tasks.invoke(
            c.root, "approval.ask_user", caller="t", question="要哪些能力？",
            options='[{"label":"导出"},{"label":"导入"},{"label":"统计"}]', multi=True))
        await asyncio.sleep(0.05)
        req = c.root.approval.pending.snapshot()[0]["req"]
        assert req["multi"] is True
        c.root.approval.pending.resolve(
            req["call_id"], {"answered": True, "choices": ["导出", "统计"], "by": "user"})
        out = await inv
        assert out["choices"] == ["导出", "统计"]
        assert out["multi"] is True and out.get("choice") is None
        assert "多项" in out["hint"]
        await c.teardown()

    _run(s())


def test_ask_user_multi_without_options_degrades(tmp_path):
    """multi=true 但无候选 → 退回单选语义（无候选项时多选无意义）。"""
    c = _build(tmp_path, approval={"provider": "web", "timeout_s": 5})

    async def s():
        await c.assemble()
        inv = asyncio.ensure_future(c.root.tasks.invoke(
            c.root, "approval.ask_user", caller="t", question="随便说说？", multi=True))
        await asyncio.sleep(0.05)
        req = c.root.approval.pending.snapshot()[0]["req"]
        assert req["options"] == [] and req["multi"] is False
        c.root.approval.pending.resolve(
            req["call_id"], {"answered": True, "text": "都行", "by": "user"})
        out = await inv
        assert out["answered"] is True and out["text"] == "都行"
        await c.teardown()

    _run(s())


def test_ask_user_audit_records_options(tmp_path):
    """审计补记候选项与多选标记（旧版只记 question，事后调试只能靠猜）。"""
    c = _build(tmp_path, approval={"provider": "web", "timeout_s": 5})

    async def s():
        await c.assemble()
        inv = asyncio.ensure_future(c.root.tasks.invoke(
            c.root, "approval.ask_user", caller="t", question="选哪个？",
            options="A, B", multi=True))
        await asyncio.sleep(0.05)
        req = c.root.approval.pending.snapshot()[0]["req"]
        c.root.approval.pending.resolve(
            req["call_id"], {"answered": True, "text": "都行", "by": "user"})
        await inv
        asked = await _events(c, Events.APPROVAL_ASKED)
        assert any("'A'" in str(e) or '"A"' in str(e) for e in asked), "审计应记下候选项"
        assert any("multi" in str(e) for e in asked), "审计应记下多选标记"
        await c.teardown()

    _run(s())
