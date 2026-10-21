"""agent.sys.events —— 事件总线服务装配。"""

from __future__ import annotations

from agent.api import EventBus, Services, plugin


@plugin
class EventsPlugin:
    """ctx.events —— EventBus。"""

    name = "agent.events"
    layer = "system"
    provides = [Services.EVENTS]

    async def apply(self, ctx) -> None:
        bus = EventBus()
        ctx.provide(Services.EVENTS, bus)
        ctx.effect(bus.aclose)   # teardown 时取消并等待在途 fire-and-forget 监听器任务


__all__ = ["EventsPlugin"]
