"""装配期校验 capability 接缝接线：每个 required seam 必须有实时监听，否则抛 ``EventWiringError``。"""

from __future__ import annotations

from .errors import EventWiringError
from .events_catalog import Events, REQUIRED_SUBSCRIBER_EVENTS, SEAM_SUBSCRIBERS


def _const_name(value: str) -> str:
    """事件值反查常量名。"""
    for k, v in vars(Events).items():
        if not k.startswith("_") and v == value:
            return k
    return value


def _verify_event_wiring(container, bus) -> None:
    """检查每个 required seam 的监听数，为 0 则抛 ``EventWiringError``。"""
    for seam in REQUIRED_SUBSCRIBER_EVENTS:
        current = bus.listeners(seam)
        if not current:
            expected = SEAM_SUBSCRIBERS.get(seam, [])
            raise EventWiringError(
                f"capability 接缝 {seam!r}（{_const_name(seam)}）缺少实时监听——"
                f"安全闸门静默打开。\n"
                f"  期望订阅方: {expected or '（未声明，请在 events_catalog.SEAM_SUBSCRIBERS 补充）'}\n"
                f"  当前订阅方: 无（0 监听）。\n"
                f"  排查：guard / budget 是否正常 apply？其 ctx.on(Events.{_const_name(seam)}) 是否执行？"
            )


__all__ = ["_verify_event_wiring"]
