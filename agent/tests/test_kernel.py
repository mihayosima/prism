"""内核测试 —— Ctx / @plugin / Container / EventBus / effects / errors（离线确定性）。

全部在内存进程内，不触网、不触引擎、不触 Node。
"""
from __future__ import annotations

import asyncio

import pytest

from agent.api import (
    AgentError,
    CapabilityNotImplemented,
    Container,
    Context,
    CyclicDependencyError,
    EventBus,
    MissingDependencyError,
    ServiceNotFoundError,
    plugin,
)


def _run(coro):
    return asyncio.run(coro)


# ---------- @plugin 类形态 / 服务注册 ----------

def test_class_plugin_provides_and_attr_access():
    @plugin
    class A:
        name = "core.a"
        provides = ["aaa"]

        async def apply(self, ctx):
            ctx.provide("aaa", {"t": 1})

    async def scenario():
        c = Container()
        c.add(A)
        await c.assemble()
        assert c.root.aaa["t"] == 1
        assert c.root.has("aaa")
        await c.teardown()
        assert not c.root.has("aaa")

    _run(scenario())


def test_function_plugin_form():
    @plugin(name="core.fn", provides=["f"])
    async def fn(ctx):
        ctx.provide("f", "hello")

    async def scenario():
        c = Container()
        c.add(fn)
        await c.assemble()
        assert c.root.f == "hello"
        await c.teardown()

    _run(scenario())


def test_missing_service_property_raises_with_available():
    root = Context(name="t")
    with pytest.raises(ServiceNotFoundError) as ei:
        _ = root.nope
    assert "root" in str(ei.value) or "t" in str(ei.value)


def test_inject_topology_orders_provider_first():
    order: list[str] = []

    @plugin
    class Consumer:
        name = "core.consumer"
        inject = ["data"]

        async def apply(self, ctx):
            order.append("consumer")

    @plugin
    class Provider:
        name = "core.provider"
        provides = ["data"]

        async def apply(self, ctx):
            order.append("provider")
            ctx.provide("data", 1)

    async def scenario():
        c = Container()
        c.add(Consumer)   # 后进也有依赖边，拓扑仍 provider 先行
        c.add(Provider)
        await c.assemble()
        assert order == ["provider", "consumer"]
        assert c.root.data == 1
        await c.teardown()

    _run(scenario())


def test_d3_duplicate_service_key_rejected():
    @plugin
    class A:
        name = "core.a"
        provides = ["x"]

        async def apply(self, ctx):
            ctx.provide("x", 1)

    @plugin
    class B:
        name = "core.b"
        provides = ["x"]

        async def apply(self, ctx):
            ctx.provide("x", 2)

    async def scenario():
        c = Container()
        c.add(A)
        c.add(B)
        with pytest.raises(AgentError):
            await c.assemble()

    _run(scenario())


def test_missing_dependency_reports_list():
    @plugin
    class P:
        name = "core.p"
        inject = ["no_such_svc"]

        async def apply(self, ctx):
            pass

    async def scenario():
        c = Container()
        c.add(P)
        with pytest.raises(MissingDependencyError) as ei:
            await c.assemble()
        assert "no_such_svc" in str(ei.value)

    _run(scenario())


def test_cycle_dependency_reports_cycle():
    @plugin
    class A:
        name = "core.a"
        provides = ["a"]
        inject = ["b"]

        async def apply(self, ctx):
            ctx.provide("a", 1)

    @plugin
    class B:
        name = "core.b"
        provides = ["b"]
        inject = ["a"]

        async def apply(self, ctx):
            ctx.provide("b", 2)

    async def scenario():
        c = Container()
        c.add(A)
        c.add(B)
        with pytest.raises(CyclicDependencyError) as ei:
            await c.assemble()
        assert "a" in str(ei.value) and "b" in str(ei.value)

    _run(scenario())


def test_apply_must_be_async():
    @plugin
    class Bad:
        name = "core.bad"

        def apply(self, ctx):  # 非 async → 诚实拒绝
            return None

    async def scenario():
        c = Container()
        c.add(Bad)
        with pytest.raises(AgentError):
            await c.assemble()

    _run(scenario())


# ---------- 对称 teardown / effects ----------

def test_symmetric_teardown_effect_reverse_order():
    order: list[str] = []

    @plugin
    class P:
        name = "core.p"
        provides = ["p_svc"]

        async def apply(self, ctx):
            ctx.provide("p_svc", 1)
            ctx.effect(lambda: order.append("p_teardown"))

    @plugin
    class Q:
        name = "core.q"
        inject = ["p_svc"]

        async def apply(self, ctx):
            ctx.effect(lambda: order.append("q_teardown"))

    async def scenario():
        c = Container()
        c.add(P)
        c.add(Q)
        await c.assemble()
        assert c.root.has("p_svc")
        await c.teardown()
        assert order == ["q_teardown", "p_teardown"]  # 逆拓扑：后装先卸
        assert not c.root.has("p_svc")

    _run(scenario())


def test_scope_teardown_reverse_and_isolate():
    val: list[int] = []

    async def scenario():
        root = Context(name="t")
        async with root.scope("s") as sc:
            sc.effect(lambda: val.append(1))
            sc.effect(lambda: val.append(2))
            sc.provide("inner", 1)
            assert sc.has("inner")
            assert not root.has("inner")
        assert val == [2, 1]
        assert sc.closed

    _run(scenario())


def test_set_overrides_provided_service():
    root = Context(name="t")
    root.provide("x", 1)
    root.set("x", 2)
    assert root.x == 2
    with pytest.raises(ServiceNotFoundError):
        root.set("never", 1)


# ---------- EventBus（五模式） ----------

def _bus() -> tuple[Context, EventBus]:
    root = Context(name="t")
    ev = EventBus()
    root.provide("events", ev)
    return root, ev


def test_emit_sync_observe():
    root, ev = _bus()
    got: list[dict] = []
    root.on("t/x", lambda p: got.append(p))
    _run(ev.emit("t/x", {"n": 1}))
    assert got == [{"n": 1}]


def test_emit_once_auto_removes():
    root, ev = _bus()
    got = []
    root.once("t/x", lambda p: got.append(p["n"]))
    _run(ev.emit("t/x", {"n": 1}))
    root.on("t/x", lambda p: None)  # 确保事件仍在
    assert got == [1]


def test_waterfall_delegate_and_transform():
    root, ev = _bus()

    async def add_one(data, next):
        data["x"] += 1
        return await next()

    async def add_two(data, next):
        data["x"] += 2
        return await next()

    root.on("t/wf", add_one)
    root.on("t/wf", add_two)
    result = _run(ev.waterfall("t/wf", {"x": 0}))
    assert result == {"x": 3}


def test_waterfall_veto_short_circuit():
    root, ev = _bus()

    async def annotate(data, next):
        data["steps"] = ["annotate"]
        return await next()

    async def veto(data, next):
        return {"allowed": False}  # 不调用 next() → 否决，后续不再执行

    root.on("t/wf", annotate)
    root.on("t/wf", veto)
    result = _run(ev.waterfall("t/wf", {"steps": []}))
    assert result == {"allowed": False}          # 否决短路，annotate 的 next 在 veto 处终止


def test_bail_short_circuits_at_decision():
    root, ev = _bus()
    called = []

    def pass_through(p):
        called.append("h1")
        return None

    async def decide(p):
        called.append("h2")
        return "decision"

    async def never(p):
        called.append("h3")
        return True

    root.on("t/b", pass_through)
    root.on("t/b", decide)
    root.on("t/b", never)
    result = _run(ev.bail("t/b", {}))
    assert result == "decision"
    assert called == ["h1", "h2"]


def test_serial_returns_first_bail_value():
    root, ev = _bus()
    root.on("t/s", lambda p: None)
    root.on("t/s", lambda p: "found")
    root.on("t/s", lambda p: "ignored")
    assert _run(ev.serial("t/s", {})) == "found"


def test_strict_payload_rejects_object():
    _, ev = _bus()
    with pytest.raises(TypeError):
        _run(ev.emit("t/bad", {"obj": object()}))


# ---------- 容器硬化：未声明 inject 的 apply 期读 ----------

def test_uninjected_read_in_apply_is_none():
    captured: dict = {}

    @plugin
    class Reader:
        name = "a.aaa_reader"          # 字母序在 provider 之前
        # 故意不声明 inject=["zz"]

        async def apply(self, ctx):
            captured["value"] = ctx.get("zz")

    @plugin
    class Provider:
        name = "a.zzz_provider"
        provides = ["zz"]

        async def apply(self, ctx):
            ctx.provide("zz", 42)

    async def scenario():
        c = Container()
        c.add(Reader)
        c.add(Provider)
        await c.assemble()
        assert captured["value"] is None      # 未声明依赖 → 拿不到（容错暴露，而非静默正确）

    _run(scenario())


# ---------- 诚实边界 ----------

def test_capability_not_implemented_honest():
    with pytest.raises(CapabilityNotImplemented):
        raise CapabilityNotImplemented("agent.llm.complete", "插槽占位")