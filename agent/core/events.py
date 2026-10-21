"""agent.core.events —— 事件总线。

payload 一律纯数据——「事件=扩展点，payload 只带数据」。
模式语义：
    emit      不 await；同步按注册序、async 并发 fire；错误隔离；只承载遥测/审计。
    waterfall await；监听收 (…原参数, next) 续接组合；「不调用 next() 即否决」；错误传播。

监听器绑定注册它的 ctx：分发时 ctx 已关闭→ 跳过并剪除。
在途的 fire-and-forget 异步任务由总线追踪，aclose() 时取消并等待，避免 teardown 后残留。，注：parallel / serial / bail 三模式已移除——无生产消费方，属 YAGNI，详见内核优化方案 K-5。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import TYPE_CHECKING, Any, Callable

from .errors import EventWiringError
from .obs import inc_counter

if TYPE_CHECKING:
    from .context import Context

logger = logging.getLogger("agent.events")

_PURE = (type(None), bool, int, float, str)


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
    """一条监听器记录：绑定注册它的 ctx、执行序、once 与是否 async。"""

    __slots__ = ("ctx", "handler", "once", "order", "is_async")

    def __init__(self, ctx: "Context", handler: Callable, once: bool, order: int, is_async: bool):
        self.ctx = ctx
        self.handler = handler
        self.once = once
        self.order = order
        self.is_async = is_async


class EventBus:
    """事件总线：注册走 ctx.on，分发走 emit / waterfall。"""

    # : fire-and-forget 在途任务上限；超出即丢弃并计数。
    DEFAULT_MAX_INFLIGHT = 256

    def __init__(self, strict_payload: bool = True, required_seams: set | None = None,
                 max_inflight: int | None = None):
        self._listeners: dict[str, list[_Listener]] = {}
        self._seq = 0
        self.strict_payload = strict_payload
        self._required_seams: set = set(required_seams or ())
        self._tasks: set[asyncio.Task] = set()   # 在途 fire-and-forget 任务
        self._max_inflight = int(max_inflight or self.DEFAULT_MAX_INFLIGHT)
        # : 因超过上限被丢弃的监听器次数
        self.dropped = 0

    @property
    def inflight(self) -> int:
        """当前在途 fire-and-forget 任务数。"""
        return len(self._tasks)

    # ---------- 注册 ----------

    def on(self, ctx: "Context", name: str, handler: Callable,
           *, prepend: bool = False, once: bool = False) -> Callable[[], bool]:
        """注册监听器，返回 disposer。"""
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
        """调试：某事件的 (ctx.name, once) 列表。"""
        return [(e.ctx.name, e.once) for e in self._listeners.get(name, ())]

    # ---------- 分发：双模式 ----------

    async def emit(self, name: str, *args: Any) -> None:
        """遥测/审计：同步按注册序、async 并发 fire；错误互不影响。

                在途任务有上限：此前只登记不设 cap ⇒ 高频事件遇到慢的
                异步监听器时会无界堆积。超限时丢弃该监听器并计数——
                刻意不排队、不阻塞：``emit`` 的契约就是"不 await"，
                阻塞会把遥测发射变成背压点，反过来拖慢业务主链路。
                丢弃是不诚实的，所以必须可见：
                ``dropped`` 计数 + ``agent_events_dropped_total`` 指标。
                ⚠️ 不影响 waterfall：闸门链是 ``await`` 的、有 ``required_seams`` 兜底，
                不经过这条路径 ⇒ 限流永远不会"悄悄打开闸门"。
                """
        if self.strict_payload:
            _assert_pure_data(args, f"emit({name!r})")
        for entry in self._snapshot(name):
            if entry.ctx.closed:
                continue
            if entry.is_async:
                if len(self._tasks) >= self._max_inflight:
                    self.dropped += 1
                    inc_counter("agent_events_dropped_total")
                    logger.warning(
                        "emit(%r) 在途监听器已满（%d）⇒ 丢弃一个异步监听器（遥测有缺）",
                        name, self._max_inflight)
                    continue
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

         链底 = 原参数；任一监听不调 next() 直接返回 ⇒ 其返回值即最终决策。
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
                return origin                      # 链底：原参数
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

    # ---------- 内部 ----------

    async def aclose(self) -> None:
        """取消并等待所有在途 fire-and-forget 监听器任务，teardown 时调用，避免残留。"""
        pending = list(self._tasks)
        for t in pending:
            t.cancel()
        for t in pending:
            try:
                await t
            except BaseException:  # noqa: BLE001 —— 取消 / 监听器自身异常均吞掉
                pass
        self._tasks.clear()

    async def idle(self) -> None:
        """等待全部在途 fire-and-forget 任务自然结束。

                与 ``aclose`` 的分工：``aclose`` 取消并等待，本方法只等待。

                为什么需要它：``emit`` 的 async 监听器是 fire-and-forget，绑定 DB 之后
                其落地时刻取决于事件循环轮次 + 线程池往返。用 ``await asyncio.sleep(0)`` × N 等待是碰运气——线程往返一慢就漏。
                本方法轮询到 ``_tasks`` 为空，是确定性等待。

                循环而非单次 ``gather``：在途监听器自身可能再 ``emit``，
                单次 gather 会漏掉派生出来的那批。
                """
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

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
        """fire-and-forget 的 async 监听器守卫：错误只记日志。"""
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
