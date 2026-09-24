"""ReAct 插件测试（``plugins.react``）—— 多步循环 / 失败即观察 / 三把闸 / 事件审计 /
    chat 委托与回退 / 权限预留 / 可管理性。全部离线确定性（fixture provider），不触网。

对应用例编号见《ReAct插件-设计方案.md》§10；fixture 脚本见 ``agent/fixtures/llm.py``
（``<!--react:turn-->`` 路由：关键词驱动、轮数确定）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.api import Services
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path, *, react: bool = True) -> PluginManager:
    """指向真实 agent/plugins 的管理器；期望态落 tmp，避免污染 data/。"""
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    if react:
        pm.set_enabled("plugins.react", True)
    return pm


def _build(tmp_path, *, react: bool = True, patch: dict | None = None):
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch, plugin_mgr=_pm(tmp_path, react=react))


async def _echo(text: str = "hi"):
    return {"echo": text}


def _register_echo(ctx) -> None:
    ctx.tasks.register(ctx, "test.echo",
                       schema={"text": {"type": "str", "required": False, "default": "hi"}},
                       handler=_echo, owner="test", description="回显参数")


# ---------------------------------------------------------------------------
# 用例 1 / 2 / 8：单步收敛 · 两步工具链 · 事件完整性
# ---------------------------------------------------------------------------


def test_single_step_converges(tmp_path):
    """用例 1：模型直接答复（无工具调用）→ 1 步收敛。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        assert c.root.has(Services.REACT)
        r = await c.root.react.run(c.root, "请直接回答这个问题")
        assert r.ok and r.steps == 1 and not r.exhausted
        assert "直接回答" in r.text
        assert r.tools == []
        await c.teardown()

    _run(s())


def test_two_step_tool_chain_and_durable_events(tmp_path):
    """用例 2 + 8：两步工具链（工具 → 再推理 → 收敛）；轨迹可由 durable 事件重建。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        r = await c.root.react.run(c.root, "两步链路：先回显再总结")
        assert r.ok and r.steps == 2 and not r.exhausted
        assert r.tools == ["test.echo"]
        assert "两步链路完成" in r.text
        rows = await c.root.db.query(
            c.root, "SELECT event FROM agent_events WHERE event LIKE 'agent/react/%'")
        events = [x["event"] for x in rows]
        assert "agent/react/start" in events
        assert "agent/react/step" in events
        assert "agent/react/final" in events
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 用例 3 / 4：失败即观察 · guard 拒绝与防死循环
# ---------------------------------------------------------------------------


def test_tool_error_becomes_observation_and_model_recovers(tmp_path):
    """用例 3：首次参数类型错 → error 观察 → 模型改参数 → 成功（整轮不中断）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        r = await c.root.react.run(c.root, "请自纠参数")
        assert r.ok and r.steps == 3 and not r.exhausted
        assert any(o["is_error"] for o in r.observations)          # 第一轮：错误观察
        assert any(not o["is_error"] for o in r.observations)      # 第二轮：成功
        assert "已纠正参数" in r.text
        await c.teardown()

    _run(s())


def test_guard_deny_observed_and_repeat_terminates(tmp_path):
    """用例 4：被 guard 拒 → error 观察；同一工具连拒超 ``max_rejects`` → 终止并如实说明。"""
    c = _build(tmp_path, patch={"agent": {"permissions": {"task_deny": ["test.echo"]},
                                          "react": {"max_rejects": 1}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        r = await c.root.react.run(c.root, "请自纠参数")
        assert r.exhausted and "反复被拒" in (r.reason or "")
        assert r.steps == 2
        assert r.observations and all(o["denied"] for o in r.observations)
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 用例 5 / 6：三把闸（步数 · 预算）
# ---------------------------------------------------------------------------


def test_max_steps_exhausted_is_honest(tmp_path):
    """用例 5：触顶（max_steps）→ ``exhausted=True``、文本如实说明、不抛异常。"""
    c = _build(tmp_path, patch={"agent": {"react": {"max_steps": 3}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        r = await c.root.react.run(c.root, "请触顶循环")
        assert r.ok and r.exhausted and r.steps == 3
        assert "max_steps" in (r.reason or "")
        assert "上限" in r.text
        rows = await c.root.db.query(
            c.root, "SELECT event FROM agent_events WHERE event='agent/react/exhausted'")
        assert rows                                                # 触顶也留审计
        await c.teardown()

    _run(s())


def test_token_budget_deny_exhausts_gracefully(tmp_path):
    """用例 6：预算拒绝 → 优雅收尾（不崩、不静默）；预算闸无需自己实现。"""
    c = _build(tmp_path, patch={"agent": {"budget": {"max_tokens_per_run": 1}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        r = await c.root.react.run(c.root, "两步链路")
        assert r.exhausted and "被拒" in (r.reason or "")
        assert "上限" in r.text
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 用例 7：上下文治理（结果截断）
# ---------------------------------------------------------------------------


def test_large_tool_result_truncated(tmp_path):
    """用例 7：超长工具结果被截断并**标注**（多步循环不会撑爆上下文）。"""
    c = _build(tmp_path, patch={"agent": {"react": {"tool_result_max_chars": 200}}})

    async def s():
        await c.assemble()

        async def _big():
            return "X" * 5000

        c.root.tasks.register(c.root, "test.big", schema={}, handler=_big, owner="test",
                              description="返回超大结果")
        r = await c.root.react.run(c.root, "请拿一次大结果")
        assert r.ok and not r.exhausted
        assert any("truncated" in o["content"] for o in r.observations)
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 用例 9 / 12：chat 委托与回退 · 启停组合矩阵
# ---------------------------------------------------------------------------


def test_chat_delegates_when_react_enabled(tmp_path):
    """用例 9a + 12：装配 react → chat 走多步（plan.intent == 'react'）。"""
    (tmp_path / "a").mkdir()
    c = _build(tmp_path / "a")

    async def s():
        await c.assemble()
        _register_echo(c.root)
        reply = await c.root.chat.send(c.root, "两步链路")
        assert reply.ok and reply.plan["intent"] == "react"
        assert reply.evidence["steps"] == 2
        await c.teardown()

    _run(s())


def test_chat_falls_back_when_react_disabled(tmp_path):
    """用例 9b + 12：停用 react → chat 回退单步路径（解耦证明，chat 契约未改）。"""
    (tmp_path / "b").mkdir()
    c = _build(tmp_path / "b", react=False)

    async def s():
        await c.assemble()
        assert not c.root.has(Services.REACT)
        reply = await c.root.chat.send(c.root, "两步链路")
        assert reply.ok
        assert reply.plan["intent"] != "react"                    # 单步规划路径
        assert "steps" not in reply.evidence
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 用例 10：权限模式接口预留（锁住"本次不改行为"）
# ---------------------------------------------------------------------------


def test_permission_mode_placeholder_keeps_full_tools(tmp_path):
    """用例 10：``permission_mode='plan'`` 下 ``visible_tools`` 占位仍返回全量。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        full = c.root.react.visible_tools(c.root)
        assert full and all("risk" in t for t in full)             # risk 元数据已就位
        c.root.state["permission_mode"] = "plan"
        assert len(c.root.react.visible_tools(c.root)) == len(full)  # 行为不变（预留）
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# L1 接缝：内部消息/工具目录 → OpenAI 兼容 wire（真实 provider 的落地前提）
# ---------------------------------------------------------------------------


def test_deepseek_wire_conversion_native_tools_shape():
    """加法式扩展的落地形状：``_wire_messages`` / ``_wire_tools`` / ``_wire_call``。"""
    from agent.sys.llm import _wire_call, _wire_messages, _wire_tools

    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do it"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "name": "test.echo", "arguments": {"text": "one"}}]},
        {"role": "user", "content": {"tool_results": [
            {"tool_call_id": "c1", "name": "test.echo", "content": '{"echo": "one"}',
             "is_error": False, "denied": False}]}},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c2", "name": "test.echo", "arguments": {}}]},
    ]
    wire = _wire_messages(msgs)
    assert wire[0] == {"role": "system", "content": "sys"}
    assert wire[1] == {"role": "user", "content": "do it"}
    assert wire[2]["content"] is None                       # 有 tool_calls → content 置空
    assert wire[2]["tool_calls"][0]["type"] == "function"
    assert wire[2]["tool_calls"][0]["function"]["name"] == "test.echo"
    assert json.loads(wire[2]["tool_calls"][0]["function"]["arguments"]) == {"text": "one"}
    assert wire[3] == {"role": "tool", "tool_call_id": "c1",
                       "content": '{"echo": "one"}'}         # 观察展开为 role=tool
    assert wire[4]["tool_calls"][0]["function"]["arguments"] == "{}"

    wt, name_map = _wire_tools([{"name": "test.echo", "description": "回显",
                                 "params": {"text": {"type": "str"}}, "required": ["text"]}])
    assert wt[0]["type"] == "function"
    # 内部名 test.echo 含 '.'，净化后在 wire 上为 test_echo（符合 ^[a-zA-Z0-9_-]+$）
    assert wt[0]["function"]["name"] == "test_echo"
    assert wt[0]["function"]["parameters"] == {
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
    # 映射表保留 净化名 → 内部原名，供回程还原
    assert name_map["test_echo"] == "test.echo"

    # 不传映射表时原样回退（兼容 fixture 路径）
    assert _wire_call({"id": "x", "function": {"name": "f", "arguments": '{"a": 1}'}}) == \
        {"id": "x", "name": "f", "arguments": {"a": 1}}
    # 传映射表时把模型回传的净化名还原成内部原名
    mapped = _wire_call({"id": "x", "function": {"name": "test_echo", "arguments": '{"a": 1}'}},
                        name_map)
    assert mapped == {"id": "x", "name": "test.echo", "arguments": {"a": 1}}


# ---------------------------------------------------------------------------
# 用例 11：可管理性与卸载无残留
# ---------------------------------------------------------------------------


def test_manageable_directory_plugin_and_no_residue(tmp_path):
    """用例 11：目录插件（可启停卸，layer 只是标签）；无自有表 ⇒ 卸载零残留。"""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                           state_file=str(Path(d) / "s.json"))
        cands = pm.discovered()
        react = [x for x in cands if x["name"] == "plugins.react"]
        assert react, "plugins.react 未出现在插件发现列表"
        assert react[0]["layer"] == "system"       # 系统组件（语义标签，不闸管理）
        assert react[0]["provides"] == ["react"]
        assert react[0]["tables"] == []            # 无自有表 → 卸载零残留

    c = _build(tmp_path)

    async def s():
        await c.assemble()
        assert c.root.has(Services.REACT)
        await c.teardown()
        assert not c.root.has(Services.REACT)             # 服务回收
        await c.teardown()                         # 幂等

    _run(s())


# ---------------------------------------------------------------------------
# 可观测：status() 钩子（插件页状态列）
# ---------------------------------------------------------------------------


def test_status_hook_reports_last_run(tmp_path):
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        st = {x["id"]: x["status"] for x in await c.plugin_statuses()}
        assert st["plugins.react"]["runs"] == 0                    # 未跑过 → 如实报 0
        await c.root.react.run(c.root, "请直接回答")
        st = {x["id"]: x["status"] for x in await c.plugin_statuses()}
        assert st["plugins.react"]["steps"] == 1
        assert st["plugins.react"]["exhausted"] is False
        await c.teardown()

    _run(s())
