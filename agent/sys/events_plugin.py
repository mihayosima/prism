"""agent.sys.events —— 事件总线服务装配。"""

from __future__ import annotations

from agent.api import EventBus, Services, plugin


@plugin
class EventsPlugin:
    """ctx.events —— EventBus（五模式 + strict-payload）。"""

    name = "agent.events"
    layer = "system"
    provides = [Services.EVENTS]

    async def apply(self, ctx) -> None:
        ctx.provide(Services.EVENTS, EventBus())


__all__ = ["EventsPlugin"]