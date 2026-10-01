"""S3 循环内上下文治理测试（``plugins.react`` × ``plugins.context``）——

覆盖验收清单：循环内不写 ``agent_messages`` · 折叠/丢弃职责不重叠 · prior 不重裁 ·
pinned 先占与降级 · 事件字段 · 超预算闸只压一次 · chain 保底 · 无治理器降级不劣化。

全部离线确定性（fixture provider），不触网。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.api import Events, Services
from agent.plugins.react.loop import ReactConfig, ReactLoop
from agent.profiles import build_agent
from agent.sys.chat import ChatDriver
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path, *, react: bool = True, context: bool = True) -> PluginManager:
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    pm.set_enabled("plugins.react", react)
    pm.set_enabled("plugins.context", context)
    pm.set_enabled("plugins.short_term", True)
    return pm


def _build(tmp_path, *, react: bool = True, context: bool = True, patch: dict | None = None):
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch, plugin_mgr=_pm(tmp_path, react=react, context=context))


async def _echo(text: str = "hi"):
    return {"echo": text}


def _register_echo(ctx) -> None:
    ctx.tasks.register(ctx, "test.echo",
                       schema={"text": {"type": "str", "required": False, "default": "hi"}},
                       handler=_echo, owner="test", description="回显参数")


def _capture_governed(ctx, stage: str = "react") -> list[dict]:
    """订阅 ``agent/context/governed``（**live 事件，非 durable**）并收集指定 stage 的载荷。

    用**同步**监听器——``emit`` 对 async 监听是 fire-and-forget，断言前可能尚未执行完。
    """
    got: list[dict] = []

    def _h(payload):
        if isinstance(payload, dict) and payload.get("stage") == stage:
            got.append(payload)

    ctx.get(Services.EVENTS).on(ctx, Events.CONTEXT_GOVERNED, _h)
    return got


# ---------------------------------------------------------------------------
# 块内准备：只折叠、不丢弃（设计 §3.6.1）
# ---------------------------------------------------------------------------


class _FakeGov:
    """最小治理器替身：只提供 ``clip_head_tail``（记录调用）。"""

    def __init__(self, threshold: int = 10):
        self.calls: list[str] = []
        self._threshold = threshold

    def clip_head_tail(self, text, **_kw):
        self.calls.append(text)
        if len(text) <= self._threshold:
            return text, False
        return text[:4] + "...[省略]..." + text[-4:], True


def _chain_of(n_observe: int, *, pinned_idx: tuple[int, ...] = ()) -> list[dict]:
    chain = [{"role": "assistant", "content": "想一下", "type": "think", "id": "t0"}]
    for i in range(n_observe):
        chain.append({"role": "user", "content": {"tool_results": [
            {"tool_call_id": f"c{i}", "name": "test.echo", "content": "L" * 100,
             "is_error": False, "denied": False}]},
            "type": "observe", "id": f"o{i}", "pinned": i in pinned_idx})
    return chain


def test_prepare_chain_folds_only_old_unpinned_observes():
    """**只折叠**老观察（``type=="observe"`` 且未 pinned）；think/act 不动；**不丢条目**。

    D2 后：无工作记忆（wm=None）时 act 折叠自动关闭——本例无 wm，act/think 均不动。
    """
    loop = ReactLoop(None, None, None, None, ReactConfig())          # type: ignore[arg-type]
    gov = _FakeGov(threshold=10)
    chain = _chain_of(5, pinned_idx=(0,))
    n_before = len(chain)
    out, newly, act_folds = loop._prepare_chain(gov, chain, keep_recent=2)

    assert len(out) == n_before                                     # 绝不丢条目
    assert newly == 2                                               # o1、o2 被折叠（o0 已 pinned）
    assert act_folds == 0                                           # 无 wm → act 折叠关闭
    assert gov.calls == [] or "L" * 100 in "".join(gov.calls)        # 折叠的是观察正文
    folded = [m for m in out if m.get("id") in ("o1", "o2")]
    assert all("[省略]" in m["content"]["tool_results"][0]["content"] for m in folded)
    assert out[0]["content"] == "想一下"                             # think 原样
    assert out[-1]["id"] == "o4" and "[省略]" not in \
        out[-1]["content"]["tool_results"][0]["content"]            # 最近 keep_recent 保留


def test_fold_content_preserves_tool_result_structure():
    """折叠**保结构**：不解体 ``{tool_results: [...]}``（否则 role=tool 配对会被破坏）。"""
    content = {"tool_results": [{"tool_call_id": "c1", "name": "test.echo",
                                 "content": "Z" * 200, "is_error": True, "denied": False}]}
    out, changed = ReactLoop._fold_content(content, _FakeGov(threshold=10).clip_head_tail)
    assert changed and isinstance(out, dict)                        # 仍是 dict，不是字符串
    tr = out["tool_results"][0]
    assert tr["tool_call_id"] == "c1" and tr["is_error"] is True    # 元数据完好
    assert "[省略]" in tr["content"]
    assert ReactLoop._fold_content("短", _FakeGov(threshold=10).clip_head_tail)[1] is False


# ---------------------------------------------------------------------------
# 循环内不污染会话档案（设计 §1.1 A1 / 验收项 2）
# ---------------------------------------------------------------------------


def test_loop_process_never_enters_agent_messages(tmp_path):
    """循环内 Think/Act/Observe 是草稿：该轮 ``agent_messages`` **只有** 1 user + 1 assistant，
    且**不含**任何工具过程（``tool_results``）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        reply = await c.root.chat.send(c.root, "两步链路：先回显再总结", session_id="s3")
        assert reply.ok and reply.plan["intent"] == "react"
        assert reply.evidence["steps"] >= 2                        # 确实多步
        rows = await c.root.db.query(
            c.root, "SELECT role, content FROM agent_messages WHERE session_id='s3' ORDER BY seq")
        assert [r["role"] for r in rows] == ["user", "assistant"]
        joined = " ".join(str(r["content"]) for r in rows)
        assert "tool_results" not in joined                        # 过程不落档案
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 治理器接线与降级（设计 §3.5 / 验收项 7）
# ---------------------------------------------------------------------------


def test_window_governed_blocks_and_event_fields(tmp_path):
    """接了治理器：块列表为 system/prior/prompt/chain；事件 ``stage="react"`` 且字段定死。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        got = _capture_governed(c.root)
        r = await c.root.react.run(c.root, "两步链路：先回显再总结",
                                   prior=[{"role": "user", "content": "前情"}])
        assert r.ok and r.steps == 2
        assert got, "未发出 stage=react 的 agent/context/governed 事件"
        p = got[-1]
        assert {b["key"] for b in p["blocks"]} >= {
            "react_system", "react_prior", "react_prompt", "react_chain"}
        react = p["react"]
        assert {"chain_tokens", "chain_peak_tokens", "clips", "pinned",
                "pinned_downgraded", "keep_recent", "budget"} <= set(react)   # §3.6.6 字段定死
        assert react["chain_peak_tokens"] >= react["chain_tokens"]            # 峰值 >= 当前
        assert p["step"] >= 1 and "input_budget" in p and "total_used" in p
        await c.teardown()

    _run(s())


def test_window_degrades_without_governor(tmp_path):
    """无治理器 → 退回旧 ``_trim`` 条数裁剪，行为不变（不发 react 遥测、不崩）。"""
    c = _build(tmp_path, context=False)

    async def s():
        await c.assemble()
        assert not c.root.has(Services.CONTEXT)
        _register_echo(c.root)
        got = _capture_governed(c.root)
        r = await c.root.react.run(c.root, "两步链路：先回显再总结")
        assert r.ok and r.steps == 2                              # 循环照常
        assert got == []                                          # 无治理器 → 无遥测
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# prior 刚性 · chain 保底（设计 §3.6.5 / §3.6.4 配套）
# ---------------------------------------------------------------------------


def test_prior_is_rigid_and_chain_reserve_is_applied(tmp_path):
    """会话级装配为循环预留 chain 保底（有效预算 = 输入预算 - 保底），prior 不被重裁。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        gov = c.root.get(Services.CONTEXT)
        _register_echo(c.root)
        short = c.root.get(Services.SHORT_TERM)
        await short.append(c.root, "p", "user", "之前的问题")
        await short.append(c.root, "p", "assistant", "之前的回答")
        priors = _capture_governed(c.root, stage="prior")
        chain_wins = _capture_governed(c.root, stage="react")
        reply = await c.root.chat.send(c.root, "两步链路：先回显再总结", session_id="p")
        assert reply.ok and reply.plan["intent"] == "react"
        # 会话级装配（chat）：有效预算 = 输入预算 - chain 保底（为循环预留）
        assert priors[-1]["input_budget"] == gov.input_budget() - gov.react_min_chain_tokens()
        # 循环内装配（react）：chain 保底已被消费，但仍需为 native tools 目录预留
        # （S3 彻底版·统一账本：tools 是请求附加项、不在 messages 预算内，先扣其占用）
        from agent.api import estimate_request_cost
        from agent.plugins.react.loop import visible_tools
        expect = gov.input_budget() - estimate_request_cost(
            messages=[], tools=visible_tools(c.root))
        p = chain_wins[-1]
        assert p["input_budget"] == expect
        prior_block = next(b for b in p["blocks"] if b["key"] == "react_prior")
        assert prior_block["reason"] == "ok"                      # 未超预算 → 不被重裁
        assert prior_block["used"] == prior_block["requested"]
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 超预算闸：一次性压缩（设计 §3.6.4）
# ---------------------------------------------------------------------------


def test_headroom_gate_compresses_once_when_prior_over_budget(tmp_path, monkeypatch):
    """``prior > input_budget`` → 调度层**只压一次**（不重试、不拒绝进入）。

    说明（实现期核对）：G1 装配后 prior 已被钳在 ``input_budget - 保底`` 内，
    常态下**不会**超预算——本闸是**降级路径的兜底**（如装配失败退回无界拼接）。
    故此处令治理器装配失败以构造该场景。
    """
    calls: list[str] = []

    async def _spy(self, ctx, session_id):
        calls.append(session_id)

    monkeypatch.setattr(ChatDriver, "_safe_compress", _spy)
    c = _build(tmp_path, patch={"agent": {"context": {"max_context_tokens": 1024,
                                                      "reserve_output_tokens": 0}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        gov = c.root.get(Services.CONTEXT)

        def _boom(blocks, **_kw):                       # 装配失败 → 退回无界拼接
            raise RuntimeError("assemble down")

        # G2：``Services.CONTEXT`` 是会话感知门面 ⇒ 要 patch 的是**当前会话实例**的类
        # （``current()`` 返回容器实例；插件按文件路径加载，模块名与 import 路径不同，
        # 故只能从实例取类，不能 import 具体类）。
        monkeypatch.setattr(type(gov.current()), "assemble", _boom)
        filler = "很长的历史内容。" * 400                # 远超 1024 token
        short = c.root.get(Services.SHORT_TERM)
        await short.append(c.root, "h", "user", filler)
        await short.append(c.root, "h", "assistant", filler)
        reply = await c.root.chat.send(c.root, "两步链路：先回显再总结", session_id="h")
        assert calls == ["h"]                            # 恰好一次，且未反复触发
        assert reply.plan["intent"] == "react"           # 仍进入循环（不拒绝）
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 端到端：10+ 步工具链不溢出（验收项 1）
# ---------------------------------------------------------------------------


def test_ten_plus_step_chain_stays_within_budget(tmp_path):
    """12 步大观察工具链：循环内每步装配 **total_used <= input_budget**，chain 不超额度。

    这是 S3 的原始动机——旧 ``_trim`` 只按**条数**裁剪，一条大观察即可撑爆窗口。
    """
    c = _build(tmp_path, patch={"agent": {
        "react": {"max_steps": 12},
        "context": {"max_context_tokens": 4096, "reserve_output_tokens": 0}}})

    async def s():
        await c.assemble()

        async def _big():
            return "D" * 6000                                  # 每步 ≈1500 token

        c.root.tasks.register(c.root, "test.big", schema={}, handler=_big,
                              owner="test", description="返回大结果")

        class _AlwaysTool:
            async def reason(self, ctx, messages, tools):
                from agent.plugins.react.components import ReasonOutput, ToolCall
                return ReasonOutput(text="继续取数", tool_calls=[ToolCall("test.big", {}, "c1")],
                                    tokens_in=1, tokens_out=1)

        loop = c.root.react
        loop._reasoner = _AlwaysTool()                          # 恒定请求工具 → 必然多步
        got = _capture_governed(c.root)
        r = await loop.run(c.root, "请反复取数直到触顶")
        assert r.exhausted and r.steps == 12                    # 确实跑到步数闸
        assert len(got) >= 10, f"步数不足（治理遥测 {len(got)} 条）"
        for p in got:
            assert p["total_used"] <= p["input_budget"]         # 每步都不溢出
            rt = p["react"]
            assert rt["chain_tokens"] <= max(1, rt["budget"])   # chain 实占不超其额度
        assert got[-1]["react"]["chain_peak_tokens"] <= got[-1]["input_budget"]
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 依赖边守卫：react --软--> context（单向；不新增硬耦合）
# ---------------------------------------------------------------------------


def test_react_context_edge_is_soft_and_one_way():
    """S3 新增依赖边必须**软**且**单向**：react 只经 ``ctx.get(Services.CONTEXT)`` 感知，
    不 import 其实现、不写进 ``inject``；治理器**不反向**依赖 react。"""
    react_src = (PLUGIN_DIR / "react" / "loop.py").read_text(encoding="utf-8")
    assert "Services.CONTEXT" in react_src                    # 只经接缝感知
    assert "agent.plugins.context" not in react_src           # 不 import 实现
    pkg_src = (PLUGIN_DIR / "react" / "__init__.py").read_text(encoding="utf-8")
    assert "Services.CONTEXT" not in pkg_src.split("def apply")[0]   # 不进 inject（软依赖）

    ctx_src = (PLUGIN_DIR / "context" / "governor.py").read_text(encoding="utf-8")
    assert "plugins.react" not in ctx_src                     # 治理器不反向依赖


# ---------------------------------------------------------------------------
# 依赖边守卫：react --软--> context（单向；不新增硬耦合）
# ---------------------------------------------------------------------------


def test_react_context_edge_is_soft_and_one_way():
    """S3 新增依赖边必须**软**且**单向**：react 只经 ``ctx.get(Services.CONTEXT)`` 感知，
    不 import 其实现、不写进 ``inject``；治理器**不反向**依赖 react。"""
    react_src = (PLUGIN_DIR / "react" / "loop.py").read_text(encoding="utf-8")
    assert "Services.CONTEXT" in react_src                    # 只经接缝感知
    assert "agent.plugins.context" not in react_src           # 不 import 实现
    pkg_src = (PLUGIN_DIR / "react" / "__init__.py").read_text(encoding="utf-8")
    assert "Services.CONTEXT" not in pkg_src.split("def apply")[0]   # 不进 inject（软依赖）

    ctx_src = (PLUGIN_DIR / "context" / "governor.py").read_text(encoding="utf-8")
    assert "plugins.react" not in ctx_src                     # 治理器不反向依赖
