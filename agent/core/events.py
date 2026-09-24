"""agent.core.events —— 事件总线（双模式：emit 广播遥测 + waterfall 能力闸门）。

payload 一律纯数据（可 JSON 化）——「事件=扩展点，payload 只带数据」（anti 跨作用域泄漏）。
模式语义（契约级）：
    emit      不 await；同步按注册序、async 并发 fire（fire-and-forget）；错误隔离；只承载遥测/审计。
    waterfall await；监听收 (…原参数, next) 续接组合；「不调用 next() 即否决」；错误传播。

监听器绑定注册它的 ctx：分发时 ctx 已关闭（插件已卸载）→ 跳过并剪除（防死引用）。
在途的 fire-and-forget 异步任务由总线追踪，aclose() 时取消并等待，避免 teardown 后残留。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING, Any, Callable

from .errors import EventDispatchError, EventWiringError

if TYPE_CHECKING:
    from .context import Context

logger = logging.getLogger("agent.events")

_PURE = (type(None), bool, int, float, str)

_BAIL: Any = object()                      # 内部哨兵："无 bail 值"


def _is_bail(value: Any) -> bool:
    """bail 语义（对齐 Cordis）：非 None 且非 False 视为短路/决策值。"""
    return value is not None and value is not False


def _assert_pure_data(args: tuple[Any, ...], what: str) -> None:
    for a in args:
        if isinstance(a, _PURE):
            continue
        if isinstance(a, (list, tuple)):
            _assert_pure_data(tuple(a), what)
            continue
        if isinstance(a, dict):
            for k, v in a.items():
                if not isinstance(k, str):
                    raise TypeError(
                        f"event payload must be pure data: dict key must be str, "
                        f"got {type(k).__name__} in {what}"
                    )
                _assert_pure_data((v,), what)
            continue
        raise TypeError(
            f"event payload must be pure data; got {type(a).__name__} in {what} — "
            "禁止传出内核内部对象（防跨作用域泄漏）"
        )


class _Listener:
    """一条监听器记录：绑定注册它的 ctx + 执行序 + once + 是否 async（注册期缓存）。"""

    __slots__ = ("ctx", "handler", "once", "order", "is_async")

    def __init__(self, ctx: "Context", handler: Callable, once: bool, order: int, is_async: bool):
        self.ctx = ctx
        self.handler = handler
        self.once = once
        self.order = order
        self.is_async = is_async


class EventBus:
    """ctx.events —— 事件总线。注册走 ctx.on（挂 ctx 作用域）；分发走 ctx.events.<mode>。"""

    def __init__(self, strict_payload: bool = True, required_seams: set = set()):
        self._listeners: dict[str, list[_Listener]] = {}
        self._seq = 0
        self.strict_payload = strict_payload
        self._required_seams: set = set(required_seams)
        self._tasks: set[asyncio.Task] = set()   # 在途 fire-and-forget 任务（K4a 追踪）

    # ---------- 注册 ----------

    def on(self, ctx: "Context", name: str, handler: Callable,
           *, prepend: bool = False, once: bool = False) -> Callable[[], bool]:
        """注册监听器；返回 disposer（移除并返回是否仍注册）。上下文 = 注册 ctx。"""
        self._seq += 1
        is_async = inspect.iscoroutinefunction(handler)
        entry = _Listener(ctx, handler, once, self._seq, is_async)
        bucket = self._listeners.setdefault(name, [])
        if prepend:
            bucket.insert(0, entry)
        else:
            bucket.append(entry)

        def dispose() -> bool:
            lst = self._listeners.get(name)
            if lst and entry in lst:
                lst.remove(entry)
                if not lst:
                    self._listeners.pop(name, None)
                return True
            return False

        return dispose

    def listeners(self, name: str) -> list[str]:
        """调试：某事件的 (ctx.name, once) 列表（自动剔除已关闭作用域）。"""
        return [(e.ctx.name, e.once) for e in self._listeners.get(name, ())]

    # ---------- 分发：双模式 ----------

    async def emit(self, name: str, *args: Any) -> None:
        """遥测/审计：同步按注册序、async 并发 fire（fire-and-forget）；错误互不影响。"""
        if self.strict_payload:
            _assert_pure_data(args, f"emit({name!r})")
        for entry in self._snapshot(name):
            if entry.ctx.closed:
                continue
            if entry.is_async:
                task = asyncio.create_task(self._guarded(entry, name, args))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            else:
                try:
                    entry.handler(*args)
                except Exception:  # noqa: BLE001 —— emit 隔离语义
                    self._report_error(name, entry, args)
        self._prune(name)

    async def waterfall(self, name: str, *args: Any) -> Any:
        """中间件组合：监听收 (…原参数, next)；next() 续接，不调用即否决。

        链底 = 原参数（中间件原地改写即生效）；任一监听不调 next() 直接返回 ⇒ 其返回值即最终决策。
        返回最外层值；错误传播。
        """
        if self.strict_payload:
            _assert_pure_data(args, f"waterfall({name!r})")
        chain = self._snapshot(name)
        if name in self._required_seams and not chain:
            raise EventWiringError(
                f"capability 接缝 {name!r} 要求 ≥1 个实时监听（required_seams），"
                f"但分发瞬间 0 监听——闸门静默打开"
                f"（装配期校验漏检，或运行期监听被剪除）"
            )
        origin = args if len(args) != 1 else args[0]

        async def next_from(index: int) -> Any:
            if index >= len(chain):
                return origin                      # 链底：原参数（多参数元组 / 单参数本身）
            entry = chain[index]
            if entry.ctx.closed:
                return await next_from(index + 1)

            async def next_() -> Any:
                return await next_from(index + 1)

            payload = (*args, next_)
            return await self._invoke_one(entry, payload, name)

        try:
            return await next_from(0)
        finally:
            self._prune(name)

    async def parallel(self, name: str, *args: Any) -> None:
        """并发 await 全部；任一失败 → 聚合 EventDispatchError。"""
        if self.strict_payload:
            _assert_pure_data(args, f"parallel({name!r})")
        entries = [e for e in self._snapshot(name) if not e.ctx.closed]
        if not entries:
            return
        results = await asyncio.gather(
            *(self._invoke_one(e, args, name) for e in entries), return_exceptions=True
        )
        self._prune(name)
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            raise EventDispatchError(f"parallel({name!r}): 聚合 {len(errors)} 个失败")

    async def serial(self, name: str, *args: Any) -> Any:
        """注册序完整执行；返回首个 bail 值（若有）；错误中止传播。"""
        if self.strict_payload:
            _assert_pure_data(args, f"serial({name!r})")
        found: Any = _BAIL
        for entry in self._snapshot(name):
            if entry.ctx.closed:
                continue
            result = await self._invoke_one(entry, args, name)
            if found is _BAIL and _is_bail(result):
                found = result
        self._prune(name)
        return None if found is _BAIL else found

    async def bail(self, name: str, *args: Any) -> Any:
        """注册序，首个「非 None 非 False」值即短路返回；无值返回 None；错误传播。"""
        if self.strict_payload:
            _assert_pure_data(args, f"bail({name!r})")
        found: Any = None
        for entry in self._snapshot(name):
            if entry.ctx.closed:
                continue
            result = await self._invoke_one(entry, args, name)
            if _is_bail(result):
                found = result
                break
        self._prune(name)
        return found

    # ---------- 内部 ----------

    async def aclose(self) -> None:
        """取消并等待所有在途 fire-and-forget 监听器任务（teardown 时调用，避免残留）。"""
        pending = list(self._tasks)
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except BaseException:  # noqa: BLE001 —— 取消 / 监听器自身异常均吞掉
                pass
        self._tasks.clear()

    def _snapshot(self, name: str) -> list[_Listener]:
        return list(self._listeners.get(name, ()))

    def _prune(self, name: str) -> None:
        bucket = self._listeners.get(name)
        if bucket:
            bucket[:] = [e for e in bucket if not e.ctx.closed]
            if not bucket:
                self._listeners.pop(name, None)

    async def _invoke_one(self, entry: _Listener, args: tuple[Any, ...], name: str) -> Any:
        try:
            result = entry.handler(*args)
            if inspect.isawaitable(result):
                return await result
            return result
        finally:
            if entry.once:
                bucket = self._listeners.get(name)
                if bucket and entry in bucket:
                    bucket.remove(entry)
                    if not bucket:
                        self._listeners.pop(name, None)

    async def _guarded(self, entry: _Listener, name: str, args: tuple[Any, ...]) -> None:
        """fire-and-forget 的 async 监听器守卫：错误只记日志（异常不向外传播）。"""
        try:
            if not entry.ctx.closed:
                await self._invoke_one(entry, args, name)
        except Exception:  # noqa: BLE001
            self._report_error(name, entry, args)

    def _report_error(self, name: str, entry: _Listener, args: tuple[Any, ...]) -> None:
        logger.exception("listener error on %r (ctx=%s): args=%r", name, entry.ctx.name, args)

    def __repr__(self) -> str:
        return f"EventBus(events={list(self._listeners)})"


__all__ = ["EventBus"]
