"""agent.plugins.short_term.backoff —— 压缩失败递增退避状态机。

落实方案：递增退避 1m→10m→1h→24h + 失败粒度+ 作用域+ 恢复探测+ 分批补偿。

设计要点：
- 每失败原因独立计数与暂停；
- 连续失败 1/2/3/≥4 → 暂停 1/10/60/1440 分钟；
- 某原因成功一次 → 清零；
- 恢复探测失败时只续期当前暂停。
"""

from __future__ import annotations

import time
from typing import Callable


# 连续失败次数 → 暂停分钟数
DEFAULT_SCHEDULE = (1, 10, 60, 1440)


class BackoffState:
    """per-plugin、按原因分类的递增退避状态机。"""

    def __init__(self, schedule: tuple[int, ...] = DEFAULT_SCHEDULE,
                 now: Callable[[], float] | None = None):
        self._schedule = tuple(schedule) or DEFAULT_SCHEDULE
        self._now = now or time.time
        # reason -> {"count": int, "pause_until": float}
        self._state: dict[str, dict[str, float]] = {}

    # ---- 查询 ----

    def should_attempt(self, reason: str) -> bool:
        """该原因当前是否可尝试。"""
        st = self._state.get(reason)
        if st is None:
            return True
        return self._now() >= st["pause_until"]

    def current_tier_minutes(self, reason: str) -> int:
        """该原因当前档位；无状态返回 0。"""
        st = self._state.get(reason)
        if st is None:
            return 0
        count = int(st["count"])
        idx = min(count - 1, len(self._schedule) - 1)
        return self._schedule[max(0, idx)]

    def is_paused(self, reason: str) -> bool:
        return not self.should_attempt(reason)

    # ---- 状态变更 ----

    def record_failure(self, reason: str) -> int:
        """记录一次真实执行异常 → 计数 +1，进入对应档位暂停。返回当前档位分钟。"""
        st = self._state.setdefault(reason, {"count": 0, "pause_until": 0.0})
        st["count"] += 1
        tier = self.current_tier_minutes(reason)
        st["pause_until"] = self._now() + tier * 60.0
        return tier

    def record_success(self, reason: str) -> None:
        """成功一次 → 清零该原因。"""
        self._state.pop(reason, None)

    def record_probe_failure(self, reason: str) -> int:
        """恢复探测失败 → 仅续期当前暂停。返回续期后档位。"""
        st = self._state.get(reason)
        if st is None:
            return 0
        tier = self.current_tier_minutes(reason)
        st["pause_until"] = self._now() + tier * 60.0
        return tier

    def record_probe_success(self, reason: str) -> None:
        """恢复探测成功 → 等同成功，清零。"""
        self.record_success(reason)

    # ---- 诊断 ----

    def stats(self) -> dict[str, dict[str, float]]:
        return {k: dict(v) for k, v in self._state.items()}


__all__ = ["BackoffState", "DEFAULT_SCHEDULE"]
