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

import pytest

from agent.api import AgentError, Events, Services, StateKeys
from agent.core.session import make_identity
from agent.plugins.react import React
from agent.plugins.subagent import runner as runner_mod
from agent.plugins.subagent import SubAgent
from agent.plugins.subagent.roles import Role, combine_tools, get_role
from agent.profiles import build_agent


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

    def has(self, key):
        return self._has if key == Services.REACT_FACTORY else False

    def get(self, key, default=None):
        return self._factory if key == Services.REACT_FACTORY else default


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
        lambda name: Role("custom", "d", ["a"], ""))
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
        assert cat["agent.delegate"]["required"] == ["task"]
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
        assert seen.get("tools_override") == ["agent.ping", "memory.search"], seen
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
    """无 tools_override（tools=[]，父循环 / 不委派）⇒ 执行层零行为变化，工具照常执行。"""
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
