"""上下文治理插件测试（agent/plugins/context，G0 骨架）——
token 口径 / 裁剪原语 / 预算模型 / 历史收窗 / 目录插件语义（装配、停用降级、接缝注入）。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent.api import Services
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
        assert type(g).__name__ == "ContextGovernor"
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
    """agent.context 节经 config_schema 宽松放行（"_any"），不触发 ConfigError。"""
    c = _build(tmp_path, patch={"agent": {"context": {"max_context_tokens": 20000,
                                                      "compact_trigger": 8}}})

    async def s():
        await c.assemble()                          # 不抛 ConfigError 即通过
        assert c.root.has(Services.CONTEXT)

    _run(s())


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
