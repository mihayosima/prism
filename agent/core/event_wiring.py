"""agent.core.event_wiring —— capability 接缝接线校验（装配期 fail-fast）。

与 ``EventBus.required_seams``（运行期 waterfall 守卫）**同源**：两者都从
``Container._wiring_required()`` 取值，杜绝"装配跳过校验、运行期 waterfall 才崩"的最坏情形。

职责：在每个 required seam 上确认"实时监听数 ≥ 1"（即 capability 闸门确实挂上），
否则在装配期即抛 ``EventWiringError``——fail-fast 既告知"何时崩"也告知"怎么修"。
"""

from __future__ import annotations

from .errors import EventWiringError
from .events_catalog import Events, REQUIRED_SUBSCRIBER_EVENTS, SEAM_SUBSCRIBERS


def _const_name(value: str) -> str:
    """事件值反查常量名（用于报错信息，便于对照 Events.X）。"""
    for k, v in vars(Events).items():
        if not k.startswith("_") and v == value:
            return k
    return value


def _verify_event_wiring(container, bus) -> None:
    """装配末尾校验：每个 required seam 必须有 ≥ 1 个实时监听。

    报错带齐三要素：哪个 seam、期望谁挂（SEAM_SUBSCRIBERS）、当前挂了谁（bus.listeners）。
    """
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
