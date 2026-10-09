"""plugins.subagent 阶段 3：异步委派（wait=false + 句柄）、防死循环护栏、叶子化、Q4 治理覆盖。

设计稿 v0.4 §3.2b / R1 / R2 / Q4。覆盖：
- 异步委派：spawn 立即返回句柄；取回是**一次 await**（非轮询）；超时不取消子任务；
  句柄按父 run 分桶；teardown 回收在飞任务；
- 防死循环：委派预算 / 同一任务重派上限 / 未回收句柄上限（结构化 rejected，不抛）；
- 叶子化（R1）：委派工具被**排除**下传（提示层与执行层同口径）；
- Q4 轻量：角色 ``context`` 派生治理器副本（原对象不被改），未知字段诚实跳过。
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from agent.api import AgentError, Events, Services, StateKeys, set_current_run_id
from agent.plugins.react.loop import _apply_whitelist, _split_whitelist_ids
from agent.plugins.subagent import runner as runner_mod
from agent.plugins.subagent import roles
from agent.plugins.subagent.roles import apply_leaf_rule
from agent.plugins.react import React
from agent.plugins.subagent import SubAgent
from agent.profiles import build_agent


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_registries():
    """模块级记账字典在测试间必须清空（父 run 无结束事件 ⇒ 测试自己负责）。"""
    runner_mod.reset_run_state()
    yield
    runner_mod.reset_run_state()


def _result(text, *, run_id="R", ok=True, exhausted=False):
    class _R:
        pass
    r = _R()
    r.text = text
    r.ok = ok
    r.exhausted = exhausted
    r.run_id = run_id
    r.as_evidence = lambda: {"ok": ok, "steps": 1}
    return r


class _GateLoop:
    """可控子循环：进入即报 started，等 gate 打开才返回（用于断言「未阻塞」与超时）。"""

    def __init__(self, **kw):
        self.kw = kw
        self.gate = asyncio.Event()
        self.started = asyncio.Event()
        self.prompt = ""

    async def run(self, ctx, prompt, *, identity=None):
        self.prompt = prompt
        self.started.set()
        await self.gate.wait()
        ctx.state[StateKeys.REACT_RUN_ID] = "CHILD_RUN"
        ctx.state[StateKeys.SESSION_KEY] = identity.session_key
        return _result("async child answer", run_id="ASYNC_RID")


class _BoomLoop:
    def __init__(self, **kw):
        self.kw = kw

    async def run(self, ctx, prompt, *, identity=None):
        raise RuntimeError("async child boom")


class _Ctx:
    def __init__(self, factory, *, data_dir, governor=None, catalog=None):
        self.state = {StateKeys.REACT_RUN_ID: "PARENT_RUN",
                      StateKeys.SESSION_KEY: "default:default"}
        self.config = {"db_path": str(pathlib.Path(data_dir) / "agent.db")}
        self._factory = factory
        self._governor = governor
        self._catalog = catalog or []
        self.effects: list = []

    def has(self, key):
        if key == Services.REACT_FACTORY:
            return True
        if key == Services.CONTEXT:
            return self._governor is not None
        return False

    def get(self, key, default=None):
        if key == Services.REACT_FACTORY:
            return self._factory
        if key == Services.CONTEXT:
            return self._governor
        return default

    def effect(self, disposer):
        self.effects.append(disposer)
        return lambda: True

    # tasks 接缝（角色校验需要 catalog）
    @property
    def tasks(self):
        return self

    def catalog(self):
        return list(self._catalog)


def _ready_factory(seen: dict | None = None):
    """工厂：返回一个**立刻完成**的子循环（gate 已开），避免测试挂死。"""
    def factory(**kw):
        if seen is not None:
            seen.update(kw)
        lp = _GateLoop()
        lp.gate.set()
        return lp
    return factory


def _write_role(directory: pathlib.Path, name: str, spec: dict) -> None:
    d = pathlib.Path(directory) / "agent_roles"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


# ---------------------------------------------------------------------------
# 叶子化（R1）：委派工具绝不下传
# ---------------------------------------------------------------------------

def test_apply_leaf_rule_none_and_list():
    assert apply_leaf_rule(None) == ["*", "-agent.delegate"]
    assert apply_leaf_rule(["a", "agent.delegate"]) == ["a", "-agent.delegate"]
    assert apply_leaf_rule(["*"]) == ["*", "-agent.delegate"]


def test_whitelist_negative_entries_pure():
    base = [{"name": "a", "owner": "o1"}, {"name": "agent.delegate", "owner": "plugins.subagent"},
            {"name": "b", "owner": "o1"}]
    # "*" + 排除 ⇒ 只剩 a/b（委派工具被剔除）
    kept = _apply_whitelist(base, ["*", "-agent.delegate"])
    assert [t["name"] for t in kept] == ["a", "b"]
    # 只有排除项 ⇒ 其余全放行
    assert [t["name"] for t in _apply_whitelist(base, ["-agent.delegate"])] == ["a", "b"]
    # 按域排除
    assert [t["name"] for t in _apply_whitelist(base, ["-o1"])] == ["agent.delegate"]
    # 无排除项时与旧行为逐值等价（"*" ⇒ 原对象返回；名单 ⇒ 精确命中）
    assert _apply_whitelist(base, ["*"]) is base
    assert [t["name"] for t in _apply_whitelist(base, ["a"])] == ["a"]
    # 空 ⇒ 原对象返回（零行为变化）
    assert _apply_whitelist(base, None) is base


def _call(name: str, cid: str = "c1"):
    class _C:
        pass
    c = _C()
    c.name = name
    c.id = cid
    return c


def test_whitelist_denied_negative_entries_execution_layer():
    """执行层同口径：被排除的工具**点名也调不动**（提示层看得见≠执行层放得过）。"""
    from agent.plugins.react.loop import ReactLoop

    class _Stub:
        def __init__(self, override):
            self._tools_override = override

    class _CtxCatalog:
        def catalog(self):
            return [{"name": "agent.delegate", "owner": "plugins.subagent"},
                    {"name": "test.echo", "owner": "plugins.testtools"}]

    class _C:
        tasks = _CtxCatalog()

    calls = [_call("agent.delegate"), _call("test.echo", "c2")]
    denied, allowed = ReactLoop._split_whitelist_denied(
        _Stub(["*", "-agent.delegate"]), _C(), calls)
    assert [d.name for d in denied] == ["agent.delegate"], denied
    assert denied[0].denied_by == "whitelist" and denied[0].denied is True
    assert [c.name for c in allowed] == ["test.echo"], allowed
    # 无排除项、含 "*" ⇒ 零行为变化（父循环/不委派路径不受影响）
    denied2, allowed2 = ReactLoop._split_whitelist_denied(_Stub(["*"]), _C(), calls)
    assert denied2 == [] and len(allowed2) == 2


def test_split_whitelist_ids():
    assert _split_whitelist_ids(["a", "-b", "*"]) == ({"a", "*"}, {"b"})
    assert _split_whitelist_ids([]) == (set(), set())
    assert _split_whitelist_ids(["-"]) == (set(), set())      # 裸 "-" 不构成排除项


def test_child_loop_never_gets_delegate_tool(monkeypatch):
    """装配出的子循环白名单必须含排除项（提示层），否则子可再委派形成 mesh。"""
    seen = {}
    loop = _GateLoop()

    def factory(**kw):
        seen.update(kw)
        return loop

    ctx = _Ctx(factory, data_dir=_NO_ROLES)
    _run(runner_mod.start_subagent(ctx, task="t", role="default", param_tools=None,
                                   max_depth=1))
    ov = list(seen.get("tools_override") or [])
    assert f"-{roles.LEAF_TOOL}" in ov, ov


# ---------------------------------------------------------------------------
# 异步委派：spawn / await / 超时 / 作用域 / 回收
# ---------------------------------------------------------------------------

_NO_ROLES = pathlib.Path(__file__).resolve().parent / "_no_roles_async"


def test_async_spawn_returns_handle_without_waiting():
    loop = _GateLoop()
    ctx = _Ctx(lambda **kw: loop, data_dir=_NO_ROLES)

    async def s():
        out = await runner_mod.start_subagent(ctx, task="异步任务", role="default",
                                              param_tools=None, max_depth=1)
        assert out["status"] == "pending", out
        assert out["handle"]
        await loop.started.wait()                 # 子已开跑
        assert loop.gate.is_set() is False        # 但尚未完成 ⇒ spawn 确实没等它
        assert runner_mod.pending_handles(ctx) == [out["handle"]]
        loop.gate.set()
        got = await runner_mod.await_subagent(ctx, out["handle"], 5.0)
        assert got["status"] == "ok", got
        assert got["answer"] == "async child answer"
        assert got["request_id"] == "ASYNC_RID"
        # ★ 子任务收尾自行复原父共享键（异步路径的 await 发生在另一次调用里）
        assert ctx.state[StateKeys.REACT_RUN_ID] == "PARENT_RUN"
        assert ctx.state[StateKeys.SESSION_KEY] == "default:default"
        assert runner_mod.pending_handles(ctx) == []
    _run(s())


def test_await_unknown_handle_is_structured_error():
    ctx = _Ctx(lambda **kw: _GateLoop(), data_dir=_NO_ROLES)

    async def s():
        out = await runner_mod.await_subagent(ctx, "nope", 1.0)
        assert out["status"] == "error"
        assert (out["evidence"] or {}).get("error") == "unknown_handle"
    _run(s())


def test_await_timeout_does_not_cancel_child():
    loop = _GateLoop()
    ctx = _Ctx(lambda **kw: loop, data_dir=_NO_ROLES)

    async def s():
        out = await runner_mod.start_subagent(ctx, task="慢任务", role="default",
                                              param_tools=None, max_depth=1)
        h = out["handle"]
        await loop.started.wait()
        timed = await runner_mod.await_subagent(ctx, h, 0.05)
        assert timed["status"] == "pending" and timed.get("timed_out") is True, timed
        assert runner_mod.pending_handles(ctx) == [h]      # 句柄仍在（未被回收）
        loop.gate.set()
        got = await runner_mod.await_subagent(ctx, h, 5.0)  # 子没被取消 ⇒ 仍能取回
        assert got["status"] == "ok" and got["answer"] == "async child answer"
    _run(s())


def test_pending_handles_are_scoped_to_parent_run():
    loop = _GateLoop()
    ctx = _Ctx(lambda **kw: loop, data_dir=_NO_ROLES)

    async def s():
        set_current_run_id("run-A")
        out = await runner_mod.start_subagent(ctx, task="A 的任务", role="default",
                                              param_tools=None, max_depth=1)
        assert runner_mod.pending_handles(ctx) == [out["handle"]]
        # 换一个父 run ⇒ 看不到别人的句柄（防跨 run 取回）
        set_current_run_id("run-B")
        assert runner_mod.pending_handles(ctx) == []
        bad = await runner_mod.await_subagent(ctx, out["handle"], 1.0)
        assert bad["status"] == "error"
        assert (bad["evidence"] or {}).get("error") == "unknown_handle"
        set_current_run_id("run-A")
        loop.gate.set()
        got = await runner_mod.await_subagent(ctx, out["handle"], 5.0)
        assert got["status"] == "ok"
    _run(s())


def test_async_child_exception_is_structured():
    ctx = _Ctx(lambda **kw: _BoomLoop(), data_dir=_NO_ROLES)

    async def s():
        out = await runner_mod.start_subagent(ctx, task="会炸的任务", role="default",
                                              param_tools=None, max_depth=1)
        got = await runner_mod.await_subagent(ctx, out["handle"], 5.0)
        assert got["status"] == "error", got
        ev = got.get("evidence") or {}
        assert ev.get("error") == "RuntimeError"
        assert "boom" in (ev.get("detail") or "")      # 有细节，但不是裸抛
    _run(s())


def test_collect_all_pending_joins_every_handle():
    loops = []

    def factory(**kw):
        lp = _GateLoop()
        loops.append(lp)
        return lp

    ctx = _Ctx(factory, data_dir=_NO_ROLES)

    async def s():
        a = await runner_mod.start_subagent(ctx, task="任务甲", role="default",
                                            param_tools=None, max_depth=1)
        b = await runner_mod.start_subagent(ctx, task="任务乙", role="default",
                                            param_tools=None, max_depth=1)
        assert len(runner_mod.pending_handles(ctx)) == 2
        for lp in loops:
            lp.gate.set()
        out = await runner_mod.collect_pending(ctx, 5.0)
        assert out["count"] == 2 and out["succeeded"] == 2, out
        assert {r["handle"] for r in out["results"]} == {a["handle"], b["handle"]}
        assert runner_mod.pending_handles(ctx) == []
    _run(s())


def test_collect_when_nothing_pending_is_honest():
    ctx = _Ctx(lambda **kw: _GateLoop(), data_dir=_NO_ROLES)

    async def s():
        out = await runner_mod.collect_pending(ctx, 1.0)
        assert out["count"] == 0 and out["status"] == "ok"
        assert "没有在飞" in (out.get("detail") or "")
    _run(s())


def test_slot_limit_applies_to_async_children():
    """限流对异步子同样生效：槽位占满 ⇒ 下一个 spawn 拿不到槽位（不是无界后台）。"""
    loop = _GateLoop()
    ctx = _Ctx(lambda **kw: loop, data_dir=_NO_ROLES)

    async def s():
        first = await runner_mod.start_subagent(ctx, task="占位", role="default",
                                                param_tools=None, max_depth=1,
                                                max_threads=1)
        assert first["status"] == "pending"
        await loop.started.wait()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(runner_mod.start_subagent(
                ctx, task="第二个", role="default", param_tools=None,
                max_depth=1, max_threads=1), 0.1)
        loop.gate.set()
        got = await runner_mod.await_subagent(ctx, first["handle"], 5.0)
        assert got["status"] == "ok"
    _run(s())


def test_cancel_all_on_teardown():
    loop = _GateLoop()
    ctx = _Ctx(lambda **kw: loop, data_dir=_NO_ROLES)

    async def s():
        out = await runner_mod.start_subagent(ctx, task="会被回收的任务", role="default",
                                              param_tools=None, max_depth=1)
        await loop.started.wait()
        assert runner_mod.pending_handles(ctx) == [out["handle"]]
        n = await runner_mod.cancel_all("teardown")
        assert n == 1
        assert runner_mod.pending_handles(ctx) == []
        assert loop.gate.is_set() is False      # 子被取消，不是跑完
    _run(s())


# ---------------------------------------------------------------------------
# 防死循环：委派预算 / 重派上限 / 句柄上限
# ---------------------------------------------------------------------------

def test_delegation_budget_exhausted_is_rejected(monkeypatch):
    monkeypatch.setattr(runner_mod, "MAX_DELEGATIONS_PER_RUN", 2)
    ctx = _Ctx(_ready_factory(), data_dir=_NO_ROLES)

    async def s():
        for i in range(2):
            out = await runner_mod.run_subagent(ctx, task=f"任务{i}", role="default",
                                                param_tools=[], max_depth=1)
            assert out["status"] == "ok", out
        third = await runner_mod.run_subagent(ctx, task="任务2", role="default",
                                              param_tools=[], max_depth=1)
        assert third["status"] == "rejected", third
        assert third["evidence"]["reason"] == "delegation_budget_exhausted"
    _run(s())


def test_resend_limit_exceeded_is_rejected(monkeypatch):
    monkeypatch.setattr(runner_mod, "MAX_RESEND_PER_TASK", 2)
    seen = []

    def factory(**kw):
        lp = _GateLoop()
        lp.gate.set()
        seen.append(lp)
        return lp

    ctx = _Ctx(factory, data_dir=_NO_ROLES)

    async def s():
        for _ in range(2):
            out = await runner_mod.run_subagent(ctx, task="打回重派的任务", role="default",
                                                param_tools=[], max_depth=1)
            assert out["status"] == "ok", out
        again = await runner_mod.run_subagent(ctx, task="打回重派的任务", role="default",
                                              param_tools=[], max_depth=1)
        assert again["status"] == "rejected", again
        assert again["evidence"]["reason"] == "resend_limit_exceeded"
        # 换个任务文本 ⇒ 预算未耗尽，仍可委派（只拦「同一任务反复打回」）
        ok = await runner_mod.run_subagent(ctx, task="另一个任务", role="default",
                                           param_tools=[], max_depth=1)
        assert ok["status"] == "ok"
    _run(s())


def test_pending_handle_limit_is_rejected(monkeypatch):
    monkeypatch.setattr(runner_mod, "MAX_PENDING_PER_RUN", 1)
    ctx = _Ctx(lambda **kw: _GateLoop(), data_dir=_NO_ROLES)

    async def s():
        first = await runner_mod.start_subagent(ctx, task="甲", role="default",
                                                param_tools=None, max_depth=1,
                                                max_threads=8)
        assert first["status"] == "pending"
        second = await runner_mod.start_subagent(ctx, task="乙", role="default",
                                                 param_tools=None, max_depth=1,
                                                 max_threads=8)
        assert second["status"] == "rejected", second
        assert second["evidence"]["reason"] == "pending_handles_exhausted"
    _run(s())


# ---------------------------------------------------------------------------
# Q4（轻量）：角色 context / max_steps / deadline_s
# ---------------------------------------------------------------------------

class _Gov:
    def __init__(self):
        self.react_keep_recent = 6
        self.clip_max_tokens = 1000


def test_role_context_derives_governance_clone(tmp_path):
    _write_role(tmp_path, "asyncro", {"context": {"react_keep_recent": 2}})
    gov = _Gov()
    seen = {}
    ctx = _Ctx(_ready_factory(seen), data_dir=tmp_path, governor=gov)

    async def s():
        await runner_mod.run_subagent(ctx, task="t", role="asyncro",
                                      param_tools=[], max_depth=1)
    _run(s())
    clone = seen.get("governance_override")
    assert clone is not None and clone is not gov
    assert clone.react_keep_recent == 2
    assert gov.react_keep_recent == 6            # 原治理器**未被就地改写**


def test_role_context_unknown_field_is_skipped(tmp_path):
    """治理器上没有该字段 ⇒ 不覆盖（返回 None ⇒ 工厂回落容器现取），不静默塞属性。"""
    _write_role(tmp_path, "ghostctx", {"context": {"clip_head_tokens": 3}})
    gov = _Gov()                                  # 只有 react_keep_recent / clip_max_tokens
    seen = {}
    ctx = _Ctx(_ready_factory(seen), data_dir=tmp_path, governor=gov)

    async def s():
        await runner_mod.run_subagent(ctx, task="t", role="ghostctx",
                                      param_tools=[], max_depth=1)
    _run(s())
    assert seen.get("governance_override") is None


def test_role_overrides_steps_and_deadline(tmp_path):
    _write_role(tmp_path, "tight", {"max_steps": 3, "deadline_s": 12})
    seen = {}

    def factory(**kw):
        seen.update(kw)
        lp = _GateLoop()
        lp.gate.set()
        return lp

    ctx = _Ctx(factory, data_dir=tmp_path)

    async def s():
        out = await runner_mod.run_subagent(ctx, task="t", role="tight",
                                            param_tools=[], max_depth=1)
        assert out["status"] == "ok"
    _run(s())
    cfg = seen.get("cfg_overrides") or {}
    assert cfg.get("max_steps") == 3 and cfg.get("deadline_s") == 12.0, seen


def test_role_invalid_fields_reject_file(tmp_path):
    """越界/未知字段 ⇒ 整份角色被拒（回落 default），绝不静默变宽。"""
    _write_role(tmp_path, "badctx", {"context": {"nope": 1}})
    _write_role(tmp_path, "badsteps", {"max_steps": 9999})
    loaded = roles.load_role_files(roles.roles_dir(tmp_path), [])
    assert "badctx" not in loaded and "badsteps" not in loaded


# ---------------------------------------------------------------------------
# 端到端：真容器 + fixture LLM（零触网）
# ---------------------------------------------------------------------------

def _build(tmp_path):
    return build_agent(
        config={"db_path": str(tmp_path / "agent.db")},
        extra_plugins=(React, SubAgent))


def test_async_delegate_e2e_real_container(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        first = await c.root.tasks.invoke(c.root, "agent.delegate",
                                          task="异步委派端到端", wait=False)
        second = await c.root.tasks.invoke(c.root, "agent.delegate",
                                           task="留到卸载的句柄", wait=False)
        handle = first["handle"]
        assert handle in runner_mod.pending_handles(c.root)
        got = await c.root.tasks.invoke(c.root, "agent.delegate", handle=handle)
        assert got["status"] == "ok", got
        # ★ 断言任务文本真到达子循环（弱断言「含 fixture 字样」曾导致破坏实验漏检）
        assert "异步委派端到端" in (got.get("answer") or ""), got
        assert runner_mod.pending_handles(c.root) == [second["handle"]]
        await c.teardown()
        # ★ teardown 必须回收**未被取回**的句柄（effect 真被挂上；否则记账泄漏）
        assert runner_mod._PENDING == {}, runner_mod._PENDING
    _run(s())


def test_async_fanout_then_collect_all_e2e(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        out = await c.root.tasks.invoke(c.root, "agent.delegate",
                                        tasks=["并行甲", "并行乙"], wait=False)
        assert out["status"] == "pending", out
        assert len(out["handles"]) == 2, out
        all_out = await c.root.tasks.invoke(c.root, "agent.delegate", handle="*")
        assert all_out["count"] == 2, all_out
        assert all_out["succeeded"] == 2, all_out
        joined = " ".join(str(r.get("answer") or "") for r in all_out["results"])
        assert "并行甲" in joined and "并行乙" in joined, all_out
        await c.teardown()
    _run(s())


def test_delegate_rejects_empty_task_after_handle_absent(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        with pytest.raises(AgentError):
            await c.root.tasks.invoke(c.root, "agent.delegate", task="", tasks=[])
        await c.teardown()
    _run(s())


def test_role_file_roundtrip_via_real_container(tmp_path):
    """目录式角色在真容器里生效（阶段 2 能力不被阶段 3 回退）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        _write_role(tmp_path, "e2erole", {"system_note": "你是 E2E 角色",
                                          "max_steps": 4})
        out = await c.root.tasks.invoke(c.root, "agent.delegate",
                                        task="角色文件验证", role="e2erole")
        assert out["status"] == "ok", out
        assert "E2E" in (out.get("answer") or ""), out
        await c.teardown()
    _run(s())
