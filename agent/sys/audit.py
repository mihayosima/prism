"""agent.sys.audit —— 兼容层。

历史引用方已全部改为 ``from agent.api import durable, now_iso``；
本模块仅为过渡期保留别名，新代码禁止再 ``from agent.sys.audit import ...``
。
"""

from __future__ import annotations

from agent.core.audit import durable, now_iso

_now_iso = now_iso          # 兼容别名

__all__ = ["durable", "now_iso", "_now_iso"]
