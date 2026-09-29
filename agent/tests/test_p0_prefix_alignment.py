"""G2.0（P0：只读观测 + 前缀对齐）测试 —— 记账面 / 声明方向 / F-1 续接对齐。

覆盖（对应《上下文管理器架构评估与重构方案》§6 P0 测试点）：
- 记账面：**附件登记不改变装配裁决**（逐字节等价）· ``unaccounted`` 随登记下降 ·
  稳定前缀计数（首个 volatile 块之前）；
- 声明方向：``missing_blocks`` 只报「连提交都没有」的必需块（零误报）· 声明幂等；
- 前缀对齐（F-1）：续接**复用头部快照** + 本次消息追加链尾 ⇒ 上一轮请求成为本轮请求的
  **真前缀**；无快照 / 开关关闭 → 如实降级并落 ``agent/context/prefix-unstable`` 体检事件；
- 遥测：``agent/context/governed`` 载荷携带 P0 新字段。

全部离线确定性（fixture provider），不触网。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from agent.api import Events, Services
from agent.core.request_cost import wire_messages
from agent.plugins.context import ContextGovernor
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _build(tmp_path, *, patch: dict | None = None):
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    pm.set_enabled("plugins.react", True)
    pm.set_enabled("plugins.context", True)
    pm.set_enabled("plugins.work_memory", True)
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch, plugin_mgr=pm)


def _register_echo(ctx) -> None:
    async def _echo(text: str = "hi"):
        return {"echo": text}

    ctx.tasks.register(ctx, "test.echo",
                       schema={"text": {"type": "str", "required": False, "default": "hi"}},
                       handler=_echo, owner="test", description="回显参数")


class _AlwaysToolReasoner:
    """恒定请求一次工具调用（强制多步），并记录**每次窗口的入参**。

    只用它替代 ``loop._reasoner``（四组件接缝之一），不改动其余编排。
    """

    def __init__(self, tool: str = "test.echo"):
        self._tool = tool
        self.seen: list[list[dict]] = []
        self._n = 0

    async def reason(self, ctx, messages, tools):
        from agent.plugins.react.components import ReasonOutput, ToolCall

        self.seen.append([dict(m) for m in messages])
        self._n += 1
        return ReasonOutput(text=f"第{self._n}步", reasoning_content=f"思考{self._n}",
                            tool_calls=[ToolCall(self._tool, {}, f"c{self._n}")],
                            tokens_in=1, tokens_out=1)


def _gov() -> ContextGovernor:
    return ContextGovernor(max_context_tokens=10000, reserve_output_tokens=1000)


def _blocks() -> list[dict]:
    """四块最小窗口：system / prior / prompt(volatile) / chain。"""
    return [
        {"key": "sys", "kind": "text", "present": True, "payload": "S" * 40,
         "rigid": True, "priority": -10},
        {"key": "prior", "kind": "messages", "present": True,
         "payload": [{"role": "user", "content": "P" * 40}],
         "rigid": True, "priority": 0, "coverage": (0, 1)},
        {"key": "prompt", "kind": "messages", "present": True,
         "payload": [{"role": "user", "content": "c" * 40}],
         "rigid": True, "priority": 5, "coverage": (1, 2), "volatile": True},
        {"key": "chain", "kind": "messages", "present": True,
         "payload": [{"role": "user", "content": "x" * 400}],
         "weight": 1.0, "priority": 10, "coverage": (2, 3)},
    ]


# ---------------------------------------------------------------------------
# 记账面（P0-1）：附件登记 = 只记账、不改裁决
# ---------------------------------------------------------------------------

def test_attachment_registration_does_not_change_assembly():
    """附件登记**不得改变任何装配裁决**：system/prior/预算/各块实占逐项等价。"""
    g = _gov()
    plain = g.assemble(_blocks(), reserve_tokens=500)
    reg = g.assemble(_blocks(), reserve_tokens=500,
                     attachments=[{"key": "native_tools", "claimed_tokens": 500}])
    assert reg.system_text == plain.system_text
    assert reg.prior == plain.prior
    assert reg.report.input_budget == plain.report.input_budget
    assert [(b.key, b.used, b.allocated, b.reason) for b in reg.report.blocks] == \
           [(b.key, b.used, b.allocated, b.reason) for b in plain.report.blocks]


def test_attachment_registration_reduces_unaccounted():
    """登记的会计意义：unaccounted 下降量 == 该附件此前的隐形占用（黑洞显影）。"""
    g = _gov()
    before = g.assemble(_blocks(), reserve_tokens=500).report.unaccounted_tokens
    after = g.assemble(_blocks(), reserve_tokens=500,
                       attachments=[{"key": "native_tools",
                                     "claimed_tokens": 500}]).report.unaccounted_tokens
    assert before - after == 500
    rep = g.assemble(_blocks(), reserve_tokens=500,
                     attachments=[{"key": "native_tools", "claimed_tokens": 500}]).report
    assert [(a.key, a.claimed, a.used) for a in rep.attachments] == [("native_tools", 500, 500)]


def test_attachments_alone_reserve_the_budget():
    """只传 attachments（不传 reserve）时也能占位——有效预留取二者较大值，不重复扣减。"""
    g = _gov()
    only = g.assemble(_blocks(), attachments=[{"key": "native_tools", "claimed_tokens": 500}])
    assert only.report.input_budget == g.assemble(_blocks(), reserve_tokens=500).report.input_budget
    # 二者都传且相等时不得叠加扣减（否则预算被吃两次）
    both = g.assemble(_blocks(), reserve_tokens=500,
                      attachments=[{"key": "native_tools", "claimed_tokens": 500}])
    assert both.report.input_budget == only.report.input_budget


def test_stable_prefix_tokens_counts_before_first_volatile():
    """稳定前缀 = 首个 volatile 块之前的装配量（缓存友好度体检指标）。"""
    rep = _gov().assemble(_blocks()).report
    assert rep.volatile_blocks == ["prompt"]
    used = {b.key: b.used for b in rep.blocks}
    assert rep.stable_prefix_tokens == used["sys"] + used["prior"]


def test_stable_prefix_is_whole_assembly_when_nothing_volatile():
    """无 volatile 标记 → 全部计为稳定前缀（不虚报风险）。"""
    blocks = [b for b in _blocks() if b["key"] != "prompt"]
    rep = _gov().assemble(blocks).report
    assert rep.volatile_blocks == []
    assert rep.stable_prefix_tokens == sum(b.used for b in rep.blocks)


# ---------------------------------------------------------------------------
# 声明方向（P0-3）：完整性校验只报「连提交都没有」
# ---------------------------------------------------------------------------

def test_missing_blocks_reports_declared_but_absent():
    g = _gov()
    g.declare([{"key": "sys"}, {"key": "prompt"}, {"key": "ghost"}], phase="loop")
    assert g.assemble(_blocks(), phase="loop").report.missing_blocks == ["ghost"]


def test_missing_blocks_ignores_present_false_and_other_phases():
    """``present=False`` 仍算已提交（不算漏报）；未声明的 (scope, phase) 恒空表（零误报）。"""
    g = _gov()
    g.declare([{"key": "sys"}, {"key": "prompt"}, {"key": "chain"},
               {"key": "prior", "required": False}], phase="loop")
    empty_prompt = [dict(b, present=(b["key"] != "prompt")) for b in _blocks()]
    assert g.assemble(empty_prompt, phase="loop").report.missing_blocks == []
    assert g.assemble(_blocks(), phase="plan").report.missing_blocks == []
    # required=False 的块缺席也不得报红灯
    no_prior = [b for b in _blocks() if b["key"] != "prior"]
    assert g.assemble(no_prior, phase="loop").report.missing_blocks == []


def test_declare_is_idempotent_last_wins():
    g = _gov()
    g.declare([{"key": "ghost"}], phase="loop")
    g.declare([{"key": "ghost", "required": False}], phase="loop")
    assert g.assemble(_blocks(), phase="loop").report.missing_blocks == []
    g.declare([{"key": "ghost"}], phase="loop")           # 再次声明 → 最后者为准
    assert g.assemble(_blocks(), phase="loop").report.missing_blocks == ["ghost"]


def test_declare_accepts_contract_type():
    """契约类型与 dict 入参等价（消费层可零 import）。"""
    from agent.plugins.context.protocols import BlockDecl

    g = _gov()
    g.declare([BlockDecl(key="ghost")], phase="loop")
    assert g.assemble(_blocks(), phase="loop").report.missing_blocks == ["ghost"]


# ---------------------------------------------------------------------------
# 前缀对齐（P0-2 / F-1）：续接复用头部快照 + 本次消息入链尾
# ---------------------------------------------------------------------------

def test_resume_reuses_frozen_head_and_previous_request_is_a_prefix(tmp_path):
    """F-1 核心断言：上一轮请求必须是本轮续接请求的**真前缀**（否则整链 miss）。"""
    c = _build(tmp_path, patch={"agent": {"react": {"max_steps": 2}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        loop = c.root.react
        spy = _AlwaysToolReasoner()
        loop._reasoner = spy

        await loop.run(c.root, "写一部三万字小说", session_id="s1")   # 第一轮（两步闸）
        assert len(spy.seen) == 2, "两步闸应产生两次窗口"
        first_run_last = spy.seen[-1]
        n1 = len(spy.seen)

        await loop.run(c.root, "继续", session_id="s1", resume=True)  # 续接
        assert len(spy.seen) > n1, "续接必须产生新窗口"
        resumed_first = spy.seen[n1]

        # ① 头部 prompt 取**快照里的任务原文**，而不是本轮的「继续」
        users = [m for m in resumed_first if m.get("role") == "user"]
        assert users[0]["content"] == "写一部三万字小说"
        # ② 本次消息追加在**链尾**（窗口最后一条）
        assert resumed_first[-1]["content"] == "继续"
        # ③ 上轮链进入本轮窗口
        assert any(m.get("id") == "step1_act" for m in resumed_first)
        # ④ **真前缀**：wire 形态下，上一轮请求逐条等于本轮请求的前 len(w1) 条
        w1 = wire_messages(first_run_last)
        w2 = wire_messages(resumed_first)
        assert w2[:len(w1)] == w1, "续接请求必须以上一轮请求为前缀（前缀缓存命中的前提）"
        await c.teardown()

    _run(s())


def test_non_resume_run_uses_current_prompt(tmp_path):
    """对照组：非续接（新任务）用本轮 prompt 重建头部，不背旧快照。"""
    c = _build(tmp_path, patch={"agent": {"react": {"max_steps": 1}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        loop = c.root.react
        spy = _AlwaysToolReasoner()
        loop._reasoner = spy

        await loop.run(c.root, "第一件事", session_id="s2")
        n1 = len(spy.seen)
        await loop.run(c.root, "第二件事", session_id="s2", resume=False)
        fresh = spy.seen[n1]
        users = [m for m in fresh if m.get("role") == "user"]
        assert users[0]["content"] == "第二件事"
        assert not any(m.get("id") == "step1_act" for m in fresh), "非续接不得携带上轮链"
        await c.teardown()

    _run(s())


def test_resume_without_snapshot_degrades_and_flags(tmp_path):
    """旧存档（无头部快照）：如实降级回现行为 + 落 prefix-unstable 体检事件，不伪造。"""
    c = _build(tmp_path, patch={"agent": {"react": {"max_steps": 1}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        wm = c.root.get(Services.WORK_MEMORY)
        # 手工构造「只有链、没有头快照」的旧版存档
        await wm.begin("react_chain:old", {"session_id": "old"})
        await wm.checkpoint("react_chain:old", "chain", {
            "chain": [{"role": "assistant", "content": "上轮结论", "type": "think",
                       "id": "step1_think"}],
            "steps": 1, "at": "t"})

        loop = c.root.react
        spy = _AlwaysToolReasoner()
        loop._reasoner = spy
        await loop.run(c.root, "继续", session_id="old", resume=True)

        win = spy.seen[0]
        users = [m for m in win if m.get("role") == "user"]
        assert users[0]["content"] == "继续", "无快照 → 退回现行为（本轮消息作 prompt）"
        assert any(m.get("id") == "step1_think" for m in win), "旧链仍须进入窗口（不劣化）"
        rows = await c.root.db.query(
            c.root,
            "SELECT payload FROM agent_events WHERE event='agent/context/prefix-unstable'")
        assert rows, "无快照续接必须落前缀体检红灯"
        payload = json.loads(rows[0]["payload"])
        assert payload["reason"] == "no_snapshot"
        await c.teardown()

    _run(s())


def test_alignment_disabled_falls_back_and_flags(tmp_path):
    """开关关闭：不读快照（一键回退旧行为），并如实落 alignment_disabled 体检事件。"""
    c = _build(tmp_path, patch={"agent": {"react": {"max_steps": 1,
                                                    "alignment_enabled": False}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        loop = c.root.react
        spy = _AlwaysToolReasoner()
        loop._reasoner = spy

        await loop.run(c.root, "写一部三万字小说", session_id="s3")
        n1 = len(spy.seen)
        await loop.run(c.root, "继续", session_id="s3", resume=True)
        users = [m for m in spy.seen[n1] if m.get("role") == "user"]
        assert users[0]["content"] == "继续", "开关关闭 → 头部按本轮重建（旧行为）"
        rows = await c.root.db.query(
            c.root,
            "SELECT payload FROM agent_events WHERE event='agent/context/prefix-unstable'")
        assert rows and json.loads(rows[0]["payload"])["reason"] == "alignment_disabled"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 遥测（P0-1 / P0-3 / P0-4）：装配报告新字段进入 agent/context/governed
# ---------------------------------------------------------------------------

def test_window_telemetry_carries_p0_fields(tmp_path):
    c = _build(tmp_path, patch={"agent": {"react": {"max_steps": 1}}})
    got: list[dict] = []

    async def s():
        await c.assemble()
        _register_echo(c.root)

        def _h(payload):
            if isinstance(payload, dict) and payload.get("stage") == "react":
                got.append(payload)

        c.root.get(Services.EVENTS).on(c.root, Events.CONTEXT_GOVERNED, _h)
        c.root.react._reasoner = _AlwaysToolReasoner()
        await c.root.react.run(c.root, "跑一步", session_id="s4")
        assert got, "必须发出 agent/context/governed 遥测"
        p = got[-1]
        for k in ("unaccounted_tokens", "stable_prefix_tokens", "volatile_blocks",
                  "missing_blocks", "attachments"):
            assert k in p, f"遥测缺 P0 字段 {k}"
        assert p["volatile_blocks"] == ["react_prompt"]
        assert p["missing_blocks"] == [], "react 端点四块齐备，不得误报漏块"
        assert any(a["key"] == "native_tools" for a in p["attachments"]), \
            "native tools 必须显式登记（此前是记账黑洞）"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 思考 token 与正文 token 分离（P0-5）
# ---------------------------------------------------------------------------

def test_reasoning_tokens_helper_reads_details_honestly():
    """helper 只认明细字段；取不到就返回 None（不估、不猜）。"""
    from agent.sys.llm import _reasoning_tokens

    assert _reasoning_tokens({"completion_tokens_details": {"reasoning_tokens": 42}}) == 42
    assert _reasoning_tokens({"completion_tokens": 10}) is None          # 无明细
    assert _reasoning_tokens({"completion_tokens_details": {}}) is None  # 明细里无该键
    assert _reasoning_tokens({"completion_tokens_details": {"reasoning_tokens": "x"}}) is None


def test_reasoning_tokens_recorded_in_budget_log(tmp_path):
    """provider 给出明细时，思考 token 独立落账（供 X-1 回答「输出里推理占多少」）。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        svc = c.root.get(Services.LLM)
        orig = svc._provider

        class _Stub:
            async def complete(self, ctx, messages, **kw):
                return {"text": "答", "usage": {
                    "prompt_tokens": 10, "completion_tokens": 20,
                    "completion_tokens_details": {"reasoning_tokens": 15}}}

        svc._provider = _Stub()
        try:
            r = await svc.complete(c.root, [{"role": "user", "content": "问"}], caller="t")
            assert r.reasoning_tokens == 15
            await asyncio.sleep(0)      # 入账监听器为 fire-and-forget，先让它跑完
            rows = await c.root.db.query(
                c.root,
                "SELECT reasoning_tokens FROM agent_budget_log ORDER BY rowid DESC LIMIT 1")
            assert int(rows[0]["reasoning_tokens"]) == 15
        finally:
            svc._provider = orig
            await c.teardown()

    _run(s())
