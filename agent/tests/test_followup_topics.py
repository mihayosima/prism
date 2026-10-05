"""后置课题实施测试（docs/governance/后置课题实施计划-口径链与懒加载-2026-09-29.md）：

- F1：口径校准因子（EWMA 维护 / cost_factor 消费 / 样本门槛 / 预借判定乘因子）
- F2：agent_budget_log.run_id 归组（react 经 REACT_RUN_ID 状态键）
- F3：口径漂移 >25% durable 事件
- P1/P2：链存档（react_chain:{session}）与 resume 续接；chat 关键词意图
- P3：账单 cache_hit 列迁移
- L1：懒加载（目录卡 / 元工具内联处理 / loaded 上限 / 分步工具视图）
- 设置页：cost_calibration / lazy_tools 三处同步登记

全部离线确定性，零触网。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

import pytest

from agent.api import Services, StateKeys
from agent.plugins.react.components import ToolCall
from agent.plugins.react.loop import (LAZY_META_TOOLS, ReactConfig, ReactLoop, TaskContext,
                                      _card, _clip_with_fallback, _domain_of,
                                      _slim_description, allowed_domains, clip_by_domain)
from agent.plugins.react.prompt import build_system, render_domains
from agent.profiles import BASE_AGENT_CONFIG, build_agent
from agent.run import chat as srv
from agent.sys.llm import LlmService
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _build(tmp_path, *, react: bool = True, context: bool = False,
           work_memory: bool = False, patch: dict | None = None):
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "state.json"))
    if react:
        pm.set_enabled("plugins.react", True)
    if context:
        pm.set_enabled("plugins.context", True)
    if work_memory:
        pm.set_enabled("plugins.work_memory", True)
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch, plugin_mgr=pm)


def _register_echo(ctx) -> None:
    async def _echo(text: str = "hi"):
        return {"echo": text}

    ctx.tasks.register(ctx, "test.echo",
                       schema={"text": {"type": "str", "required": False, "default": "hi"}},
                       handler=_echo, owner="test", description="回显参数")


# ---------------------------------------------------------------------------
# F1：口径校准因子
# ---------------------------------------------------------------------------

def test_cost_factor_applied_to_budget_judgement(tmp_path):
    """F1：样本达标后估算×因子参与预借判定（因子 2.0 → 提前触发拒绝线）。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db"),
                            "agent": {"budget": {"max_tokens_per_run": 200,
                                                 "finalize_reserve_tokens": 0}}})

    async def s():
        await c.assemble()
        big = [{"role": "user", "content": "x" * 600}]     # 裸估算 ≈150 < 200
        # 样本不足（门槛 5）→ 因子不生效，不拒
        c.root.state[StateKeys.LLM_COST_RATIO] = {"fixture": {"ratio": 2.0, "samples": 2}}
        await c.root.llm.complete(c.root, big, caller="t")
        # 样本达标 → est×2 ≈300 > 200 → 拒绝
        c.root.state[StateKeys.LLM_COST_RATIO] = {"fixture": {"ratio": 2.0, "samples": 9}}
        from agent.api import CapabilityNotImplemented
        with pytest.raises(CapabilityNotImplemented):
            await c.root.llm.complete(c.root, big, caller="t")
        await c.teardown()

    _run(s())


def test_ewma_maintained_after_calls(tmp_path):
    """F1：每次调用后 EWMA 维护（样本数递增，ratio 落在夹紧区间）。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")})

    async def s():
        await c.assemble()
        for _ in range(2):
            await c.root.llm.complete(c.root, [{"role": "user", "content": "你好"}],
                                      caller="t")
            for _ in range(5):                   # _post 是 async fire-and-forget
                await asyncio.sleep(0)           # 且同一 caller 的 pending 槽会被下次覆盖
        stats = c.root.state.get(StateKeys.LLM_COST_RATIO) or {}
        m = stats.get("fixture") or {}
        assert int(m.get("samples") or 0) >= 2, "每次调用须喂 EWMA 样本"
        assert 0.5 <= float(m.get("ratio") or 0) <= 3.0
        await c.teardown()

    _run(s())


def test_cost_calibration_off_disables_factor(tmp_path):
    """F1：cost_calibration=False → 因子不参与判定（裸估算现状）。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db"),
                            "agent": {"budget": {"max_tokens_per_run": 200,
                                                 "finalize_reserve_tokens": 0,
                                                 "cost_calibration": False}}})

    async def s():
        await c.assemble()
        c.root.state[StateKeys.LLM_COST_RATIO] = {"fixture": {"ratio": 9.0, "samples": 99}}
        await c.root.llm.complete(c.root, [{"role": "user", "content": "x" * 600}],
                                  caller="t")          # 裸估算 ≈150 < 200 → 不拒
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# F2 / F3 / P3：账单归组 · 漂移事件 · 缓存列
# ---------------------------------------------------------------------------

def test_budget_log_records_run_id(tmp_path):
    """F2：react 的 LLM 调用经 REACT_RUN_ID 状态键归组落账。"""
    c = _build(tmp_path, context=True)

    async def s():
        await c.assemble()
        _register_echo(c.root)
        await c.root.react.run(c.root, "请触顶循环")
        for _ in range(5):                       # _post 是 async fire-and-forget，让出循环
            await asyncio.sleep(0)
        rows = await c.root.db.query(
            c.root, "SELECT run_id FROM agent_budget_log"
                    " WHERE run_id IS NOT NULL AND run_id != ''")
        assert rows, "react 调用的账单须带 run_id 归组键"
        await c.teardown()

    _run(s())


def test_budget_log_has_cache_hit_column(tmp_path):
    """P3：agent_budget_log 幂等迁移出 cache_hit 列（可写入可查询）。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")})

    async def s():
        await c.assemble()
        await c.root.db.execute(
            c.root, "INSERT INTO agent_budget_log(model, tokens_in, tokens_out,"
                    " est_cost, run_id, cache_hit, at) VALUES (?,?,?,?,?,?,?)",
            ("m", 1, 2, None, "r1", 500, "t"))
        rows = await c.root.db.query(
            c.root, "SELECT cache_hit, run_id FROM agent_budget_log WHERE run_id='r1'")
        assert rows and rows[0]["cache_hit"] == 500
        await c.teardown()

    _run(s())


def test_cost_drift_event_on_big_deviation(tmp_path):
    """F3：真实 usage 与估算偏差 >25% → durable agent/llm/cost-drift（容器内经 budget）。"""
    c = build_agent(config={"db_path": str(tmp_path / "agent.db")})

    class _Prov:
        label = "stub"

        async def complete(self, ctx, messages, schema=None, tools=None):
            return {"text": "ok",
                    "usage": {"prompt_tokens": 1000, "completion_tokens": 10}}

    async def s():
        await c.assemble()
        c.root.llm._provider = _Prov()           # 注入大 usage 的 stub provider
        await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}], caller="t")
        for _ in range(5):                       # _post 是 async fire-and-forget，让出循环
            await asyncio.sleep(0)
        rows = await c.root.db.query(
            c.root, "SELECT event FROM agent_events WHERE event='agent/llm/cost-drift'")
        assert rows, "偏差 >25% 须落 drift 事件"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# P1 / P2：链存档与续接
# ---------------------------------------------------------------------------

def test_chain_archived_to_work_memory(tmp_path):
    """P1：run 收尾把折叠链存工作记忆 react_chain:{session}，并发 archived 事件。"""
    c = _build(tmp_path, context=True, work_memory=True,
               patch={"agent": {"react": {"max_steps": 3}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        await c.root.react.run(c.root, "请触顶循环")
        wm = c.root.get(Services.WORK_MEMORY)
        # 2.2：归档键按**会话键** ``{user}:{session}``（不同用户同名会话不串档）
        arc = await wm.get("react_chain:default:default")   # 未传 session → 全 default
        assert arc, "链存档必须落工作记忆"
        cps = json.loads(arc.get("checkpoints") or "[]")
        data = next((x.get("data") or {} for x in reversed(cps)
                     if x.get("name") == "chain"), {})
        assert data.get("chain"), "存档须携带链条目"
        rows = await c.root.db.query(
            c.root, "SELECT event FROM agent_events WHERE event='agent/react/archived'")
        assert rows, "存档须落 durable 事件"
        await c.teardown()

    _run(s())


def test_resume_loads_archived_chain(tmp_path):
    """P2：resume=True 时上轮链进入本轮窗口；resume=False 不进入。"""
    c = _build(tmp_path, context=True, work_memory=True,
               patch={"agent": {"react": {"max_steps": 2}}})

    async def s():
        await c.assemble()
        _register_echo(c.root)
        await c.root.react.run(c.root, "请触顶循环")           # 第一轮：存档

        loop = c.root.react
        orig = loop._reasoner
        captured: list[list[dict]] = []

        class _Spy:
            async def reason(self, ctx, messages, tools):
                captured.append(list(messages))
                return await orig.reason(ctx, messages, tools)

        loop._reasoner = _Spy()
        await loop.run(c.root, "继续", session_id="default", resume=True)
        assert any(m.get("id") == "step1_act" for m in captured[0]), \
            "续接时窗口必须包含上轮链条目"

        captured.clear()
        await loop.run(c.root, "新任务", session_id="default", resume=False)
        assert not any(m.get("id") == "step1_act" for m in captured[0]), \
            "非续接不得携带上轮链"
        await c.teardown()

    _run(s())


def test_chat_resume_intent_keywords():
    """P2：chat 续接意图按词表匹配（配置化；空词表恒 False）。"""

    class _Ctx:
        config = {"agent": {"react": {"resume_keywords": ["继续", "continue"]}}}

    from agent.sys.chat import is_resume_intent
    assert is_resume_intent(_Ctx(), "继续写第10章") is True
    assert is_resume_intent(_Ctx(), "please continue") is True
    assert is_resume_intent(_Ctx(), "换个话题，讲讲天气") is False
    assert is_resume_intent(_Ctx(), "") is False

    class _NoKw:
        config = {"agent": {"react": {"resume_keywords": []}}}

    assert is_resume_intent(_NoKw(), "继续") is False


# ---------------------------------------------------------------------------
# L1：懒加载
# ---------------------------------------------------------------------------

_LAZY_CFG = ReactConfig(lazy_tools=True, lazy_tools_max=2)


def _lazy_tools():
    return [{"name": n, "description": f"{n} 工具的说明", "params": {"x": {}},
             "required": ["x"], "risk": "read", "owner": "t"}
            for n in ("a", "b", "c", "d")]


def test_lazy_partition_and_cap():
    """L1：元工具内联处理；loaded 上限诚实拒绝；未加载调用回错误观察。

    P2-4：``_partition_meta`` 改为 async（``wm_read`` 读回需异步工作记忆接缝），
    首参为工作记忆接缝（本用例不涉及读回 → 传 ``None``）。
    """
    async def s():
        loop = ReactLoop(None, None, None, None, _LAZY_CFG)      # type: ignore[arg-type]
        from agent.plugins.react.loop import TaskContext
        task = TaskContext(loaded=set())
        calls = [ToolCall(name="tool_search", arguments={"keyword": "a"}, id="m1"),
                 ToolCall(name="tool_load", arguments={"names": "a,b,c"}, id="m2"),
                 ToolCall(name="d", arguments={}, id="u1")]
        meta, real = await loop._partition_meta(None, task, calls, _lazy_tools())
        assert [r.name for r in meta] == ["tool_search", "tool_load", "d"]
        assert real == [], "未加载的 d 不得进入真实执行"
        assert task.loaded == {"a", "b"}, "上限 2 → 只装载前两个，c 暂缓"
        assert "暂缓" in meta[1].content and "c" in meta[1].content
        assert "未加载" in meta[2].content and "tool_load" in meta[2].content

        # 已加载后 → 进入真实执行
        calls2 = [ToolCall(name="a", arguments={"x": 1}, id="r1")]
        meta2, real2 = await loop._partition_meta(None, task, calls2, _lazy_tools())
        assert meta2 == [] and [x.name for x in real2] == ["a"]

    asyncio.run(s())


def test_lazy_tools_for_step_view():
    """L1：分步工具视图 = 元工具 + 已加载完整定义 + 其余目录卡（卡无 schema）。"""
    loop = ReactLoop(None, None, None, None, _LAZY_CFG)      # type: ignore[arg-type]
    from agent.plugins.react.loop import TaskContext
    task = TaskContext(loaded={"a"})
    view = loop._tools_for_step(_lazy_tools(), task)
    names = [t["name"] for t in view]
    assert names[:2] == ["tool_search", "tool_load"], "元工具恒在且带完整定义"
    by = {t["name"]: t for t in view}
    assert by["a"].get("params") == {"x": {}}, "已加载 → 完整定义"
    assert by["b"].get("params") == {} and by["b"].get("required") == [], \
        "未加载 → 目录卡（无 schema）"
    assert _slim_description(by["b"].get("description"))


def test_lazy_config_wiring(tmp_path):
    """L1：react.lazy_tools 配置装配生效（默认 off）。"""
    c1 = _build(tmp_path)
    c2 = _build(tmp_path, patch={"agent": {"react": {"lazy_tools": True}}})

    async def s():
        await c1.assemble()
        assert c1.root.react.config.lazy_tools is False
        await c1.teardown()
        await c2.assemble()
        assert c2.root.react.config.lazy_tools is True
        assert c2.root.react.config.lazy_tools_max == 12
        await c2.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# 设置页：三处同步登记
# ---------------------------------------------------------------------------

def test_new_toggles_registered_in_settings():
    """cost_calibration / lazy_tools / alignment_enabled 三处登记；默认与 profiles 同源；bool 钳制。"""
    from agent.tests._web_sources import frontend_settings_source

    src = frontend_settings_source()      # B4：`_PAGE` 已删除 → 读前端源码
    k1, k2, k3 = "cost_calibration", "lazy_tools", "alignment_enabled"
    for k, pid, default in ((k1, "plugins.budget", True),
                            (k2, "plugins.react", False),
                            (k3, "plugins.react", True)):
        assert k in srv._SETTING_SCHEMA
        assert srv._PLUGIN_OF_KEY[k] == pid, f"{k} 归属错误"
        assert k in srv._SETTING_DEFAULTS
        assert srv._SETTING_DEFAULTS[k] == default, f"{k} 默认值与决议漂移"
        assert f"set_{k}" in src, f"{k} 未同步到前端"
    assert srv._coerce_setting(k1, "on") is True
    assert srv._coerce_setting(k1, "off") is False
    assert srv._coerce_setting(k2, True) is True
    assert srv._coerce_setting(k3, "off") is False and srv._coerce_setting(k3, "on") is True


def test_card_is_projection_only():
    """L1：目录卡是纯投影——原目录 dict 不被改动。"""
    t = {"name": "x", "description": "第一行\n第二行", "params": {"p": {"type": "str"}},
         "required": ["p"], "risk": "read", "owner": "o"}
    card = _card(t)
    assert card["params"] == {} and card["required"] == []
    assert t["params"] == {"p": {"type": "str"}} and t["required"] == ["p"], \
        "原目录不得被投影改动"


# ---------------------------------------------------------------------------
# G（路线 2）：投影层「域级裁剪」——纯函数层与接线层
# 依据 docs/governance/工具税域级裁剪实施方案-2026-09-30.md
# ---------------------------------------------------------------------------

def _dom_tools():
    """两域样本：d1 两个工具、d2 一个工具、一个无 owner 工具（归一为空域）。"""
    return [{"name": "a1", "description": "A1", "params": {"x": {}}, "required": ["x"],
             "risk": "read", "owner": "d1"},
            {"name": "a2", "description": "A2", "params": {"y": {}}, "required": [],
             "risk": "write", "owner": "d1"},
            {"name": "b1", "description": "B1", "params": {"z": {}}, "required": ["z"],
             "risk": "write", "owner": "d2"},
            {"name": "n1", "description": "N1", "params": {}, "required": [],
             "risk": "write"}]


class _CfgStub:
    """仅承载 ``ctx.config`` 的最小桩（allowed_domains 只读这一个接缝）。"""

    def __init__(self, react: dict | None):
        self.config = {"agent": {"react": react or {}}}


def test_clip_by_domain_is_pure_projection():
    """G1：``allowed is None`` 原样返回同一对象；白名单裁剪且不改入参。"""
    tools = _dom_tools()
    assert _domain_of({"owner": "d1"}) == "d1"
    assert _domain_of({"name": "x"}) == "" and _domain_of({"owner": None}) == "", \
        "缺失/空 owner 归一为空串（渲染域清单时标注为「未归属」）"
    same, hidden = clip_by_domain(tools, None)
    assert same is tools and hidden == {}, "None ⇒ 不裁剪且保持同一列表对象"

    kept, hidden = clip_by_domain(tools, {"d1"})
    assert [t["name"] for t in kept] == ["a1", "a2"]
    assert hidden == {"d2": 1, "": 1}, "隐藏域计数含「未归属」空域"
    assert len(tools) == 4, "纯投影：入参列表不被改动"

    kept, hidden = clip_by_domain(tools, set())
    assert kept == [] and hidden == {"d1": 2, "d2": 1, "": 1}


def test_allowed_domains_config_branches():
    """G1/G6：开关关 / 空白名单 ⇒ None（不裁剪）；白名单 ⇒ 去空白后的集合。"""
    assert allowed_domains(_CfgStub(None)) is None
    assert allowed_domains(_CfgStub({"tool_domains_enabled": False,
                                     "tool_domains": ["d1"]})) is None, "总开关关 ⇒ 不裁剪"
    assert allowed_domains(_CfgStub({"tool_domains_enabled": True,
                                     "tool_domains": []})) is None, "空白名单 ⇒ 不做全隐藏"
    assert allowed_domains(_CfgStub({"tool_domains_enabled": True,
                                     "tool_domains": ["  ", ""]})) is None, "全空白 ⇒ 不裁剪"
    got = allowed_domains(_CfgStub({"tool_domains_enabled": True,
                                    "tool_domains": [" d1 ", "d2"]}))
    assert got == {"d1", "d2"}, "域名去空白"
    assert allowed_domains(_CfgStub({"tool_domains_enabled": True,
                                     "tool_domains": "d1"})) == {"d1"}, "容忍非列表（字符串按单域）"


def test_clip_with_fallback_typo_guard():
    """安全阀：白名单与任何已注册域都不相交（拼错）⇒ 回退为不裁剪，不静默全隐藏。"""
    tools = _dom_tools()
    allowed, vis, hidden = _clip_with_fallback(tools, {"d9"})
    assert allowed is None and vis is tools and hidden == {}, "拼错域名 ⇒ 回退不裁剪"
    allowed, vis, hidden = _clip_with_fallback(tools, {"d1"})
    assert allowed == {"d1"} and [t["name"] for t in vis] == ["a1", "a2"]
    allowed, vis, hidden = _clip_with_fallback([], {"d9"})
    assert allowed == {"d9"} and vis == [], "无工具时不做无谓回退"


def test_tools_for_step_domain_view():
    """G1/G3：域过滤后 native view —— 隐藏域不出现；已加载者跨域仍出现完整定义。"""
    tools = _dom_tools()
    task = TaskContext(loaded={"b1"})            # b1 属隐藏域 d2，但已加载
    view = ReactLoop._tools_for_step(tools, task, allowed={"d1"})
    names = [t["name"] for t in view]
    assert names[:3] == ["tool_search", "tool_load", "wm_read"], "元工具恒在"
    assert "a1" in names and "a2" in names, "放行域 ⇒ 目录卡"
    assert "b1" in names, "已加载 ⇒ 跨域放行（tool_load 成功即自动解禁）"
    assert "n1" not in names, "隐藏域且未加载 ⇒ 不出现在 native tools"
    by = {t["name"]: t for t in view}
    assert by["b1"].get("params") == {"z": {}}, "已加载 ⇒ 完整定义（跨域放行）"
    assert by["a1"].get("params") == {} and by["a1"].get("required") == [], "a1 为目录卡"


def test_tools_for_step_default_is_bytewise_legacy():
    """G1 对称断言：``allowed=None`` 时新实现与改造前逐字节相等。"""
    tools = _dom_tools()
    for loaded in (set(), {"a1"}, {"a1", "b1"}):
        task = TaskContext(loaded=set(loaded))
        legacy = [dict(m) for m in LAZY_META_TOOLS]
        for t in tools:
            legacy.append(t if t.get("name") in task.loaded else _card(t))
        assert ReactLoop._tools_for_step(tools, task, None) == legacy
        assert ReactLoop._tools_for_step(tools, task) == legacy, "缺省参必须与显式 None 等价"


def test_render_domains_and_build_system_default():
    """G1/G4：无隐藏域时系统提示逐字节不变；有隐藏域时出现「域清单」。"""
    cat = [_card(t) for t in _dom_tools()]
    sm = "身份：测试"
    assert render_domains(None) == "" and render_domains({}) == ""
    assert build_system(cat, sm, hidden_domains=None) == build_system(cat, sm)
    assert build_system(cat, "", hidden_domains={}) == build_system(cat, "")

    out = build_system(cat, sm, hidden_domains={"d2": 1})
    assert "其它工具域（当前未展开，可用 tool_search 检索）" in out
    assert "- d2（1 个）" in out
    assert out.index("可用工具") < out.index("其它工具域") < out.index("运行环境")


def test_tool_domains_registered_in_settings():
    """C5：两个新键三处同步登记 + 前端字面量在场 + 钳制语义（含 None 陷阱）。"""
    from agent.tests._web_sources import frontend_settings_source

    src = frontend_settings_source()
    react = (BASE_AGENT_CONFIG.get("agent") or {}).get("react") or {}
    for k in ("tool_domains_enabled", "tool_domains"):
        assert k in srv._SETTING_SCHEMA, f"{k} 未登记 schema"
        assert srv._PLUGIN_OF_KEY[k] == "plugins.react", f"{k} 归属错误"
        assert k in srv._SETTING_DEFAULTS
        assert srv._SETTING_DEFAULTS[k] == react[k], f"{k} 与 profiles 默认漂移"
        assert f'id="set_{k}"' in src, f"{k} 未同步到前端（须为独立控件 id）"

    # 白名单是「下拉可选」而非手填：下拉触发条 + 默认收起的面板（面板内是复选项）
    assert 'id="set_tool_domains_btn"' in src, "放行域缺少下拉触发条（未改成下拉框形态）"
    assert 'class="set_tool_domain_cb"' in src, "放行域未渲染为可多选的候选项"
    panel = re.search(r'id="set_tool_domains"([^>]*)>', src)
    assert panel is not None, "放行域未渲染下拉面板容器"
    assert "hidden" in panel.group(1), \
        "放行域面板未默认收起（应呈下拉形态，而非常驻展开的列表）"
    assert 'id="set_tool_domains" type="text"' not in src, \
        "放行域仍是手填文本框（应改为可勾选）"
    assert 'placeholder="如 plugins.windows_mcp' not in src, "仍残留手填提示文案"

    # 钳制：布尔照 lazy_tools；列表照 ask（支持中英文逗号/顿号，过滤空段）
    assert srv._coerce_setting("tool_domains_enabled", "on") is True
    assert srv._coerce_setting("tool_domains_enabled", False) is False
    assert srv._coerce_setting("tool_domains", "a, b，c、d") == ["a", "b", "c", "d"]
    assert srv._coerce_setting("tool_domains", []) == []
    assert srv._coerce_setting("tool_domains", "plugins.windows_mcp") == ["plugins.windows_mcp"]

    # 陷阱回归（2026-10-04 改为「陷阱已修」）：
    # 原实现把 ``None`` 收成字符串 ``"None"``（幽灵项）；此处**曾**断言 ``== ["None"]``，
    # 借这个副作用来提醒「_SETTING_DEFAULTS 必须是 [] 而非 None」。
    # 副作用式守卫的致命问题是：**陷阱被修好的那天，守卫也一起失效**。
    # 现改为直接断言默认值本身——防护更明确，且不依赖任何缺陷行为。
    assert srv._coerce_setting("tool_domains", None) == [], \
        "None 必须当空列表，不能产生字面量 'None' 幽灵项"
    assert srv._SETTING_DEFAULTS["tool_domains"] == [], \
        "默认值必须是 [] 而非 None（否则设置页会渲染出一个 None 候选项）"
    assert srv._coerce_setting("tool_domains", srv._SETTING_DEFAULTS["tool_domains"]) == []


def test_domain_options_listing():
    """C6：设置页「可勾选」候选项的生成规则（已注册域 ∪ 已选但未注册域）。"""
    from agent.run.chat import _domain_options

    tools = [
        {"name": "mcp.windows.App", "owner": "plugins.windows_mcp"},
        {"name": "mcp.windows.Click", "owner": "plugins.windows_mcp"},
        {"name": "agent.ping", "owner": "agent.tasks"},
        {"name": "no.owner", "owner": ""},                      # 异常数据：不计入候选项
        {"name": "none.owner"},                                 # 缺失 owner：同上
    ]

    # 已注册域按名字排序；owner 为空的不出现（不引导用户去勾选异常数据）
    assert _domain_options(tools, []) == [
        {"name": "agent.tasks", "count": 1, "available": True},
        {"name": "plugins.windows_mcp", "count": 2, "available": True},
    ]

    # ★ 关键：已选但当前未注册的域必须保留（available=False），否则保存时会被静默丢弃
    opts = _domain_options(tools, ["plugins.windows_mcp", "plugins.gone"])
    assert opts == [
        {"name": "agent.tasks", "count": 1, "available": True},
        {"name": "plugins.gone", "count": 0, "available": False},
        {"name": "plugins.windows_mcp", "count": 2, "available": True},
    ]

    # 已注册且已选的域只出现一次（不被「配置回填」重复追加）
    once = _domain_options(tools, ["agent.tasks", "agent.tasks"])
    assert len([o for o in once if o["name"] == "agent.tasks"]) == 1

    # 容错：selected 为 None / 裸字符串 / 含空白，均不抛错且语义正确
    assert _domain_options(tools, None) == _domain_options(tools, [])
    assert _domain_options(tools, "") == _domain_options(tools, [])
    # 裸字符串包成单元素（不得逐字符拆成一堆单字候选项）
    assert [o["name"] for o in _domain_options(tools, "plugins.gone")] == [
        "agent.tasks", "plugins.gone", "plugins.windows_mcp"]
    # 空白串被规整后不与已注册域重复
    assert [o["name"] for o in _domain_options(tools, ["  agent.tasks  "])].count("agent.tasks") == 1

    # 空目录 ⇒ 空清单（前端据此渲染「暂无已注册工具域」空态，且不提交 patch）
    assert _domain_options([], []) == []
