"""agent.sys.loop —— 裁决驱动主循环（loop-as-plugin）。

- ``AgentDriver`` 协议：消费方只依赖接口，不依赖具体 loop 实现 —— 换驱动 = 换插件。
- ``ProduceDriver``：跑一次 task（``tasks.resolve`` → ``run(ctx,**params)``）：
  重叠 skip + deadline 上限 + 单次异常捕获（不 kill 调度）。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Protocol

from agent.api import Services, plugin


class AgentDriver(Protocol):
    """驱动协议（loop-as-plugin：换驱动 = 换插件，消费方只依赖接口）。

    ``cancel`` / ``when_idle`` 为 B6 恢复的能力（详见《B5B6-流式与取消-设计方案.md》）：
    取消在**任意 await 点**即时生效；``when_idle`` 等驱动进入空闲，供优雅停止与 A4 提问时机用。
    """

    async def run(self, ctx, *, task: str, params: dict) -> "RunResult": ...
    async def cancel(self) -> None: ...
    async def when_idle(self) -> None: ...


@dataclass(slots=True)
class RunResult:
    ok: bool
    result: Any = None
    error: str | None = None
    skipped: bool = False
    span_ms: int = 0


class ProduceDriver:
    """裁决驱动：执行一次任务（简化 target 模型——任务=注册表里可调用的目标）。"""

    def __init__(self):
        self._running: dict[str, asyncio.Task] = {}      # task 名 -> 在跑的 asyncio 任务（供 cancel）

    async def run(self, ctx, *, task: str, params: dict | None = None) -> RunResult:
        params = params or {}
        running = ctx.state.setdefault("loop_running", set())
        if task in running:
            return RunResult(ok=False, skipped=True, error=f"overlap skip: {task} 上一轮未结束")
        running.add(task)
        started = time.monotonic()
        cur = asyncio.current_task()
        if cur is not None:
            self._running[task] = cur
        try:
            entry = ctx.tasks.resolve(ctx, task)
            if entry is None:
                return RunResult(ok=False, error=f"任务不存在: {task!r}")
            deadline = ((ctx.config or {}).get("agent") or {}).get("loop", {}).get("deadline_s", 60)
            result = await asyncio.wait_for(entry(**params), timeout=deadline)
            return RunResult(ok=True, result=result, span_ms=int((time.monotonic() - started) * 1000))
        except asyncio.TimeoutError:
            return RunResult(ok=False, error=f"deadline 超时（> {params}）")
        except Exception as exc:  # noqa: BLE001 —— 单次任务失败不 kill 循环
            return RunResult(ok=False, error=str(exc))
        finally:
            running.discard(task)
            self._running.pop(task, None)

    async def cancel(self) -> None:
        """取消全部在跑的任务——`Task.cancel()` 在任意 await 点即时生效。"""
        for t in list(self._running.values()):
            t.cancel()

    async def when_idle(self) -> None:
        """等待驱动进入空闲（无在跑任务）。供优雅停止与 A4 提问时机使用。"""
        while self._running:
            await asyncio.sleep(0.01)


@plugin
class Loop:
    """ctx.loop —— 主循环（裁决驱动）。loop-as-plugin：可换另一个 driver 插件。"""

    name = "agent.loop"
    layer = "system"
    provides = [Services.LOOP]
    inject = [Services.TASKS, Services.EVENTS]

    async def apply(self, ctx) -> None:
        ctx.provide(Services.LOOP, ProduceDriver())


__all__ = ["AgentDriver", "Loop", "ProduceDriver", "RunResult"]