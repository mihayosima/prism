"""agent.core.container —— 插件装配容器（无特权内核 · slim Cordis）。

- ``@plugin`` 装饰（类或函数）：name/inject/provides/layer/version 元数据；apply(ctx)→start(ctx)；
  teardown 经 ctx.effect 对称展开（注册即逆）。
- 依赖解析 = inject 拓扑（Kahn）；缺失给缺失清单；环报错；服务键唯一（D-3）。
- 装配/启动按拓扑序；卸载逆序回收每插件（capture/release + effect 逆序）。
- 硬化：apply 期读某服务必须已声明 inject（不靠字母序巧合）——未声明则 ctx.get=default 或属性读报错。

零业务词（test_agent_purity 守卫）。
"""

from __future__ import annotations

import inspect
from typing import Any, Callable, Mapping, Sequence

from .context import Context
from .errors import AgentError, CyclicDependencyError, MissingDependencyError
from .events_catalog import REQUIRED_SUBSCRIBER_EVENTS
from .event_wiring import _verify_event_wiring


def _coerce_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


class Plugin:
    """插件描述：元数据 + apply/start 钩子。由 @plugin 生成，或手写 dict。"""

    def __init__(
        self,
        raw: Any,
        *,
        name: str,
        version: str = "1.0.0",
        inject: Sequence[str] | tuple[str, ...] = (),
        provides: Sequence[str] | tuple[str, ...] = (),
        start: Callable[[Context], Any] | None = None,
    ):
        self._raw = raw
        self.name = name
        self.version = version
        self.layer = getattr(raw, "layer", "business")  # system=系统机制 / business=业务插件
        self.inject = _coerce_list(inject)
        self.provides = _coerce_list(provides)
        self._start_hook = start
        self._apply_is_class = isinstance(raw, type)
        self._instance: Any = None

    @property
    def id(self) -> str:
        return self.name

    @property
    def description(self) -> str:
        return str(getattr(self._raw, "description", "") or "")

    @property
    def tables(self) -> list[str]:
        """业务自带表（类属性 `tables`，控制台/插件页聚合用；缺省空）。"""
        if isinstance(self._raw, type):
            return list(getattr(self._raw, "tables", ()) or ())
        return []

    @property
    def params_schema(self) -> dict:
        """任务参数声明（类属性 `params_schema`，可选）。"""
        f = getattr(self._raw, "params_schema", None)
        return dict(f) if isinstance(f, dict) else {}

    def _obj(self) -> Any:
        """类插件按需实例化（容器管理单例）；函数插件即函数本身。"""
        if not self._apply_is_class:
            return self._raw
        if self._instance is None:
            self._instance = self._raw()
        return self._instance

    async def apply(self, ctx: Context) -> None:
        obj = self._obj()
        if self._apply_is_class:
            return await _awaitable(obj.apply(ctx), f"plugin <{self.name}>.apply")
        return await _awaitable(obj(ctx), f"plugin <{self.name}>.apply")

    async def start(self, ctx: Context) -> None:
        if self._apply_is_class:
            obj = self._obj()
            if hasattr(obj, "start"):
                return await _awaitable(obj.start(ctx), f"plugin <{self.name}>.start")
            return
        if self._start_hook is None:
            return
        return await _awaitable(self._start_hook(ctx), f"plugin <{self.name}>.start")

    def __repr__(self) -> str:
        return f"Plugin({self.name!r} v{self.version}, inject={self.inject}, provides={self.provides})"


def plugin(
    arg: Any = None,
    *,
    name: str | None = None,
    version: str | None = None,
    inject: Sequence[str] | None = None,
    provides: Sequence[str] | None = None,
    start: Callable[[Context], Any] | None = None,
) -> Any:
    """``@plugin`` 装饰器：类（class-level 属性声明）或函数（函数体即 apply）。"""
    if arg is None:
        return lambda obj: _wrap_plugin(
            obj, name=name, version=version, inject=inject, provides=provides, start=start
        )
    return _wrap_plugin(arg, name=name, version=version, inject=inject, provides=provides, start=start)


def _wrap_plugin(
    obj: Any,
    *,
    name: str | None,
    version: str | None,
    inject: Sequence[str] | None,
    provides: Sequence[str] | None,
    start: Callable[[Context], Any] | None,
) -> Plugin:
    if isinstance(obj, type):
        p_name = name or getattr(obj, "name", obj.__name__)
        p_version = version or getattr(obj, "version", "1.0.0")
        p_inject = inject if inject is not None else getattr(obj, "inject", ())
        p_provides = provides if provides is not None else getattr(obj, "provides", ())
        return Plugin(obj, name=p_name, version=p_version, inject=p_inject, provides=p_provides)
    fn = obj
    return Plugin(fn, name=name or fn.__name__, version=version or "1.0.0",
                  inject=inject or (), provides=provides or (), start=start)


async def _awaitable(result: Any, what: str) -> Any:
    """诚实边界：apply/start 一律 async；返回非 awaitable 即拒绝。"""
    if inspect.isawaitable(result):
        return await result
    raise AgentError(f"{what} must be async (returned non-awaitable {result!r})")


class Container:
    """插件装配容器：内置服务预置 + 拓扑排序 + apply/start + 对称 teardown。"""

    def __init__(self, builtin: Mapping[str, Any] | None = None, name: str = "agent",
                 verify_wiring: bool = True):
        self._root = Context(name=name)
        for key, value in (builtin or {}).items():
            self._root.provide(key, value)
        self._plugins: dict[str, Plugin] = {}
        self._order: list[Plugin] = []
        self._caps: dict[str, tuple[frozenset[str], int]] = {}
        self._assembled = False
        # P3：capability 接缝接线校验开关（默认开；裸/裁剪容器显式 False 豁免）。
        self._verify_wiring = verify_wiring

    @property
    def root(self) -> Context:
        return self._root

    @property
    def assembled(self) -> bool:
        return self._assembled

    def _wiring_required(self) -> set[str]:
        """单源：装配期校验 + 运行期 waterfall 守卫共用的 required seam 集合。
        verify_wiring=False → 返回空集，两处同时关闭，杜绝漂移。
        """
        if not self._verify_wiring:
            return set()
        return set(REQUIRED_SUBSCRIBER_EVENTS)

    def plugin_manifest(self) -> list[dict]:
        """已装配插件清单（通用 introspection；供元数据/诊断展示）。"""
        return [
            {"id": p.id, "layer": p.layer, "inject": list(p.inject), "provides": list(p.provides),
             "version": p.version, "description": p.description,
             "tables": list(p.tables), "params_schema": dict(p.params_schema)}
            for p in self._plugins.values()
        ]

    async def plugin_statuses(self) -> list[dict]:
        """可选自述状态钩子：插件类实现 ``async status(ctx) -> dict`` ⇒ 收集运行态摘要。

        未实现 status 返回 None（诚实：不给笼统假状态）；单插件异常标注 error，不拖垮整批。
        """
        out: list[dict] = []
        for p in self._plugins.values():
            obj = p._obj()
            fn = getattr(obj, "status", None)
            if fn is None or not inspect.iscoroutinefunction(fn):
                out.append({"id": p.id, "status": None})
                continue
            try:
                out.append({"id": p.id, "status": await fn(self._root)})
            except Exception as exc:  # noqa: BLE001 —— 单插件失败不拖垮
                out.append({"id": p.id, "status": {"error": str(exc)}})
        return out

    def add(self, *entries: Any) -> None:
        """登记插件（Plugin 实例 / @plugin 产物 / dict 描述）。可叠加多次。"""
        if self._assembled:
            raise AgentError("container already assembled; cannot add plugins")
        for entry in entries:
            p = entry if isinstance(entry, Plugin) else _from_dict(entry)
            if p.id in self._plugins:
                raise AgentError(f"duplicate plugin id {p.id!r} (D-3 注册唯一)")
            self._plugins[p.id] = p

    # ---------- 依赖解析（inject 拓扑） ----------

    def _resolve_order(self) -> list[Plugin]:
        providers: dict[str, str] = {}  # service key -> plugin id
        for p in self._plugins.values():
            for k in p.provides:
                if k in providers and providers[k] != p.id:
                    raise AgentError(
                        f"service {k!r} provided by both <{providers[k]}> and <{p.id}>; "
                        "同一键由单一插件提供（D-3）"
                    )
                providers[k] = p.id

        indeg: dict[str, int] = {pid: 0 for pid in self._plugins}
        deps: dict[str, list[str]] = {pid: [] for pid in self._plugins}
        missing: list[str] = []
        for pid, p in self._plugins.items():
            for k in p.inject:
                if self._root.has(k):      # 内置服务满足
                    continue
                owner = providers.get(k)
                if owner is None:
                    missing.append(f"<{pid}> needs {k!r}")
                    continue
                if owner == pid:
                    indeg[pid] += 1        # 自依赖：propagate 为环
                    deps[pid].append(pid)
                    continue
                indeg[pid] += 1
                deps[owner].append(pid)
        if missing:
            raise MissingDependencyError(missing)

        order: list[Plugin] = []
        ready = [pid for pid, d in indeg.items() if d == 0]
        while ready:
            ready.sort()                   # 确定序，便于调试/复现
            pid = ready.pop(0)
            order.append(self._plugins[pid])
            for dep in deps[pid]:
                indeg[dep] -= 1
                if indeg[dep] == 0:
                    ready.append(dep)
        if len(order) != len(self._plugins):
            remaining = [pid for pid, d in indeg.items() if d > 0]
            raise CyclicDependencyError(_find_cycle(self._plugins, providers, remaining))
        return order

    # ---------- 装配 / 启动 / 卸载（对称） ----------

    async def assemble(self) -> None:
        """拓扑序 apply 全部插件。

        失败回滚：任一插件 apply 抛错 → 先释放该插件已注册的部分 effect/服务（对称收尾，
        如未闭合的资源句柄），再逆序回收已完成插件，root 收尾后可安全重建。
        """
        if self._assembled:
            raise AgentError("container already assembled")
        order = self._resolve_order()
        try:
            for p in order:
                cap = self._root.capture()
                self._root._current_plugin = p.id
                try:
                    await p.apply(self._root)
                except BaseException:
                    # 该插件部分已注册（如服务/effect）→ 释放其区间，防资源悬挂
                    try:
                        await self._root.release(cap)
                    except BaseException:  # noqa: BLE001
                        pass
                    raise
                finally:
                    self._root._current_plugin = None
                self._caps[p.id] = cap
            self._order = order
            self._assembled = True
            # P3：capability 接缝接线校验（装配期 fail-fast）+
            #     同源注入 bus.required_seams（运行期 waterfall 守卫）。
            bus = self._root.get("events")
            if bus is not None:
                bus.required_seams = self._wiring_required()
                if self._verify_wiring:
                    _verify_event_wiring(self, bus)
        except BaseException:
            # 已完成插件的区间逆序回收 + root 收尾，随后原样抛出
            for cap in list(self._caps.values())[::-1]:
                try:
                    await self._root.release(cap)
                except BaseException:  # noqa: BLE001
                    pass
            self._caps.clear()
            try:
                await self._root.teardown()
            except BaseException:  # noqa: BLE001
                pass
            raise

    async def start(self) -> None:
        """拓扑序 start 全部插件。"""
        self._require_assembled()
        for p in self._order:
            self._root._current_plugin = p.id
            try:
                await p.start(self._root)
            finally:
                self._root._current_plugin = None

    async def teardown(self) -> None:
        """对称卸载：逆拓扑序回收每插件（effect 逆序 + 服务回收），随后 root 收尾。"""
        if not self._assembled:
            return
        errors: list[BaseException] = []
        for p in reversed(self._order):
            try:
                await self._root.release(self._caps[p.id])
            except BaseException as exc:  # noqa: BLE001 —— 聚合，保证后续插件仍卸载
                errors.append(exc)
        try:
            await self._root.teardown()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        self._plugins.clear()
        self._order.clear()
        self._caps.clear()
        self._assembled = False
        if errors:
            from .errors import TeardownError

            raise TeardownError(self._root.name, errors)

    def _require_assembled(self) -> None:
        if not self._assembled:
            raise AgentError("container not assembled yet; call assemble() first")


def _from_dict(entry: Any) -> Plugin:
    if isinstance(entry, dict):
        return Plugin(
            raw=entry["apply"],
            name=entry["name"],
            version=entry.get("version", "1.0.0"),
            inject=entry.get("inject", ()),
            provides=entry.get("provides", ()),
            start=entry.get("start"),
        )
    raise TypeError(f"container.add() expects Plugin/@plugin/dict, got {type(entry).__name__}")


def _find_cycle(plugins: Mapping[str, Plugin], providers: Mapping[str, str],
                remaining: Sequence[str]) -> list[str]:
    """在残余（环内）节点中 DFS 找出一个可读环路径。"""
    adj: dict[str, list[str]] = {}
    for pid in remaining:
        adj.setdefault(pid, [])
        for k in plugins[pid].inject:
            owner = providers.get(k)
            if owner and owner in remaining and owner != pid:
                adj[pid].append(owner)
    visited: set[str] = set()
    stack: list[str] = []

    def dfs(node: str) -> list[str]:
        if node in stack:
            return stack[stack.index(node):] + [node]
        if node in visited:
            return []
        visited.add(node)
        stack.append(node)
        for nxt in adj.get(node, []):
            path = dfs(nxt)
            if path:
                return path
        stack.pop()
        return []

    for pid in remaining:
        path = dfs(pid)
        if path:
            return path
    return list(remaining)


__all__ = ["Container", "Plugin", "plugin"]