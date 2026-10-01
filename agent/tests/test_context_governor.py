"""上下文治理插件测试（agent/plugins/context，G0 骨架）——
token 口径 / 裁剪原语 / 预算模型 / 历史收窗 / 目录插件语义（装配、停用降级、接缝注入）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agent.api import ConfigError, Services
from agent.core.tokens import clip_json, clip_text, estimate_tokens
from agent.plugins.context import ContextGovernor
from agent.profiles import build_agent
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path, *, enable: bool = True) -> PluginManager:
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "plugin_state.json"))
    pm.set_enabled("plugins.context", enable)
    return pm


def _build(tmp_path, *, enable: bool = True, patch: dict | None = None):
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch, plugin_mgr=_pm(tmp_path, enable=enable))


# ---- core.tokens 原语 ----


def test_estimate_tokens_cjk_ascii_mixed():
    """CJK 0.75 token/字、ASCII 4 字符/token（json.dumps 含引号包装，计入开销）。"""
    assert estimate_tokens("abcd") == 1                     # '"abcd"' 6 字符 → 1 token
    assert estimate_tokens("你好") == 2                     # '"你好"' 4 字符 + 2 CJK×0.75
    assert estimate_tokens("你是谁") > estimate_tokens("hi")  # 中文不再被 len//4 低估
    assert estimate_tokens("上下文治理" * 50) > 100          # 长中文按 0.75 token/字增长


def test_clip_text_under_limit_and_head_tail():
    """未超限原样返回；超限保头尾 + 中部省略标注（省略 token 数如实）。"""
    text = "a" * 40                       # ≈10 tokens
    out, omitted = clip_text(text, 100)
    assert out == text and omitted == 0

    big = "甲" * 400 + "乙" * 400          # CJK ≈ 600 tokens
    out, omitted = clip_text(big, 100, head_ratio=0.5)
    assert 0 < omitted < estimate_tokens(big)
    assert out.startswith("甲") and out.endswith("乙")
    assert "[...省略" in out and "tokens...]" in out
    # 裁剪后体积显著下降（标注本身有少量开销）
    assert estimate_tokens(out) < estimate_tokens(big)


def test_clip_text_zero_budget_clears():
    out, omitted = clip_text("一些内容", 0)
    assert out == "" and omitted > 0


def test_clip_json_shape():
    obj = {"k": "v" * 100}
    out, omitted = clip_json(obj, 1000)
    assert '"k"' in out and omitted == 0
    out2, omitted2 = clip_json({"k": "字" * 500}, 20)
    assert omitted2 > 0 and "[...省略" in out2


# ---- 预算模型 ----


def test_budget_model_and_validation():
    g = ContextGovernor(max_context_tokens=10000, reserve_output_tokens=1000,
                        memory_ratio=0.2, history_ratio=0.5)
    assert g.input_budget() == 9000
    assert g.memory_budget() == 1800
    assert g.history_budget() == 4500
    assert g.source_clip_budget() == 8192      # 默认口径
    for bad in ({"max_context_tokens": 0},
                {"max_context_tokens": 100, "reserve_output_tokens": 100},
                {"memory_ratio": 1.5}):
        try:
            ContextGovernor(**bad)
            raise AssertionError(f"应拒绝非法配置: {bad}")
        except ValueError:
            pass


def test_from_config_overrides():
    g = ContextGovernor.from_config({"max_context_tokens": 8000, "history_ratio": 0.4})
    assert g.max_context_tokens == 8000
    assert g.history_ratio == 0.4
    assert g.reserve_output_tokens == 4000     # 缺省键落默认值


# ---- 历史收窗 ----


def _msg(role: str, content: str) -> dict:
    return {"role": role, "content": content}


def test_pick_history_greedy_by_token_not_count():
    """预算内从新到旧贪心收取；顺序保持原序；丢弃数如实。"""
    g = ContextGovernor(max_context_tokens=24000, history_ratio=0.6)
    msgs = [_msg("user", "x" * 40) for _ in range(10)]   # 每条 ≈11 tokens（JSON 包装后）
    kept, dropped = g.pick_history(msgs, budget=40)
    assert kept == msgs[len(msgs) - len(kept):]           # 连续尾部（等长消息）
    assert dropped == 10 - len(kept) and len(kept) >= 1


def test_pick_history_keeps_newest_rigidly():
    """最新一条刚性保留：即便它单独超预算。"""
    g = ContextGovernor()
    big = _msg("user", "长" * 500)
    kept, dropped = g.pick_history([big], budget=10)
    assert kept == [big] and dropped == 0


def test_pick_history_keep_first_user():
    """keep_first：首条 user 刚性保留（多步循环首问语义），与 react _trim 对齐。"""
    g = ContextGovernor()
    msgs = [_msg("user", "首问")] + [_msg("assistant", "x" * 200) for _ in range(20)]
    kept, dropped = g.pick_history(msgs, budget=30, keep_first=True)
    assert kept[0] == msgs[0] and kept[-1] == msgs[-1]
    assert len(kept) == 2                       # 首问 + 最新一条（其余全超预算）
    assert dropped == len(msgs) - len(kept)


def test_pick_history_empty():
    assert ContextGovernor().pick_history([]) == ([], 0)


# ---- 目录插件语义 ----


def test_plugin_discovered_and_provides_context():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                           state_file=str(Path(d) / "state.json"))
        cand = [c for c in pm.discovered() if c["name"] == "plugins.context"]
        assert cand, "plugins.context 未出现在插件发现列表"
        assert cand[0]["layer"] == "system"
        assert cand[0]["provides"] == ["context"]
        assert cand[0]["tables"] == []             # G0 无表；G1 随插件自带迁移


def test_assemble_provides_governor_from_config(tmp_path):
    c = _build(tmp_path, patch={"agent": {"context": {"max_context_tokens": 12345}}})

    async def s():
        await c.assemble()
        assert c.root.has(Services.CONTEXT)
        g = c.root.get(Services.CONTEXT)
        # 插件管理器按文件路径加载（模块名 agent_plugins.*），跨导入路径不做 isinstance，
        # 以类名 + 行为断言。
        # G2（会话级）：``Services.CONTEXT`` 现在是**会话感知门面**（``GovernorRouter``）——
        # 无会话覆盖时恒转发到「按容器配置构造」的那一个实例，故配置贯通与行为断言不变；
        # 类名断言改为门面名（这是本批**有意**的接口变化，见实施方案 §4 路线 B）。
        assert type(g).__name__ == "GovernorRouter"
        assert g.max_context_tokens == 12345       # 配置节贯通
        assert g.history_budget() == int((12345 - 4000) * 0.6)

    _run(s())


def test_disabled_context_plugin_not_assembled(tmp_path):
    """停用 → 不装配 ctx.context（软依赖消费方可据此降级）。"""
    c = _build(tmp_path, enable=False)

    async def s():
        await c.assemble()
        assert not c.root.has(Services.CONTEXT)

    _run(s())


def test_governor_seam_injection(tmp_path):
    """接缝注入：ctx.get("context.governor") 存在即用注入实现（容器按插件名排序，
    假插件名须排在 plugins.context 之前）。"""
    from agent.api import plugin as plugin_dec

    @plugin_dec
    class FakeGovernor:
        name = "aaa.fake_governor"
        provides: list = []
        inject: list = []

        async def apply(self, ctx) -> None:
            class _Stub:
                def input_budget(self) -> int:
                    return 7

                def memory_budget(self) -> int:
                    return 0

                def history_budget(self) -> int:
                    return 0

                def source_clip_budget(self) -> int:
                    return 0

                def pick_history(self, messages, budget=None, *, keep_first=False):
                    return ([], len(messages))

            ctx.provide("context.governor", _Stub())

    pm = _pm(tmp_path)
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")}, plugin_mgr=pm,
                    extra_plugins=(FakeGovernor,))

    async def s():
        await c.assemble()
        g = c.root.get(Services.CONTEXT)
        assert g.input_budget() == 7               # 注入实现生效，非默认 governor

    _run(s())


def test_context_config_node_passes_validation(tmp_path):
    """agent.context 节**具体键类型**校验（G2.0 P1-2：由 "_any" 收紧，与 budget 节同纪律）。

    合法键通过；拼错键（如 ``compact_trigger`` 少了 ``_tokens``）在装配期即 fail-fast——
    这正是「不再静默落默认值」的落点。
    """
    from agent.core.config_schema import validate_config

    c = _build(tmp_path, patch={"agent": {"context": {"max_context_tokens": 20000,
                                                      "working_window_tokens": 96000,
                                                      "window_high_ratio": 0.7}}})

    async def s():
        await c.assemble()                          # 合法 context 节点不抛 ConfigError
        assert c.root.has(Services.CONTEXT)

    _run(s())

    # 未知键（拼错）→ fail-fast
    with pytest.raises(ConfigError):
        validate_config({"agent": {"context": {"compact_trigger": 8}}})
    # 类型不符（int 期望却给 str）→ fail-fast
    with pytest.raises(ConfigError):
        validate_config({"agent": {"context": {"working_window_tokens": "96000"}}})


# ---- G1 分块装配（assemble） ----


def test_assemble_present_filter_and_priority_order():
    """注册制：present=False / 空内容不参与；text 块按 priority 升序 _SEP 连接。"""
    from agent.plugins.context import BlockSpec
    g = ContextGovernor(max_context_tokens=1_000_000, reserve_output_tokens=0)
    asm = g.assemble([
        BlockSpec("a", "text", True, "AAA", rigid=True, priority=20),
        BlockSpec("b", "text", True, "BBB", rigid=True, priority=10),
        BlockSpec("c", "text", False, "CCC", priority=0),      # absent
        BlockSpec("d", "text", True, "   ", priority=5),       # empty → 跳过
    ])
    assert asm.system_text == "BBB\n\nAAA"
    by = {r.key: r for r in asm.report.blocks}
    assert by["c"].reason == "absent" and by["d"].reason == "empty"
    assert by["a"].reason == "ok" and by["b"].reason == "ok"


def test_assemble_report_is_dataclass_not_dict():
    """Assembly.report 为 AssemblyReport dataclass（非裸 dict）。"""
    from agent.plugins.context import Assembly, AssemblyReport, BlockReport, BlockSpec
    g = ContextGovernor()
    asm = g.assemble([BlockSpec("x", "text", True, "hi")])
    assert isinstance(asm, Assembly)
    assert isinstance(asm.report, AssemblyReport)
    assert all(isinstance(r, BlockReport) for r in asm.report.blocks)
    assert asm.report.input_budget == g.input_budget()


def test_assemble_weight_renormalize_when_block_absent():
    """存在块间重归一：memory 缺席 → tools 份额上升（静态比例作废）。"""
    from agent.plugins.context import BlockSpec
    g = ContextGovernor(max_context_tokens=400, reserve_output_tokens=0)
    big_tools = "t" * 4000
    big_hist = [{"role": "user", "content": "h" * 4000, "seq": 1}]

    def alloc(asm, key):
        return next(r.allocated for r in asm.report.blocks if r.key == key)

    with_mem = g.assemble([
        BlockSpec("memory", "text", True, "m" * 4000, weight=0.10),
        BlockSpec("tools", "text", True, big_tools, weight=0.20),
        BlockSpec("history", "messages", True, big_hist, weight=0.60),
    ])
    no_mem = g.assemble([
        BlockSpec("tools", "text", True, big_tools, weight=0.20),
        BlockSpec("history", "messages", True, big_hist, weight=0.60),
    ])
    assert alloc(no_mem, "tools") > alloc(with_mem, "tools")


def test_assemble_redistribute_surplus_to_truncated_block():
    """剩余回吐（water-filling）：小需求块的未用余量回吐给被截断块，redistributed>0。"""
    from agent.plugins.context import BlockSpec
    ib = 1000
    g = ContextGovernor(max_context_tokens=ib, reserve_output_tokens=0)
    asm = g.assemble([
        BlockSpec("memory", "text", True, "x", weight=0.10),          # 需求极小
        BlockSpec("tools", "text", True, "t" * 8000, weight=0.20),    # 需求巨大
    ])
    mem = next(r for r in asm.report.blocks if r.key == "memory")
    tools = next(r for r in asm.report.blocks if r.key == "tools")
    assert mem.used == mem.requested            # 小需求块落定（用满）
    assert tools.allocated > 600                # 回吐抬高（单轮份额 1000×0.2/0.3≈666）
    assert asm.report.redistributed > 0


def test_assemble_rigid_overflow_dual_channel():
    """刚性超限：report.rigid_overflow 非空 + system_text 含显式降级标注（双通道）。"""
    from agent.plugins.context import BlockSpec
    g = ContextGovernor(max_context_tokens=50, reserve_output_tokens=0)
    asm = g.assemble([
        BlockSpec("k1", "text", True, "a" * 100, rigid=True, priority=0),   # ≈26 tok，先满足
        BlockSpec("k2", "text", True, "b" * 400, rigid=True, priority=1),   # ≈101 tok，超限降级
    ])
    assert asm.report.rigid_overflow                              # report 通道
    assert "上下文预算超限" in asm.system_text                      # 文本通道
    by = {r.key: r for r in asm.report.blocks}
    assert by["k1"].reason == "ok"
    assert by["k2"].reason == "downgraded"
    assert by["k2"].used < by["k2"].requested


def test_assemble_coverage_left_open_right_closed():
    """左开右闭：covers 归 summary（(0,covers]），covers+1 起归 history（(covers,N]），无重复。"""
    from agent.plugins.context import BlockSpec
    g = ContextGovernor(max_context_tokens=1_000_000, reserve_output_tokens=0)
    covers = 3
    hist = [{"role": "user", "content": f"m{i}", "seq": i} for i in range(covers + 1, 7)]  # 4..6
    asm = g.assemble([
        BlockSpec("summary", "messages", True,
                  [{"role": "user", "content": "前情", "seq": covers}],  # seq=covers 属 summary
                  weight=0.1, coverage=(0, covers)),
        BlockSpec("history", "messages", True, hist, weight=0.6, coverage=(covers, 6)),
    ])
    contents = [m["content"] for m in asm.prior]
    assert contents == ["前情", "m4", "m5", "m6"]        # summary 在前，history 在后，无重复
    assert all("seq" not in m for m in asm.prior)        # 输出剥离内部 seq


def test_assemble_mutex_drops_overlap():
    """重叠区间：history 误含已覆盖 seq → 治理器剔除，绝不双份注入。"""
    from agent.plugins.context import BlockSpec
    g = ContextGovernor(max_context_tokens=1_000_000, reserve_output_tokens=0)
    asm = g.assemble([
        BlockSpec("summary", "messages", True,
                  [{"role": "user", "content": "前情", "seq": 3}],
                  weight=0.1, coverage=(0, 3)),
        BlockSpec("history", "messages", True,
                  [{"role": "user", "content": "dup3", "seq": 3},     # 重叠（应被剔除）
                   {"role": "user", "content": "ok4", "seq": 4}],
                  weight=0.6, coverage=(3, 4)),
    ])
    contents = [m["content"] for m in asm.prior]
    assert "dup3" not in contents
    assert "前情" in contents and "ok4" in contents


def test_assemble_total_within_budget_when_flexible():
    """可裁块场景：非刚性块总实占不超过 input_budget（回吐不越界）。"""
    from agent.plugins.context import BlockSpec
    ib = 300
    g = ContextGovernor(max_context_tokens=ib, reserve_output_tokens=0)
    asm = g.assemble([
        BlockSpec("tools", "text", True, "t" * 4000, weight=0.2),
        BlockSpec("history", "messages", True,
                  [{"role": "user", "content": "h" * 4000, "seq": 1}], weight=0.6),
    ])
    flex_used = sum(r.used for r in asm.report.blocks
                    if r.reason in ("ok", "trimmed", "downgraded"))
    assert flex_used <= ib


# ---- 降级对拍（Step2：plan 段接入不劣化） ----


def test_fallback_assemble_replicates_plan_concat():
    """fallback 精确保留现状拼接语义（头部 \\n、块间 \\n\\n）。"""
    from agent.sys.chat import _fallback_assemble, MARK_CHAT_PLAN, PLAN_SYSTEM
    head = MARK_CHAT_PLAN + "\n" + "SM" + "\n" + PLAN_SYSTEM
    blocks = [
        {"key": "plan_head", "kind": "text", "present": True, "payload": head,
         "rigid": True, "priority": 0},
        {"key": "memory", "kind": "text", "present": True, "payload": "MEM", "priority": 10},
        {"key": "tools", "kind": "text", "present": True, "payload": "TOOLS", "priority": 20},
        {"key": "env", "kind": "text", "present": True, "payload": "ENV", "priority": 90},
    ]
    assert _fallback_assemble(blocks).system_text == head + "\n\nMEM\n\nTOOLS\n\nENV"


def test_fallback_byte_equal_to_governor_when_budget_ample():
    """预算充足时，governor.assemble（含 dict 入参）与 chat._fallback_assemble 逐字节等价。"""
    from agent.sys.chat import _fallback_assemble, MARK_CHAT_PLAN, PLAN_SYSTEM
    g = ContextGovernor(max_context_tokens=1_000_000, reserve_output_tokens=0)
    head = MARK_CHAT_PLAN + "\n" + "SM" + "\n" + PLAN_SYSTEM
    blocks = [
        {"key": "plan_head", "kind": "text", "present": True, "payload": head,
         "rigid": True, "priority": 0},
        {"key": "memory", "kind": "text", "present": True, "payload": "MEM", "priority": 10},
        {"key": "tools", "kind": "text", "present": True, "payload": "TOOLS", "priority": 20},
        {"key": "env", "kind": "text", "present": True, "payload": "ENV",
         "rigid": True, "priority": 90},
        {"key": "summary", "kind": "messages", "present": True,
         "payload": [{"role": "user", "content": "S", "seq": 1}],
         "weight": 0.1, "coverage": (0, 1)},
        {"key": "history", "kind": "messages", "present": True,
         "payload": [{"role": "user", "content": "H", "seq": 2}],
         "weight": 0.6, "coverage": (1, 2)},
    ]
    asm_gov = g.assemble(blocks)                 # dict 入参（同时覆盖 _coerce）
    asm_fb = _fallback_assemble(blocks)
    assert asm_gov.system_text == asm_fb.system_text
    assert asm_gov.prior == asm_fb.prior


def test_fallback_byte_equal_when_block_absent():
    """块缺失（memory/tools absent）时两者仍等价——空块不占位、不产生多余分隔符。"""
    from agent.sys.chat import _fallback_assemble, MARK_CHAT_PLAN, PLAN_SYSTEM
    g = ContextGovernor(max_context_tokens=1_000_000, reserve_output_tokens=0)
    head = MARK_CHAT_PLAN + "\n" + PLAN_SYSTEM       # 无系统记忆
    blocks = [
        {"key": "plan_head", "kind": "text", "present": True, "payload": head,
         "rigid": True, "priority": 0},
        {"key": "memory", "kind": "text", "present": False, "payload": "", "priority": 10},
        {"key": "tools", "kind": "text", "present": False, "payload": "", "priority": 20},
        {"key": "env", "kind": "text", "present": True, "payload": "ENV",
         "rigid": True, "priority": 90},
    ]
    assert g.assemble(blocks).system_text == head + "\n\nENV"
    assert g.assemble(blocks).system_text == _fallback_assemble(blocks).system_text


def test_governor_receives_covers_as_param_not_reverse_query(tmp_path):
    """架构约束⑤：``covers`` 由 chat 经 ``BlockSpec.coverage`` **入参**传入，
    governor 不反向查询 short_term（避免循环依赖）。"""
    from agent.api import plugin as plugin_dec

    captured: dict = {}

    @plugin_dec
    class SpyGovPlugin:
        name = "aaa.spy_gov"          # 排在 plugins.context 之前 → 接缝注入生效
        provides: list = []
        inject: list = []

        async def apply(self, ctx) -> None:
            class _Spy:
                def input_budget(self) -> int:
                    return 1_000_000

                def memory_budget(self) -> int:
                    return 0

                def history_budget(self) -> int:
                    return 0

                def source_clip_budget(self) -> int:
                    return 0

                def pick_history(self, messages, budget=None, *, keep_first=False):
                    return (list(messages), 0)

                def assemble(self, blocks):
                    import agent.plugins.context as _ctxpkg
                    captured["blocks"] = list(blocks)
                    return _ctxpkg.Assembly(system_text="", prior=[],
                                            report=_ctxpkg.AssemblyReport())

            ctx.provide("context.governor", _Spy())

    pm = _pm(tmp_path)
    pm.set_enabled("plugins.short_term", True)
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")}, plugin_mgr=pm,
                    extra_plugins=(SpyGovPlugin,))

    async def s():
        await c.assemble()
        short = c.root.get(Services.SHORT_TERM)
        for i in range(6):
            await short.append(c.root, "z", "user", f"消息{i}")
        await short._store.upsert_summary(c.root, "z", summary="前情", version=1,
                                          covers_up_to_seq=4, tokens=5)
        await c.root.chat._build_prior(c.root, "z", "x")
        blocks = {b["key"]: b for b in captured["blocks"]}
        assert blocks["summary"]["coverage"] == (0, 4)      # 左开右闭：summary=(0,covers]
        assert blocks["history"]["coverage"] == (4, 5)      # history=(covers,N]，N=末条历史 seq
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# S3 循环内上下文：预算口径 · 折叠原语 · pinned 先占/降级 · 预留额度 · 职责守卫
# ---------------------------------------------------------------------------


def test_react_budget_derives_from_input_budget_and_can_go_negative():
    """S3 校正④：``react_budget = input_budget() - prior_tokens``，**可为负**（不静默当 0）。"""
    g = ContextGovernor(max_context_tokens=1000, reserve_output_tokens=400)
    assert g.input_budget() == 600
    assert g.react_budget(0) == 600
    assert g.react_budget(200) == 400
    assert g.react_budget(600) == 0
    assert g.react_budget(900) == -300          # prior 超预算 → 负值，交调用方显式处置


def test_clip_head_tail_folds_middle_and_keeps_both_ends():
    """S3 折叠原语：超阈值 → 保头尾 + 中段省略标注；实占 <= 头+尾。"""
    g = ContextGovernor(clip_max_tokens=200, clip_head_tokens=100, clip_tail_tokens=100)
    text = "HEAD" + ("M" * 4000) + "TAIL"
    out, clipped = g.clip_head_tail(text)
    assert clipped and out != text
    assert out.startswith("HEAD") and out.endswith("TAIL")   # 尾部（错误常见处）不缺席
    assert "省略" in out                                       # 诚实标注，不静默丢弃
    assert estimate_tokens(out) <= 200


def test_clip_head_tail_under_threshold_is_untouched():
    """未超阈值 → 原样返回（只折叠超限者，不无谓改写）。"""
    g = ContextGovernor(clip_max_tokens=10_000)
    assert g.clip_head_tail("短内容") == ("短内容", False)


def test_trim_messages_pinned_first_occupy_then_downgrade():
    """S3 校正②：pinned **按原顺序先占额度**；额度用尽的剩余 pinned **降级**为普通条目。

    同时校验：降级条目 id 记入 ``BlockReport.pinned_downgraded``（dataclass 字段，非裸 dict）。
    """
    from agent.plugins.context import BlockSpec

    g = ContextGovernor(max_context_tokens=100_000, reserve_output_tokens=0)
    big = "x" * 400                                  # ≈100 token
    msgs = [
        {"role": "user", "content": big, "pinned": True, "id": "p1"},
        {"role": "user", "content": big, "pinned": True, "id": "p2"},
        {"role": "user", "content": big, "pinned": True, "id": "p3"},
    ]
    kept, downgraded, _ = g._trim_messages(msgs, 250)      # 只够两条
    assert [m["id"] for m in kept] == ["p1", "p2"]         # 先占：按原顺序
    assert downgraded == ["p3"]                            # 剩余者降级（刚性失效，可追溯）

    g2 = ContextGovernor(max_context_tokens=250, reserve_output_tokens=0)
    asm = g2.assemble([BlockSpec("chain", "messages", True, [dict(m) for m in msgs],
                                 weight=1.0)])
    rep = next(r for r in asm.report.blocks if r.key == "chain")
    assert rep.pinned_downgraded == ["p3"]                 # dataclass 字段，消费方可直接读
    assert rep.trimmed is True


def test_trim_messages_zero_budget_records_all_pinned_downgraded():
    """无空间时 pinned 全部刚性失效——**诚实记录**，不静默当无事发生。"""
    g = ContextGovernor()
    msgs = [{"role": "user", "content": "a", "pinned": True, "id": "z1"}]
    kept, downgraded, _ = g._trim_messages(msgs, 0)
    assert kept == [] and downgraded == ["z1"]


def test_trim_messages_without_pinned_keeps_contiguous_newest():
    """无 pinned 时行为不变：从新到旧**连续**贪心（保消息配对不断裂）。"""
    g = ContextGovernor()
    msgs = [{"role": "user", "content": "x" * 400, "seq": i} for i in range(3)]
    kept, downgraded, _ = g._trim_messages(msgs, 250)
    assert [m["seq"] for m in kept] == [1, 2]              # 连续后缀，不抽空中间
    assert downgraded == []


def test_assemble_reserve_tokens_reduces_effective_budget():
    """S3 新增：``reserve_tokens`` 为后续块预留额度（防先装配者吃满）。"""
    from agent.plugins.context import BlockSpec

    g = ContextGovernor(max_context_tokens=1000, reserve_output_tokens=0)
    blocks = [BlockSpec("history", "messages", True,
                        [{"role": "user", "content": "y" * 4000, "seq": 1}], weight=1.0)]
    full = g.assemble(blocks)
    reserved = g.assemble(blocks, reserve_tokens=400)
    assert full.report.input_budget == 1000
    assert reserved.report.input_budget == 600             # 有效预算已扣除预留
    used_full = next(r.used for r in full.report.blocks if r.key == "history")
    used_res = next(r.used for r in reserved.report.blocks if r.key == "history")
    assert used_res <= used_full


def test_governor_has_no_business_wording():
    """S3 校正③守卫：治理器**不感知业务语义**——源码内不出现 ``observe`` 之类字样，
    只认通用元数据 ``pinned``（``type`` 属消费方私有，治理器不碰）。"""
    import re

    src = (PLUGIN_DIR / "context" / "governor.py").read_text(encoding="utf-8")
    assert "pinned" in src                                  # 通用元数据：认识
    assert not re.search(r"\bobserve", src, re.I)           # 业务字样：不认识
    assert not re.search(r"\bthink\b|\bact\b", src, re.I)


def test_s3_settings_registered_in_all_three_places():
    """S3 新参数必须**三处同步登记**（schema / owner / 默认值 / 前端 keys），
    否则前端不可见或后端拒收（G1 教训的制度化）。"""
    from agent.run import chat as srv
    from agent.tests._web_sources import frontend_settings_source

    src = frontend_settings_source()      # B4：`_PAGE` 已删除 → 读前端源码
    for k in ("clip_max_tokens", "clip_head_tokens", "clip_tail_tokens",
              "react_keep_recent", "react_min_chain_tokens"):
        assert k in srv._SETTING_SCHEMA, f"{k} 未登记 schema"
        assert srv._PLUGIN_OF_KEY[k] == "plugins.context", f"{k} 归属错误"
        assert k in srv._SETTING_DEFAULTS, f"{k} 缺默认值"
        assert srv._coerce_setting(k, srv._SETTING_DEFAULTS[k]) == srv._SETTING_DEFAULTS[k]
        assert f"'{k}'" in src, f"{k} 未同步到前端 keys/字段表"

