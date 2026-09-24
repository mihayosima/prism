"""内核测试 —— Ctx / @plugin / Container / EventBus（emit / waterfall）/ effects / errors（离线确定性）。

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
    Services,
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
        c = Container(verify_wiring=False)
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
        c = Container(verify_wiring=False)
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
        c = Container(verify_wiring=False)
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
        c = Container(verify_wiring=False)
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
        c = Container(verify_wiring=False)
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
        c = Container(verify_wiring=False)
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
        c = Container(verify_wiring=False)
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
        c = Container(verify_wiring=False)
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


# ---------- EventBus（双模式：emit / waterfall） ----------

def _bus() -> tuple[Context, EventBus]:
    root = Context(name="t")
    ev = EventBus()
    root.provide(Services.EVENTS, ev)
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


def test_strict_payload_rejects_object():
    _, ev = _bus()
    with pytest.raises(TypeError):
        _run(ev.emit("t/bad", {"obj": object()}))


def test_aclose_cancels_inflight_async_listeners():
    """K4a：emit 飞行途中调用 aclose 能取消在途任务并清空追踪集。"""
    root, ev = _bus()
    started = []
    cancelled = []

    async def slow(payload):
        started.append(1)
        try:
            await asyncio.sleep(5)          # 模拟长耗时的异步监听器
        except asyncio.CancelledError:
            cancelled.append(1)
            raise

    root.on("t/async", slow)

    async def scenario():
        emit_task = asyncio.create_task(ev.emit("t/async", {}))
        # 轮询让出，直到 slow 真正开始运行（fire-and-forget 已入追踪集）
        for _ in range(50):
            await asyncio.sleep(0)
            if started:
                break
        assert started == [1]                   # slow 已开始运行
        assert ev._tasks                       # 在途任务在追踪集中
        await ev.aclose()                      # 取消并等待在途任务
        assert cancelled == [1]                # 在途任务确被取消
        assert ev._tasks == set()              # 追踪集清空
        assert emit_task.done()                # emit 包装任务已结束，无滞留

    _run(scenario())


def test_emit_async_task_auto_discarded_after_done():
    """K4a：异步监听器完成后，done 回调自动将其移出追踪集（无残留引用）。"""
    root, ev = _bus()
    done = []

    async def quick(payload):
        done.append(1)

    root.on("t/q", quick)

    async def scenario():
        await ev.emit("t/q", {})               # 同步等待 emit 协程返回
        # emit 把 quick 排成 fire-and-forget；轮询让出直到其完成且 done 回调已将其移出集
        for _ in range(50):
            await asyncio.sleep(0)
            if done and not ev._tasks:
                break
        assert done == [1]
        assert ev._tasks == set()              # 已完成任务不在追踪集中（无悬挂引用）

    _run(scenario())


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
        c = Container(verify_wiring=False)
        c.add(Reader)
        c.add(Provider)
        await c.assemble()
        assert captured["value"] is None      # 未声明依赖 → 拿不到（容错暴露，而非静默正确）

    _run(scenario())


# ---------- 诚实边界 ----------

def test_capability_not_implemented_honest():
    with pytest.raises(CapabilityNotImplemented):
        raise CapabilityNotImplemented("agent.llm.complete", "插槽占位")