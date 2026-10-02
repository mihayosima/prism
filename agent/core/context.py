"""agent.core.context —— 服务仓 Context（slim Cordis）。

- 服务注册：``ctx.provide(key, value) -> detach``；D-3 同作用域重复注册抛冲突。
- 服务读取：``ctx.<key>`` ≡ ``ctx.get(key, default)``；缺失属性读抛 ServiceNotFoundError（附可用键清单）。
- 可逆副作用：``ctx.effect(disposer)``；作用域 teardown 逆序执行（对称卸载）。
- 事件监听：``ctx.on / ctx.once``，依赖 ``events`` 服务；teardown 对称移除。
- 子作用域：``ctx.child / ctx.scope``（GC 单元）。**当前生产代码仅 ``sys/progress.py`` 一处
  在用**（SSE 订阅作用域），其余是预留能力——注意子 ctx 的 ``state`` **不继承父级**（见 ``child()``）。
  插件级 ``capture/release`` 供容器对称回收。

零业务词（test_agent_purity 守卫）。
"""

from __future__ import annotations

import contextvars
import inspect
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from .errors import (
    ScopeClosedError,
    ServiceConflictError,
    ServiceNotFoundError,
    TeardownError,
)
from .services_catalog import Services

_MISSING: Any = object()

# 当前插件 id：容器在装配/启动窗口内注入，监听 ctx 当前作用域。
# 用 ContextVar 而非 ctx 上的普通属性——并发安全（多轮可并行互不串扰），
# 且不再需要容器写私有属性（K5）。
CURRENT_PLUGIN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "agent_current_plugin", default=None
)


def current_plugin() -> str | None:
    """当前插件 id（容器装配/启动窗口内有效）。"""
    return CURRENT_PLUGIN.get()


class State:
    """作用域变量容器（``ctx.state``）：属性读写 + dict 语义二选一。"""

    def __init__(self, data: Mapping[str, Any] | None = None):
        self.__dict__.update(data or {})

    def __getattr__(self, key: str) -> Any:
        try:
            return self.__dict__[key]
        except KeyError:
            raise AttributeError(f"state has no variable {key!r}") from None

    def __setattr__(self, key: str, value: Any) -> None:
        self.__dict__[key] = value

    def __getitem__(self, key: str) -> Any:
        return self.__dict__[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.__dict__[key] = value

    def __contains__(self, key: str) -> bool:
        return key in self.__dict__

    def get(self, key: str, default: Any = None) -> Any:
        return self.__dict__.get(key, default)

    def setdefault(self, key: str, default: Any = None) -> Any:
        return self.__dict__.setdefault(key, default)

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def __repr__(self) -> str:
        return f"State({self.__dict__!r})"


class _Effect:
    """effect disposer 封装：支持 sync/async，只执行一次。"""

    __slots__ = ("raw", "ran")

    def __init__(self, raw: Callable[[], Any]):
        self.raw = raw
        self.ran = False

    async def run(self) -> None:
        self.ran = True
        result = self.raw()
        if inspect.isawaitable(result):
            await result


class Context:
    """服务容器 + 作用域（根 ctx 无 parent；child/scope 沿父链解析服务）。"""

    def __init__(self, parent: Context | None = None, name: str | None = None):
        self._parent = parent
        self._name = name or (f"{parent._name}/scope" if parent is not None else "root")
        self._services: dict[str, Any] = {}
        self._effects: list[_Effect] = []
        self._state = State()
        self._closed = False

    # ---------- 元信息 ----------

    @property
    def name(self) -> str:
        return self._name

    @property
    def root(self) -> "Context":
        node = self
        while node._parent is not None:
            node = node._parent
        return node

    @property
    def state(self) -> State:
        return self._state

    @property
    def closed(self) -> bool:
        return self._closed

    # ---------- 服务容器 ----------

    def provide(self, key: str, value: Any) -> Callable[[], bool]:
        """注册服务，返回 detach。D-3：同作用域重复注册抛 ServiceConflictError。"""
        self._assert_open()
        if key in self._services:
            raise ServiceConflictError(key, self._name)
        self._services[key] = value

        def detach() -> bool:
            if key in self._services:
                del self._services[key]
                return True
            return False

        return detach

    def get(self, key: str, default: Any = None) -> Any:
        """读取服务：自身 → 父链；关闭后只放行 default（防跨作用域泄漏）。"""
        if self._closed:
            return default
        node: Context | None = self
        while node is not None:
            if key in node._services:
                return node._services[key]
            node = node._parent
        return default

    def has(self, key: str) -> bool:
        return self.get(key, _MISSING) is not _MISSING

    def set(self, key: str, value: Any) -> None:
        """覆盖已提供服务（就近沿父链）；未提供报错。"""
        self._assert_open()
        node: Context | None = self
        while node is not None:
            if key in node._services:
                node._services[key] = value
                return
            node = node._parent
        raise ServiceNotFoundError(key, self._name, action="set（仅可覆盖已提供的服务）")

    def keys(self) -> list[str]:
        """当前可见服务键集（父链去重），供诊断/可用清单。"""
        seen: set[str] = set()
        node: Context | None = self
        while node is not None:
            seen.update(node._services)
            node = node._parent
        return sorted(seen)

    # ---------- 属性访问 ≡ 服务 lookup ----------

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        if not self.has(key):
            raise ServiceNotFoundError(key, self._name,
                                       action="读取", available=self.keys())
        return self.get(key)

    def __setattr__(self, key: str, value: Any) -> None:
        if key.startswith("_"):
            object.__setattr__(self, key, value)
            return
        self.set(key, value)  # 非下划线写 = 覆盖已提供服务（未提供报错）

    # ---------- 可逆副作用 ----------

    def effect(self, disposer: Callable[[], Any]) -> Callable[[], bool]:
        """注册 disposer（sync/async 均可）；作用域结束逆序执行。返回 detach。"""
        self._assert_open()
        entry = _Effect(disposer)
        self._effects.append(entry)

        def detach() -> bool:
            if entry in self._effects:
                self._effects.remove(entry)
                return True
            return False

        return detach

    # ---------- 事件监听注册（依赖 events 服务） ----------

    def on(self, name: str, handler: Callable, *,
           prepend: bool = False, once: bool = False) -> Callable[[], bool]:
        """注册事件监听器，绑定当前作用域（teardown 对称移除）。"""
        events = self.get(Services.EVENTS)
        if events is None or not hasattr(events, "on"):
            raise ServiceNotFoundError(Services.EVENTS, self._name,
                                       action="ctx.on（事件总线未装配为 events 服务）")
        dispose = events.on(self, name, handler, prepend=prepend, once=once)
        self.effect(dispose)
        return dispose

    def once(self, name: str, handler: Callable, *, prepend: bool = False) -> Callable[[], bool]:
        return self.on(name, handler, prepend=prepend, once=True)

    # ---------- 作用域生命周期 ----------

    def child(self, name: str | None = None) -> "Context":
        self._assert_open()
        return Context(parent=self, name=name)

    @asynccontextmanager
    async def scope(self, name: str | None = None) -> AsyncIterator["Context"]:
        child = self.child(name)
        try:
            yield child
        finally:
            await child.teardown()

    async def teardown(self) -> None:
        """逆序执行全部 effect 并关闭；聚合失败；已关闭幂等。"""
        if self._closed:
            return
        errors: list[BaseException] = []
        for entry in reversed(self._effects):
            try:
                if not entry.ran:
                    await entry.run()
            except BaseException as exc:  # noqa: BLE001 —— 聚合，不中断对称卸载
                errors.append(exc)
        self._closed = True
        self._services.clear()
        self._effects.clear()
        if errors:
            raise TeardownError(self._name, errors)

    # ---------- 插件作用域捕获 / 回收（容器装配用） ----------

    def capture(self) -> tuple[frozenset[str], int]:
        """快照当前服务键集 + effect 数 → 标记一个插件的增量区间边界。"""
        self._assert_open()
        return (frozenset(self._services), len(self._effects))

    async def release(self, cap: tuple[frozenset[str], int]) -> None:
        """回收插件区间：逆序执行该区间 effect + 卸载其新增服务（对称）。"""
        self._assert_open()
        svc_before, eff_before = cap
        errors: list[BaseException] = []
        for entry in reversed(self._effects[eff_before:]):
            try:
                if not entry.ran:
                    await entry.run()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
        del self._effects[eff_before:]
        for k in set(self._services) - set(svc_before):
            del self._services[k]
        if errors:
            raise TeardownError(self._name, errors)

    # ---------- 内部 ----------

    def _assert_open(self) -> None:
        if self._closed:
            raise ScopeClosedError(self._name)

    def __repr__(self) -> str:
        return f"Context({self._name!r}, services={self.keys()})"


__all__ = ["Context", "State", "current_plugin", "CURRENT_PLUGIN"]