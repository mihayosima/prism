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
from agent.sys.llm import _tokens
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
    """CJK 0.75 token/字、ASCII 4 字符/token（json.dumps 含引号包装，计入开销）；
    sys.llm._tokens 别名与 core 同源。"""
    assert estimate_tokens("abcd") == 1                     # '"abcd"' 6 字符 → 1 token
    assert estimate_tokens("你好") == 2                     # '"你好"' 4 字符 + 2 CJK×0.75
    assert estimate_tokens("你是谁") > estimate_tokens("hi")  # 中文不再被 len//4 低估
    long_cjk = "上下文治理" * 50
    assert estimate_tokens(long_cjk) == _tokens(long_cjk)   # 兼容别名同源


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
