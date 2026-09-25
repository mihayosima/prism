"""agent.sys.audit —— 兼容层（D4 后实现已迁 ``agent.core.audit``）。

历史引用方已全部改为 ``from agent.api import durable, now_iso``；
本模块仅为过渡期保留别名，新代码禁止再 ``from agent.sys.audit import ...``
（守卫测试 tests/test_decouple_guards 断言生产代码零引用）。
"""

from __future__ import annotations

from agent.core.audit import durable, now_iso

_now_iso = now_iso          # 兼容别名（旧私有名）

__all__ = ["durable", "now_iso", "_now_iso"]
