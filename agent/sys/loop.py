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

from agent.api import plugin


class AgentDriver(Protocol):
    async def run(self, ctx, *, task: str, params: dict) -> "RunResult": ...


@dataclass(slots=True)
class RunResult:
    ok: bool
    result: Any = None
    error: str | None = None
    skipped: bool = False
    span_ms: int = 0


class ProduceDriver:
    """裁决驱动：执行一次任务（简化 target 模型——任务=注册表里可调用的目标）。"""

    async def run(self, ctx, *, task: str, params: dict | None = None) -> RunResult:
        params = params or {}
        running = ctx.state.setdefault("loop_running", set())
        if task in running:
            return RunResult(ok=False, skipped=True, error=f"overlap skip: {task} 上一轮未结束")
        running.add(task)
        started = time.monotonic()
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


@plugin
class Loop:
    """ctx.loop —— 主循环（裁决驱动）。loop-as-plugin：可换另一个 driver 插件。"""

    name = "agent.loop"
    layer = "system"
    provides = ["loop"]
    inject = ["tasks", "events"]

    async def apply(self, ctx) -> None:
        ctx.provide("loop", ProduceDriver())


__all__ = ["AgentDriver", "Loop", "ProduceDriver", "RunResult"]