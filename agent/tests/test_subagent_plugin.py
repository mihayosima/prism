"""plugins.subagent 验收测试（MVP；设计稿 v0.3）。

覆盖：
- 角色白名单三层交集的下两层（role.tools ∩ 请求 tools）纯函数；
- ``run_subagent``：结构化返回 / 共享 state 的 save·restore / 深度护栏 / 工厂缺席降级 /
  无交集报错 / 子运行期异常结构化（不裸抛）；
- 插件装配：REACT_FACTORY 在场 ⇒ 注册 ``agent.delegate``（owner=plugins.subagent, risk=read）；
  REACT_FACTORY 缺席 ⇒ 不注册（优雅降级）；
- 端到端（真容器 + 假工厂）：经 ``ctx.tasks.invoke`` 跑通委派并回填 request_id，且父 REACT_RUN_ID 复原。
"""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from agent.api import AgentError, Events, Services, StateKeys
from agent.core.session import make_identity
from agent.plugins.react import React
from agent.plugins.subagent import runner as runner_mod
from agent.plugins.subagent import roles, SubAgent
from agent.plugins.subagent.roles import Role, combine_tools, get_role
from agent.profiles import build_agent


@pytest.fixture(autouse=True)
def _clean_delegation_budget():
    """阶段 3：委派预算是**每父 run**记账的模块级状态。

    单测直调工具时没有 HTTP 请求 ⇒ 无 run/会话 ContextVar ⇒ 同进程内所有委派
    落进同一个回退桶。若不逐例清空，前例的记账会拖垮后例（实测：跑到第 25 次
    委派开始全被 rejected）。这是**测试隔离**需要，不是放宽护栏。
    """
    runner_mod.reset_run_state()
    yield
    runner_mod.reset_run_state()


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# 纯函数：角色白名单交集（下两层；父权限裁剪由工厂再做）
# ---------------------------------------------------------------------------

def test_combine_tools_no_narrowing():
    assert combine_tools(None, None) is None
    assert combine_tools(["a", "b"], None) == ["a", "b"]
    assert combine_tools(None, ["a"]) == ["a"]


def test_combine_tools_intersect():
    assert combine_tools(["a", "b"], ["b", "c"]) == ["b"]
    assert combine_tools(["*"], ["a"]) == ["a"]
    assert combine_tools(["a"], ["*"]) == ["a"]


def test_combine_tools_disjoint_raises():
    with pytest.raises(ValueError):
        combine_tools(["a"], ["x"])


def test_get_role_fallback_unknown():
    assert get_role("default").name == "default"
    assert get_role("zzz").name == "default"
    assert "explorer" in get_role("explorer").name


# ---------------------------------------------------------------------------
# run_subagent：核心逻辑（假 ctx + 假工厂）
# ---------------------------------------------------------------------------

def _result(text, *, run_id="R", ok=True, exhausted=False):
    class _R:
        pass
    r = _R()
    r.text = text
    r.ok = ok
    r.exhausted = exhausted
    r.run_id = run_id

    def as_evidence():
        return {"ok": ok, "steps": 1}
    r.as_evidence = as_evidence
    return r


class _FakeLoop:
    def __init__(self, **kw):
        self.kw = kw

    async def run(self, ctx, prompt, *, identity=None):
        # 模拟 react 真实行为：运行期把共享账单键改写为子 run，收尾再清空
        ctx.state[StateKeys.REACT_RUN_ID] = "CHILD_RUN"
        ctx.state[StateKeys.SESSION_KEY] = identity.session_key
        return _result("child answer", run_id="CHILD_RID")


class _RaisingLoop:
    def __init__(self, **kw):
        pass

    async def run(self, ctx, prompt, *, identity=None):
        ctx.state[StateKeys.REACT_RUN_ID] = "CHILD_RUN"
        raise RuntimeError("child boom")


class _FakeCtx:
    def __init__(self, factory=None, has_factory=True):
        self.state = {StateKeys.REACT_RUN_ID: "PARENT_RUN",
                      StateKeys.SESSION_KEY: "default:default"}
        self._factory = factory
        self._has = has_factory
        # 阶段 2：角色文件加载需要 config(db_path) 与 tasks.catalog()
        self.config = {"db_path": str(pathlib.Path(_NO_ROLES_DIR) / "agent.db")}
        self.tasks = _FakeTasks(_CAT)
        self.effects: list = []

    def has(self, key):
        return self._has if key == Services.REACT_FACTORY else False

    def get(self, key, default=None):
        return self._factory if key == Services.REACT_FACTORY else default

    def effect(self, disposer):
        """阶段 3：apply 会注册异步委派回收器（真 Context 同签名）。"""
        self.effects.append(disposer)
        return lambda: True


class _FakeTasks:
    """最小 tasks 接缝：只提供角色校验所需的 catalog()。"""

    def __init__(self, catalog):
        self._catalog = catalog

    def catalog(self):
        return list(self._catalog)


# 空角色目录（保证单测不读到真实 data/agent_roles）
_NO_ROLES_DIR = pathlib.Path(__file__).resolve().parent / "_no_roles"


def _fake_durable_recorder(captured):
    async def _d(ctx, ev, payload):
        captured.append((ev, payload))
    return _d


def test_run_subagent_ok_and_state_restored(monkeypatch):
    captured = []
    monkeypatch.setattr(runner_mod, "durable", _fake_durable_recorder(captured))
    ctx = _FakeCtx(factory=_FakeLoop)
    out = _run(runner_mod.run_subagent(
        ctx, task="t", role="default", param_tools=[], max_depth=1))
    assert out["status"] == "ok"
    assert out["answer"] == "child answer"
    assert out["request_id"] == "CHILD_RID"
    # ★ 共享账单键必须复原（子收尾会清空它）
    assert ctx.state[StateKeys.REACT_RUN_ID] == "PARENT_RUN"
    assert ctx.state[StateKeys.SESSION_KEY] == "default:default"
    # SUBAGENT_SPAWN 审计落盘
    assert captured and captured[0][0] == Events.SUBAGENT_SPAWN
    assert captured[0][1]["child_run_id"] == "CHILD_RID"
    assert captured[0][1]["status"] == "ok"


def test_run_subagent_depth_limit(monkeypatch):
    monkeypatch.setattr(runner_mod, "durable", _fake_durable_recorder([]))
    ctx = _FakeCtx(factory=_FakeLoop)

    async def _go():
        runner_mod._SUBAGENT_DEPTH.set(1)      # 已达上限（max_depth=1）
        return await runner_mod.run_subagent(
            ctx, task="t", role="default", param_tools=[], max_depth=1)
    with pytest.raises(AgentError):
        _run(_go())


def test_run_subagent_factory_absent():
    ctx = _FakeCtx(factory=None, has_factory=False)
    with pytest.raises(AgentError):
        _run(runner_mod.run_subagent(
            ctx, task="t", role="default", param_tools=[], max_depth=1))


def test_run_subagent_disjoint_tools(monkeypatch):
    monkeypatch.setattr(runner_mod, "durable", _fake_durable_recorder([]))
    ctx = _FakeCtx(factory=_FakeLoop)
    monkeypatch.setattr(
        runner_mod, "get_role",
        lambda name, extra=None: Role("custom", "d", ["a"], ""))
    with pytest.raises(AgentError):
        _run(runner_mod.run_subagent(
            ctx, task="t", role="custom", param_tools=["b"], max_depth=1))


def test_run_subagent_child_error_structured(monkeypatch):
    captured = []
    monkeypatch.setattr(runner_mod, "durable", _fake_durable_recorder(captured))
    ctx = _FakeCtx(factory=_RaisingLoop)
    out = _run(runner_mod.run_subagent(
        ctx, task="t", role="default", param_tools=[], max_depth=1))
    assert out["status"] == "error"
    assert out["answer"] is None
    assert "RuntimeError" in out["evidence"]["error"]
    assert ctx.state[StateKeys.REACT_RUN_ID] == "PARENT_RUN"   # 即便子异常也复原
    assert captured and captured[0][1]["status"] == "error"


# ---------------------------------------------------------------------------
# 插件装配：REACT_FACTORY 在场 ⇒ 注册；缺席 ⇒ 不注册
# ---------------------------------------------------------------------------

def test_apply_registers_delegate_when_factory_present():
    class _Reg:
        def __init__(self):
            self.names = []
        def register(self, ctx, name, **kw):
            self.names.append(name)
            return lambda: True
    ctx = _FakeCtx(factory=_FakeLoop, has_factory=True)
    ctx.tasks = _Reg()
    # @plugin 把类包成 Plugin 实例；用其内部原始类实例化做隔离断言
    p = SubAgent._raw()
    _run(p.apply(ctx))
    assert "agent.delegate" in ctx.tasks.names


def test_apply_skips_delegate_when_factory_absent():
    class _Reg:
        def __init__(self):
            self.names = []
        def register(self, ctx, name, **kw):
            self.names.append(name)
            return lambda: True
    ctx = _FakeCtx(factory=None, has_factory=False)
    ctx.tasks = _Reg()
    p = SubAgent._raw()
    _run(p.apply(ctx))
    assert "agent.delegate" not in ctx.tasks.names


# ---------------------------------------------------------------------------
# 端到端：真容器 + 假工厂（经 ctx.tasks.invoke 跑通委派）
# ---------------------------------------------------------------------------

def _build(tmp_path):
    return build_agent(
        config={"db_path": str(tmp_path / "agent.db")},
        extra_plugins=(React, SubAgent))


async def _echo_handler(text: str = "hi"):
    """test.echo 的处理器（fixture 脚本会调它；仅测试用）。"""
    return {"echo": text}


def _register_echo(ctx) -> None:
    ctx.tasks.register(
        ctx, "test.echo",
        schema={"text": {"type": "str", "required": False, "default": "hi"}},
        handler=_echo_handler, owner="plugins.testtools",
        description="回显参数", risk="read")


def test_delegate_registered_in_real_container(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        cat = {t["name"]: t for t in c.root.tasks.catalog()}
        assert "agent.delegate" in cat
        assert cat["agent.delegate"]["owner"] == "plugins.subagent"
        assert cat["agent.delegate"]["risk"] == "read"
        # 阶段 2 起 task/tasks 二选一 ⇒ 二者皆非 required，由 handler 校验「至少给一个」
        assert cat["agent.delegate"]["required"] == [], cat["agent.delegate"]["required"]
        params = cat["agent.delegate"]["params"]
        assert "task" in params and "tasks" in params and "role" in params
        await c.teardown()
    _run(s())


def test_delegate_end_to_end_invokes_child(tmp_path):
    c = _build(tmp_path)
    fake_factory = lambda **kw: _FakeLoop(**kw)

    async def s():
        await c.assemble()
        # 用假工厂替换真 react 工厂，隔离 fixture LLM，专测委派装配 + handler 逻辑
        c.root.set(Services.REACT_FACTORY, fake_factory)
        before = c.root.state.get(StateKeys.REACT_RUN_ID)
        out = await c.root.tasks.invoke(
            c.root, "agent.delegate", task="research X", tools=[])
        after = c.root.state.get(StateKeys.REACT_RUN_ID)
        # 父共享账单键复原（未被子的 CHILD_RUN 污染）
        assert before == after
        assert out["status"] == "ok"
        assert out["answer"] == "child answer"
        assert out["request_id"] == "CHILD_RID"
        # 假工厂确实拿到了正确的受限 depth（顶层调用 depth=1）
        # （通过再次 invoke 难以直接取 loop.kw；此处以 SUBAGENT_SPAWN 审计佐证）
        rows = await c.root.db.query(
            c.root,
            "SELECT payload FROM agent_events WHERE event=? ORDER BY seq DESC LIMIT 1",
            (Events.SUBAGENT_SPAWN,))
        assert rows, "SUBAGENT_SPAWN 审计应落盘"
        assert json.loads(rows[-1]["payload"])["status"] == "ok"
        await c.teardown()
    _run(s())


# ---------------------------------------------------------------------------
# ★ 真引擎端到端：真ReactLoop + fixture LLM（默认 provider）——证明子循环真能跑
# ---------------------------------------------------------------------------

def test_delegate_with_real_react_loop_and_fixture_llm(tmp_path):
    """不替换工厂：用真 react 工厂构造子循环，由离线 fixture LLM 驱动跑完。

    这是「委派链路真跑通」的硬证明——上方e2e 用假工厂只验证装配与handler 逻辑，
    本例让真实 ``ReactLoop.run`` 走完推理→收敛→结构化返回的全程。
    """
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        # 不 set 工厂：走真 react 插件 provide 的 REACT_FACTORY
        assert c.root.get(Services.REACT_FACTORY) is not None, "真工厂应在场"
        before = c.root.state.get(StateKeys.REACT_RUN_ID)
        task_text = "总结这份代码的职责与边界"
        out = await c.root.tasks.invoke(
            c.root, "agent.delegate", task=task_text, tools=[])
        after = c.root.state.get(StateKeys.REACT_RUN_ID)
        assert before == after, "父共享账单键必须复原"
        # fixture LLM 对普通 prompt 走「直接回答（fixture）：{prompt[:40]}」分支
        assert out["status"] == "ok", out
        assert out["answer"] and "fixture" in out["answer"], out["answer"]
        # ★ 任务本体必须真到达子循环（fixture 会回显 prompt 前40 字）——
        #   否则「子循环跑通」是假象：可能拿了空prompt 也返回非空文本。
        assert task_text in out["answer"], (
            f"子循环未收到任务本体（answer={out['answer']!r}）⇒ 委派链路未真正传参")
        # 子 run_id 必须真实回填（来自 ReactResult.run_id）
        assert out["request_id"], "child run_id 应回填到 request_id"
        assert out["evidence"], "evidence 应来自子ReactResult.as_evidence()"
        # 审计：child_run_id 与返回一致
        rows = await c.root.db.query(
            c.root,
            "SELECT payload FROM agent_events WHERE event=? ORDER BY seq DESC LIMIT 1",
            (Events.SUBAGENT_SPAWN,))
        assert rows, "SUBAGENT_SPAWN 应落盘"
        p = json.loads(rows[-1]["payload"])
        assert p["status"] == "ok"
        assert p["child_run_id"] == out["request_id"]
        await c.teardown()
    _run(s())


# ---------------------------------------------------------------------------
# 阶段 2-B：目录式角色文件（校验 / 安全边界 / 内置优先）
# ---------------------------------------------------------------------------

_CAT = [{"name": "agent.ping", "owner": "agent.tasks"},
        {"name": "agent.delegate", "owner": "plugins.subagent"}]


def test_role_file_loaded_and_applied(tmp_path):
    """放一个合法角色文件 ⇒ 可被解析并生效（加角色不用改代码）。"""
    d = tmp_path / "agent_roles"
    d.mkdir()
    (d / "reviewer.json").write_text(
        json.dumps({"description": "审查角色",
                    "tools": ["agent.ping"],
                    "system_note": "你是审查角色，只做复核"}), encoding="utf-8")
    loaded = roles.load_role_files(d, _CAT)
    assert "reviewer" in loaded
    assert loaded["reviewer"]["tools"] == ["agent.ping"]
    r = roles.get_role("reviewer", loaded)
    assert r.name == "reviewer" and "审查角色" in r.system_note


def test_role_file_unknown_tool_rejected_entirely(tmp_path):
    """★ 安全边界：引用不存在的工具 ⇒ 整份角色拒绝（绝不静默变成「不收窄」）。"""
    d = tmp_path / "agent_roles"
    d.mkdir()
    (d / "sneaky.json").write_text(
        json.dumps({"tools": ["agent.ping", "不存在的工具"]}), encoding="utf-8")
    loaded = roles.load_role_files(d, _CAT)
    assert "sneaky" not in loaded, "未知工具的角色必须整份拒绝，否则白名单被静默放宽"
    # 且该角色回落 default（不因文件坏而崩）
    assert roles.get_role("sneaky", loaded).name == "default"


def test_role_file_domain_reference_allowed(tmp_path):
    """按域（owner）引用工具是合法的（与执行层拦截口径一致）。"""
    d = tmp_path / "agent_roles"
    d.mkdir()
    (d / "dom.json").write_text(json.dumps({"tools": ["agent.tasks"]}), encoding="utf-8")
    loaded = roles.load_role_files(d, _CAT)
    assert loaded["dom.json".rsplit(".", 1)[0]] if False else True
    assert loaded["dom"]["tools"] == ["agent.tasks"]


def test_role_file_bad_schema_skipped_without_breaking_others(tmp_path):
    """坏文件跳过，不影响同目录其他合法角色。"""
    d = tmp_path / "agent_roles"
    d.mkdir()
    (d / "good.json").write_text(json.dumps({"tools": ["agent.ping"]}), encoding="utf-8")
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    (d / "unknown_field.json").write_text(json.dumps({"nope": 1}), encoding="utf-8")
    loaded = roles.load_role_files(d, _CAT)
    assert "good" in loaded
    assert "broken" not in loaded and "unknown_field" not in loaded


def test_role_file_illegal_name_skipped(tmp_path):
    """非法角色名（路径穿越/注入）⇒ 跳过。"""
    d = tmp_path / "agent_roles"
    d.mkdir()
    (d / "..evil.json").write_text(json.dumps({}), encoding="utf-8")
    loaded = roles.load_role_files(d, _CAT)
    assert not any("evil" in k or ".." in k for k in loaded), loaded


def test_role_file_missing_dir_returns_empty(tmp_path):
    """目录不存在 ⇒ 空表（内置角色照常可用，零行为变化）。"""
    assert roles.load_role_files(tmp_path / "nope", _CAT) == {}
    assert roles.get_role("default", {}).name == "default"


def test_builtin_role_not_overridden_by_file(tmp_path):
    """内置角色优先：文件不得覆盖 default（升级不改变既有语义）。"""
    d = tmp_path / "agent_roles"
    d.mkdir()
    (d / "default.json").write_text(
        json.dumps({"system_note": "恶意覆盖"}), encoding="utf-8")
    loaded = roles.load_role_files(d, _CAT)
    r = roles.get_role("default", loaded)
    assert r.system_note == "", "内置角色被文件覆盖了"


def test_role_file_end_to_end_through_delegate(tmp_path):
    """端到端：文件角色的 system_note 真到达子 prompt（不只是被解析）。"""
    c = _build(tmp_path)
    roles_path = pathlib.Path(tmp_path) / "agent_roles"
    roles_path.mkdir()
    (roles_path / "reviewer.json").write_text(
        json.dumps({"description": "审查", "tools": None,
                    "system_note": "你是审查角色FILEMARK"}), encoding="utf-8")

    async def s():
        await c.assemble()
        out = await c.root.tasks.invoke(
            c.root, "agent.delegate", task="复核一下", role="reviewer", tools=[])
        assert out["status"] == "ok", out
        assert "你是审查角色FILEMARK" in out["answer"], out["answer"]
        await c.teardown()
    _run(s())


# ---------------------------------------------------------------------------
# 阶段 2：并行委派（asyncio.gather + 每父 run 信号量）
# ---------------------------------------------------------------------------

class _SlowLoop:
    """记录「在飞峰值」的假子循环：用于证明并发真发生且受限流约束。"""

    def __init__(self, **kw):
        self.kw = kw

    async def run(self, ctx, prompt, *, identity=None):
        async with _FLIGHT_LOCK:
            _FLIGHT["now"] += 1
            _FLIGHT["peak"] = max(_FLIGHT["peak"], _FLIGHT["now"])
        try:
            await asyncio.sleep(_CHILD_DELAY)          # 模拟子循环耗时
        finally:
            async with _FLIGHT_LOCK:
                _FLIGHT["now"] -= 1
        r = _result(f"done:{prompt[:20]}")
        return r


_FLIGHT_LOCK = asyncio.Lock()
_FLIGHT = {"now": 0, "peak": 0}
_CHILD_DELAY = 0.05


def _reset_flight():
    _FLIGHT["now"] = 0
    _FLIGHT["peak"] = 0


def test_parallel_delegate_returns_all_results_in_order():
    ctx = _FakeCtx(factory=_SlowLoop)

    async def s():
        out = await runner_mod.run_subagents_parallel(
            ctx, tasks=["任务A", "任务B", "任务C"], role="default",
            param_tools=[], max_depth=1)
        assert out["status"] == "ok", out
        assert out["count"] == 3 and out["succeeded"] == 3
        # 顺序必须与入参一致（父循环要按 index 对回子任务）
        assert [r["index"] for r in out["results"]] == [0, 1, 2]
        assert [r["task"] for r in out["results"]] == ["任务A", "任务B", "任务C"]
        assert all(r["status"] == "ok" for r in out["results"])
        assert len(out["request_ids"]) == 3
    _run(s())


def test_parallel_delegate_runs_concurrently_and_respects_semaphore():
    """限流的**上限**与并发的**下限**都要验：既不串行、也不超发。"""
    ctx = _FakeCtx(factory=_SlowLoop)

    async def s():
        _reset_flight()
        n, limit = 6, 2
        out = await runner_mod.run_subagents_parallel(
            ctx, tasks=[f"t{i}" for i in range(n)], role="default",
            param_tools=[], max_depth=1, max_threads=limit)
        assert out["status"] == "ok", out
        assert out["count"] == n
        # 下限：真并发（峰值 >1，否则只是顺序执行）
        assert _FLIGHT["peak"] > 1, f"未并发，峰值={_FLIGHT['peak']}"
        # 上限：不得超过每父 run 的在飞上限
        assert _FLIGHT["peak"] <= limit, f"超出限流 {limit}，峰值={_FLIGHT['peak']}"
    _run(s())


def test_parallel_delegate_partial_failure_isolated():
    """单个子失败 ⇒ 整体 partial，其他子照常拿到结果（不掀翻整批）。"""

    class _FlakyLoop:
        def __init__(self, **kw):
            self.kw = kw

        async def run(self, ctx, prompt, *, identity=None):
            if "坏" in prompt:
                raise RuntimeError("子循环炸了")
            return _result("ok")

    ctx = _FakeCtx(factory=_FlakyLoop)

    async def s():
        out = await runner_mod.run_subagents_parallel(
            ctx, tasks=["正常A", "坏任务", "正常B"], role="default",
            param_tools=[], max_depth=1)
        assert out["status"] == "partial", out
        assert out["succeeded"] == 2 and out["failed"] == 1
        bad = out["results"][1]
        assert bad["status"] == "error"
        assert "RuntimeError" in bad["evidence"]["error"]
        assert out["results"][0]["status"] == "ok"
        assert out["results"][2]["status"] == "ok"
    _run(s())


def test_parallel_delegate_all_fail_is_error():
    class _BadLoop:
        def __init__(self, **kw):
            pass

        async def run(self, ctx, prompt, *, identity=None):
            raise RuntimeError("always fail")

    ctx = _FakeCtx(factory=_BadLoop)

    async def s():
        out = await runner_mod.run_subagents_parallel(
            ctx, tasks=["a", "b"], role="default", param_tools=[], max_depth=1)
        assert out["status"] == "error", out
        assert out["succeeded"] == 0 and out["failed"] == 2
    _run(s())


def test_parallel_delegate_rejects_empty_and_oversized():
    ctx = _FakeCtx(factory=_SlowLoop)

    async def s():
        with pytest.raises(AgentError):
            await runner_mod.run_subagents_parallel(
                ctx, tasks=[], role="default", param_tools=[], max_depth=1)
        with pytest.raises(AgentError):
            await runner_mod.run_subagents_parallel(
                ctx, tasks=["x"] * (runner_mod.MAX_SUBAGENT_BATCH + 1),
                role="default", param_tools=[], max_depth=1)
    _run(s())


def test_parallel_delegate_restores_parent_shared_state(tmp_path):
    """并行收尾后，父共享账单键必须回到父值（并发改写不得残留）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        c.root.state[StateKeys.REACT_RUN_ID] = "PARENT_X"
        out = await c.root.tasks.invoke(
            c.root, "agent.delegate", tasks=["子任务甲", "子任务乙"], tools=[])
        assert out["status"] == "ok", out
        assert c.root.state[StateKeys.REACT_RUN_ID] == "PARENT_X"
        await c.teardown()
    _run(s())


def test_parallel_delegate_real_engine_end_to_end(tmp_path):
    """真引擎并行：多个子循环真跑完，各自有独立 run_id（审计不串号）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        out = await c.root.tasks.invoke(
            c.root, "agent.delegate",
            tasks=["总结模块甲的职责", "总结模块乙的职责"], tools=[])
        assert out["status"] == "ok", out
        assert out["count"] == 2
        rids = [r["request_id"] for r in out["results"]]
        assert all(rids) and len(set(rids)) == 2, f"子 run_id 应各不相同：{rids}"
        # 任务本体真到达各自的子循环
        assert "模块甲" in out["results"][0]["answer"]
        assert "模块乙" in out["results"][1]["answer"]
        await c.teardown()
    _run(s())


def test_delegate_rejects_when_both_task_and_tasks_empty(tmp_path):
    """task 与 tasks 皆空 ⇒ 诚实报错，不静默委派一个空任务。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        with pytest.raises(AgentError):
            await c.root.tasks.invoke(c.root, "agent.delegate", task="", tools=[])
        await c.teardown()
    _run(s())


def test_delegate_with_real_loop_restores_shared_state(tmp_path):
    """真引擎下共享 state 隔离：子循环收尾清空 REACT_RUN_ID，父须复原。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        # 模拟父循环运行中的账单键
        c.root.state[StateKeys.REACT_RUN_ID] = "PARENT_REAL"
        out = await c.root.tasks.invoke(
            c.root, "agent.delegate", task="ping 子任务", tools=[])
        assert c.root.state[StateKeys.REACT_RUN_ID] == "PARENT_REAL", \
            "子循环把共享 REACT_RUN_ID 清空了却未复原⇒ 父后续工具调用会丢run_id"
        assert out["status"] == "ok", out
        await c.teardown()
    _run(s())


def test_delegate_with_real_loop_depth_guard_blocks_grandchild(tmp_path):
    """真引擎下深度护栏：子循环内部再调 delegate ⇒ 被拒（不无限递归）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        # 模拟已在子循环深度 1 的上下文里再委派
        token = runner_mod._SUBAGENT_DEPTH.set(1)
        try:
            with pytest.raises(AgentError) as ei:
                await c.root.tasks.invoke(
                    c.root, "agent.delegate", task="孙任务", tools=[])
            # ★ 必须是深度护栏拒绝（而非别的偶发错误），否则本测试无判别力
            assert "深度" in str(ei.value), f"非深度护栏拒绝：{ei.value}"
        finally:
            runner_mod._SUBAGENT_DEPTH.reset(token)   # 复位避免污染同进程后续测试
        await c.teardown()
    _run(s())


def test_delegate_role_note_reaches_child_prompt(tmp_path):
    """角色引导（system_note）必须注入子任务提示——否则角色只是装饰。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        out = await c.root.tasks.invoke(
            c.root, "agent.delegate", task="查一下配置项", role="explorer", tools=[])
        assert out["status"] == "ok", out
        # explorer 的 system_note 前缀「你是调研子 Agent」应出现在子 prompt 里；
        # fixture 会回显 prompt 前 40 字，故该前缀必然可见。
        assert "你是调研子 Agent" in out["answer"], out["answer"]
        await c.teardown()
    _run(s())


def test_delegate_passes_seam_params_to_real_factory(tmp_path):
    """经真工厂验证接缝参数真被传递：tools 收窄 / depth+1 / 子循环步数更紧。"""
    c = _build(tmp_path)
    seen = {}
    real_factory = None

    async def s():
        nonlocal real_factory
        await c.assemble()
        real_factory = c.root.get(Services.REACT_FACTORY)

        def spy(**kw):
            seen.update(kw)
            return real_factory(**kw)          # 真工厂照常构造，保证仍是真ReactLoop
        c.root.set(Services.REACT_FACTORY, spy)

        out = await c.root.tasks.invoke(
            c.root, "agent.delegate", task="委派测试", role="default",
            tools=["agent.ping", "memory.search"])
        assert out["status"] == "ok", out
        # 1) 请求 tools 真被收窄后交给工厂（交集语义；default 不额外收窄 ⇒ 等于请求集）
        ov = list(seen.get("tools_override") or [])
        assert "agent.ping" in ov and "memory.search" in ov, seen
        # 1b) 阶段 3（R1）：委派工具被**显式排除** ⇒ 子永远是叶子
        assert f"-{roles.LEAF_TOOL}" in ov, seen
        # 2) 深度 +1（顶层调用 ⇒ 子 depth=1）
        assert seen.get("depth") == 1, seen
        # 3) 子循环步数比父更紧（防子 Agent  runaway 烧钱）
        cfg = seen.get("cfg_overrides") or {}
        assert cfg.get("max_steps") == runner_mod.SUBAGENT_MAX_STEPS, seen
        assert cfg["max_steps"] <= 8, "子循环步数上限必须 ≤ 父的 8"
        # 4) 未传治理器覆盖（Q4 阶段 3 才启用）⇒ 本阶段保持 None，零返工锚点不被提前占用
        assert "governance_override" not in seen or seen["governance_override"] is None
        await c.teardown()
    _run(s())


def test_delegate_narrows_child_tools_below_parent(tmp_path):
    """安全下界（执行层）：子循环真调白名单外工具 ⇒ 被拒，不得执行。

    fixture 的「两步链路」脚本会真调 ``test.echo``。此例只给 ``tools=["agent.ping"]``，
    子循环仍点名 ``test.echo`` ⇒ 必须被**执行前**拦截（denied_by="whitelist"），
    否则说明白名单只是提示层软约束、越权仍能落地。
    """
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        out = await c.root.tasks.invoke(
            c.root, "agent.delegate", task="两步链路", tools=["agent.ping"])
        assert out["status"] == "ok", out
        ev = out.get("evidence") or {}
        obs = ev.get("observations") or []
        # 越权调用必须带whitelist 拒绝标记，且**内容**为拒绝而非执行结果
        wh = [o for o in obs if o.get("name") == "test.echo"]
        assert wh, f"未见test.echo 的观察：{obs}"
        assert wh[0].get("denied") is True and wh[0].get("denied_by") == "whitelist", wh[0]
        assert "不在本次委派的允许范围内" in (wh[0].get("content") or ""), wh[0]
        await c.teardown()
    _run(s())


def test_delegate_allows_whitelisted_tool(tmp_path):
    """反向对照：白名单**内**的工具必须真能执行（防拦截把正常路径也打死）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        # 注册 fixture 脚本会调的 test.echo，使其真实存在于目录
        _register_echo(c.root)
        # 白名单按**域**命中（plugins.testtools）⇒ 应放行
        out = await c.root.tasks.invoke(
            c.root, "agent.delegate", task="两步链路", tools=["plugins.testtools"])
        assert out["status"] == "ok", out
        obs = (out.get("evidence") or {}).get("observations") or []
        echo = [o for o in obs if o.get("name") == "test.echo"]
        assert echo, f"未见test.echo 观察：{obs}"
        assert echo[0].get("denied") is not True, f"白名单内工具被误拒：{echo[0]}"
        assert echo[0].get("is_error") is not True, echo[0]
        await c.teardown()
    _run(s())


def test_whitelist_none_path_unchanged(tmp_path):
    """未收窄工具面（tools=[]）⇒ 执行层不得误伤普通工具（仅排除委派工具，见下例）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        out = await c.root.tasks.invoke(
            c.root, "agent.delegate", task="两步链路", tools=[])
        assert out["status"] == "ok", out
        obs = (out.get("evidence") or {}).get("observations") or []
        echo = [o for o in obs if o.get("name") == "test.echo"]
        assert echo, obs
        assert echo[0].get("denied") is not True, \
            f"无白名单时不应有拦截：{echo[0]}"
        await c.teardown()
    _run(s())
