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

# ---------------------------------------------------------------------------
# 设置页开放预算上限（2026-09-28）：三处登记 + 覆盖映射端到端
# ---------------------------------------------------------------------------

def test_budget_setting_registered_in_settings_schema():
    """预算上限进设置页须**三处同步登记**（schema / 归属 / 默认值 / 前端 keys），
    沿用 S3 制度化范式；默认值须与 profiles.BASE_AGENT_CONFIG 同源。"""
    from agent.profiles import BASE_AGENT_CONFIG
    from agent.run import chat as srv

    k = "max_tokens_per_run"
    assert k in srv._SETTING_SCHEMA
    assert srv._PLUGIN_OF_KEY[k] == "plugins.budget", "归属错误：须经 plugins.budget 映射到 agent.budget"
    assert k in srv._SETTING_DEFAULTS
    base = ((BASE_AGENT_CONFIG.get("agent") or {}).get("budget") or {}).get("max_tokens_per_run")
    assert srv._SETTING_DEFAULTS[k] == base, "设置默认值与 profiles 默认漂移"
    assert srv._coerce_setting(k, "123456") == 123456          # 字符串 → int 钳制
    with pytest.raises(ValueError):
        srv._coerce_setting(k, 999)                            # 低于下限 1000
    with pytest.raises(ValueError):
        srv._coerce_setting(k, 99_999_999)                     # 高于上限 1000 万
    assert "'max_tokens_per_run'" in srv._PAGE, "未同步到前端 keys/字段表"


def test_budget_setting_override_takes_effect(tmp_path, monkeypatch):
    """覆盖文件 ``plugins.budget`` → ``_merge_cfg`` 映射 ``agent.budget`` → 装配生效。"""
    import argparse
    import json

    from agent.run import chat as srv

    ov_file = tmp_path / "overrides.json"
    ov_file.write_text(json.dumps({"plugins.budget": {"max_tokens_per_run": 123456}}),
                       encoding="utf-8")
    monkeypatch.setattr(srv, "_OVERRIDE_FILE", ov_file)

    args = argparse.Namespace(fixture=True, provider="fixture", model="fixture",
                              db=str(tmp_path / "agent.db"))
    cfg = srv._merge_cfg(args)
    assert cfg["agent"]["budget"]["max_tokens_per_run"] == 123456

    c = build_agent(config=cfg)

    async def s():
        await c.assemble()
        budget_cfg = ((c.root.config or {}).get("agent") or {}).get("budget") or {}
        assert budget_cfg.get("max_tokens_per_run") == 123456, "装配后预算上限未生效"
        await c.teardown()

    _run(s())
