"""预算护栏测试 —— 容器累计上限 + 新会话重置。"""

from __future__ import annotations

import asyncio

import pytest

from agent.api import CapabilityNotImplemented
from agent.profiles import build_agent


def _run(coro):
    return asyncio.run(coro)


def test_budget_cumulative_and_reset_on_new_session(tmp_path):
    c = build_agent(config={"db_path": str(tmp_path / "agent.db"),
                            "agent": {"budget": {"max_tokens_per_run": 50}}})

    async def s():
        await c.assemble()
        denied = False
        for _ in range(30):
            try:
                await c.root.llm.complete(c.root, [{"role": "user", "content": "x" * 40}],
                                          schema={}, caller="t")
            except CapabilityNotImplemented:
                denied = True
                break
        assert denied, "累计超限应触发预算拒绝"
        assert c.root.state.get("budget_spent", 0) > 0        # 拒绝发生在"将要超"时，故 < 上限
        # 新会话 → 清空累计
        await c.root.chat.reset(c.root, "s")
        assert c.root.state.get("budget_spent", 0) == 0
        # 重置后可再调用
        await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}],
                                  schema={}, caller="t")
        await c.teardown()

    _run(s())