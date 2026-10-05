"""阶段 2（G2）：上下文管理与预算**会话级化** —— T1–T10 覆盖。

对应《上下文与预算会话级化-实施方案-2026-10-02》§6 测试计划：

| 编号 | 用例 |
|---|---|
| T1 | 会话级窗口隔离（不同会话窗口参数互不影响） |
| T2 | 无覆盖等价性（未设覆盖时逐值等于裸治理器） |
| T3 | 协议不漂移（门面转发治理器全部公开成员） |
| T4/T5 | 预算额度与累计按会话隔离 |
| T6 | **P0 回归**：清会话 A 不清零 B 的预算 |
| T7 | 账单按会话归因（``agent_budget_log.session`` 与累计对拍） |
| T8 | 校准共享（``LLM_COST_RATIO`` 仍单份全局） |
| T9 | 配置存储 v1→v2 迁移（读兼容 / 写前备份 / 版本守卫 / 无平铺副本） |
| T10 | sessions LRU 上限（只淘汰覆盖，不动会话数据） |

手法：容器级集成（真装配 + fixture LLM，零网络）+ 纯函数单测（存储规则）。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent.api import (CapabilityNotImplemented, SESSION_CFG_VIEW, Services,
                       StateKeys)
from agent.core.scope_config import (evict_lru, merge_overrides, section_of,
                                     touch)
from agent.plugins.context import ContextGovernor
from agent.profiles import build_agent
from agent.run.config_store import ConfigStore
from agent.sys.plugin_manager import PluginManager

PLUGIN_DIR = Path(__file__).resolve().parent.parent / "plugins"


def _run(coro):
    return asyncio.run(coro)


def _pm(tmp_path) -> PluginManager:
    pm = PluginManager(plugin_dir=str(PLUGIN_DIR),
                       state_file=str(tmp_path / "plugin_state.json"))
    pm.set_enabled("plugins.context", True)
    return pm


def _build(tmp_path, *, patch: dict | None = None):
    return build_agent(config={"db_path": str(tmp_path / "agent.db")},
                       patch=patch, plugin_mgr=_pm(tmp_path))


def _spent(ctx) -> dict:
    m = ctx.state.get(StateKeys.BUDGET_SPENT)
    return m if isinstance(m, dict) else {}


async def _settle(ctx) -> None:
    """等待 `LLM_POST_CALL`（**异步** fire-and-forget）对账任务**确定性**落地。

    不对账时 ``budget_spent`` 还停在预借值、账单行也未写——断言会看到中间态。
    此前用 ``await asyncio.sleep(0)`` × N：绑定 DB 后对账经 ``to_thread`` 卸载，
    只要线程往返比 N 次事件循环轮次慢就会漏（实测挂红）⇒ 改用 ``EventBus.idle()``
    ——轮询到全部在途监听器任务结束，与循环轮次/线程调度无关。
    """
    await ctx.events.idle()


# ---------------------------------------------------------------------------
# T1：会话级窗口隔离
# ---------------------------------------------------------------------------

def test_t1_session_window_isolated_and_container_default(tmp_path):
    """会话 A 设小窗，B 不设 ⇒ A 的输入预算更小，B 恒等于容器默认。"""
    store = ConfigStore(path=tmp_path / "cfg.json")
    store.patch_session("u:A", {"plugins.context": {"max_context_tokens": 8000}})
    c = _build(tmp_path, patch={"agent": {"context": {"max_context_tokens": 24000,
                                                      "reserve_output_tokens": 4000}}})

    async def s():
        await c.assemble()
        c.root.provide(SESSION_CFG_VIEW, store.view)
        gov = c.root.get(Services.CONTEXT)
        c.root.state[StateKeys.SESSION_KEY] = ""
        base = gov.input_budget()
        c.root.state[StateKeys.SESSION_KEY] = "u:A"
        a = gov.input_budget()
        c.root.state[StateKeys.SESSION_KEY] = "u:B"
        b = gov.input_budget()
        assert base == 20000, "容器默认应为 24000-4000"
        assert a == 4000, "会话 A 应使用自己的窗口（8000-4000）"
        assert b == base, "无覆盖的会话必须等于容器默认（继承而非替换）"
        # 恢复到容器级：同一对象随会话键变化而变（消费方零改动的前提）
        c.root.state[StateKeys.SESSION_KEY] = ""
        assert gov.input_budget() == base
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# T2：无覆盖等价性（逐值对拍裸治理器）
# ---------------------------------------------------------------------------

_SCALAR_ATTRS = ("max_context_tokens", "reserve_output_tokens", "memory_ratio",
                 "history_ratio", "source_clip_tokens", "working_window_tokens",
                 "window_high_ratio", "window_low_ratio")
_METHODS = ("input_budget", "memory_budget", "history_budget", "source_clip_budget",
            "react_keep_recent", "react_min_chain_tokens", "clip_max_tokens",
            "clip_head_tokens", "clip_tail_tokens", "compact_mode",
            "clear_at_least_tokens")


def test_t2_no_override_is_value_equivalent(tmp_path):
    """未设任何会话覆盖（含注入了空视图、以及有会话键但无覆盖）⇒ 逐值等于裸治理器。"""
    store = ConfigStore(path=tmp_path / "cfg.json")
    c = _build(tmp_path, patch={"agent": {"context": {"max_context_tokens": 20000,
                                                      "memory_ratio": 0.25,
                                                      "compact_mode": "on"}}})

    async def s():
        await c.assemble()
        c.root.provide(SESSION_CFG_VIEW, store.view)
        gov = c.root.get(Services.CONTEXT)
        bare = ContextGovernor.from_config(
            ((c.root.config or {}).get("agent") or {}).get("context"))
        for sk in ("", "u:A"):
            c.root.state[StateKeys.SESSION_KEY] = sk
            for name in _SCALAR_ATTRS:
                assert getattr(gov, name) == getattr(bare, name), f"{name} 漂移（sk={sk!r}）"
            for name in _METHODS:
                assert getattr(gov, name)() == getattr(bare, name)(), f"{name}() 漂移"
            assert gov.react_budget(1000) == bare.react_budget(1000)
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# T3：协议不漂移（门面转发全部公开成员）
# ---------------------------------------------------------------------------

_REQUIRED_MEMBERS = {
    "assemble", "plan", "execute", "pick_history", "declare", "assess",
    "ledger", "render_ledger", "check_invariants", "clip_head_tail",
    "input_budget", "memory_budget", "history_budget", "source_clip_budget",
    "react_budget", "react_keep_recent", "react_min_chain_tokens",
    "clip_max_tokens", "clip_head_tokens", "clip_tail_tokens",
}


def test_t3_router_forwards_every_public_member(tmp_path):
    """门面须转发治理器的**全部公开成员**——治理器新增方法时门面自动跟随。"""
    c = _build(tmp_path)

    async def s():
        await c.assemble()
        gov = c.root.get(Services.CONTEXT)
        bare = ContextGovernor.from_config(None)
        assert _REQUIRED_MEMBERS <= set(dir(bare)), "协议面收缩（用例需同步更新）"
        for name in _REQUIRED_MEMBERS:
            assert hasattr(gov, name), f"门面缺 {name} —— 协议漂移"
        # 契约守卫：门面**不得**自带同名公开成员而遮蔽治理器（当前只允许 current）
        own_public = {n for n in vars(type(gov)) if not n.startswith("_")}
        assert own_public <= {"current"}, f"门面新增公开成员会遮蔽治理器：{own_public}"
        assert "current" not in dir(bare), "治理器出现 current ⇒ 会被门面遮蔽，须改名"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# T4/T5：预算额度与累计按会话隔离
# ---------------------------------------------------------------------------

def test_t4_t5_budget_limit_and_spend_isolated(tmp_path):
    """A 上限 1000 被拒；B 上限 100000 正常；B 的消耗不记到 A。"""
    store = ConfigStore(path=tmp_path / "cfg.json")
    store.patch_session("u:A", {"plugins.budget": {"max_tokens_per_run": 1000,
                                                   "finalize_reserve_tokens": 0}})
    store.patch_session("u:B", {"plugins.budget": {"max_tokens_per_run": 100000,
                                                   "finalize_reserve_tokens": 0}})
    c = _build(tmp_path, patch={"agent": {"budget": {"max_tokens_per_run": 50000,
                                                     "finalize_reserve_tokens": 0,
                                                     "cost_calibration": False}}})

    async def s():
        await c.assemble()
        c.root.provide(SESSION_CFG_VIEW, store.view)
        big = [{"role": "user", "content": "x" * 8000}]      # 估算 ≈2000 > A 的 1000
        c.root.state[StateKeys.SESSION_KEY] = "u:A"
        with pytest.raises(CapabilityNotImplemented):
            await c.root.llm.complete(c.root, big, caller="t")
        assert _spent(c.root).get("u:A", 0) == 0, "拒绝发生在预借之前，A 不应有累计"

        c.root.state[StateKeys.SESSION_KEY] = "u:B"
        r = await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}],
                                      caller="t")
        await _settle(c.root)
        assert r.tokens_in > 0
        m = _spent(c.root)
        assert m.get("u:A", 0) == 0, "T5：B 的消耗串到了 A 的桶"
        assert m.get("u:B", 0) > 0, "B 的消耗未落到自己的桶"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# T6：P0 回归 —— 清会话 A 不清零 B 的预算
# ---------------------------------------------------------------------------

def test_t6_chat_reset_only_clears_own_session(tmp_path):
    """清空会话 A ⇒ A 的累计归零，**B 的累计必须原样保留**（P0 缺陷的修复证明）。"""
    c = _build(tmp_path, patch={"agent": {"budget": {"max_tokens_per_run": 100000,
                                                     "finalize_reserve_tokens": 0,
                                                     "cost_calibration": False}}})

    async def s():
        await c.assemble()
        msg = [{"role": "user", "content": "x" * 200}]
        for sk in ("u:A", "u:B"):
            c.root.state[StateKeys.SESSION_KEY] = sk
            await c.root.llm.complete(c.root, msg, caller="t")
            await _settle(c.root)                                   # 先让对账落地再切会话
        m = _spent(c.root)
        a_before, b_before = m.get("u:A", 0), m.get("u:B", 0)
        assert a_before > 0 and b_before > 0

        await c.root.chat.reset(c.root, "u:A")               # 只清 A
        m = _spent(c.root)
        assert m.get("u:A", 0) == 0, "A 自身未被清零"
        assert m.get("u:B", 0) == b_before, "清 A 把 B 的预算清零了（P0 回归）"
        await c.teardown()

    _run(s())


def test_t6b_legacy_reset_without_session_falls_back_to_all(tmp_path):
    """兼容：不带 ``session_id`` 的历史 CHAT_RESET ⇒ 回退清全部（不劣化旧调用方）。"""
    from agent.api import Events
    c = _build(tmp_path, patch={"agent": {"budget": {"cost_calibration": False}}})

    async def s():
        await c.assemble()
        c.root.state[StateKeys.SESSION_KEY] = "u:A"
        await c.root.llm.complete(c.root, [{"role": "user", "content": "hi"}], caller="t")
        await _settle(c.root)
        assert _spent(c.root).get("u:A", 0) > 0
        await c.root.events.emit(Events.CHAT_RESET, {})       # 无 session_id
        assert not _spent(c.root), "无 session_id 时应回退清全部"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# T7：账单按会话归因
# ---------------------------------------------------------------------------

def test_t7_budget_log_attributed_by_session(tmp_path):
    """``agent_budget_log.session`` 写入正确，且 Σ(token) 与本会话累计对拍。"""
    c = _build(tmp_path, patch={"agent": {"budget": {"max_tokens_per_run": 100000,
                                                     "finalize_reserve_tokens": 0,
                                                     "cost_calibration": False}}})

    async def s():
        await c.assemble()
        msg = [{"role": "user", "content": "x" * 200}]
        for sk in ("u:A", "u:A", "u:B"):
            c.root.state[StateKeys.SESSION_KEY] = sk
            await c.root.llm.complete(c.root, msg, caller="t")
            await _settle(c.root)
        rows = await c.root.db.query(
            c.root, "SELECT session, tokens_in, tokens_out FROM agent_budget_log")
        assert len(rows) == 3
        per: dict[str, int] = {}
        for r in rows:
            per[r["session"]] = per.get(r["session"], 0) + int(r["tokens_in"] or 0) + \
                int(r["tokens_out"] or 0)
        spent = _spent(c.root)
        assert per["u:A"] == spent.get("u:A", 0), "A 的账单合计与累计不一致"
        assert per["u:B"] == spent.get("u:B", 0), "B 的账单合计与累计不一致"
        assert len([r for r in rows if r["session"] == "u:A"]) == 2
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# T8：校准共享（LLM_COST_RATIO 仍单份全局）
# ---------------------------------------------------------------------------

def test_t8_cost_calibration_stays_global(tmp_path):
    """两会话交替调用后，校准样本仍累加在**同一份**按模型分桶的统计里。"""
    c = _build(tmp_path, patch={"agent": {"budget": {"max_tokens_per_run": 100000,
                                                     "finalize_reserve_tokens": 0,
                                                     "cost_calibration": True}}})

    async def s():
        await c.assemble()
        msg = [{"role": "user", "content": "x" * 400}]
        for sk in ("u:A", "u:B"):
            c.root.state[StateKeys.SESSION_KEY] = sk
            await c.root.llm.complete(c.root, msg, caller="t")
            await _settle(c.root)
        stats = c.root.state.get(StateKeys.LLM_COST_RATIO) or {}
        assert stats, "校准统计未写入"
        total = sum(int(v.get("samples") or 0) for v in stats.values())
        assert total == 2, f"校准应全局累加（两次调用 ⇒ 2 样本），实得 {total}"
        await c.teardown()

    _run(s())


# ---------------------------------------------------------------------------
# T9：配置存储 v1→v2 迁移
# ---------------------------------------------------------------------------

def test_t9_store_v1_read_compat_and_v2_write(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"plugins.budget": {"max_tokens_per_run": 123}}),
                 encoding="utf-8")
    st = ConfigStore(path=p)
    assert st.is_v1 and st.container()["plugins.budget"]["max_tokens_per_run"] == 123
    assert not (tmp_path / "cfg.v1.bak").exists(), "只读不应升级文件"
    assert p.read_text(encoding="utf-8").find('"version"') < 0, "读不应写回"

    st.patch_session("u:A", {"plugins.budget": {"max_tokens_per_run": 9}})
    assert (tmp_path / "cfg.v1.bak").exists(), "首次写 v2 前必须备份 v1"
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert not any(k.startswith("plugins.") for k in raw), "禁止双写平铺副本"
    assert raw["container"]["plugins.budget"]["max_tokens_per_run"] == 123
    assert raw["sessions"]["u:A"]["plugins.budget"]["max_tokens_per_run"] == 9

    st2 = ConfigStore(path=p)                                  # 回读一致
    assert st2.view("budget", "u:A")["max_tokens_per_run"] == 9
    assert st2.view("budget", "u:B")["max_tokens_per_run"] == 123   # 缺键 ⇒ 继承容器
    assert st2.view("budget")["max_tokens_per_run"] == 123


def test_t9b_unknown_version_parsed_as_v1_readonly(tmp_path):
    """未知 version ⇒ 按 v1 只读解析（绝不把脏数据当 v2 落盘）。"""
    p = tmp_path / "cfg2.json"
    p.write_text(json.dumps({"version": 99,
                             "plugins.budget": {"max_tokens_per_run": 7}}),
                 encoding="utf-8")
    st = ConfigStore(path=p)
    assert st.is_v1
    assert st.container()["plugins.budget"]["max_tokens_per_run"] == 7
    assert st.sessions() == {}


def test_t9c_corrupt_file_degrades_to_empty(tmp_path):
    p = tmp_path / "cfg3.json"
    p.write_text("{ this is not json", encoding="utf-8")
    st = ConfigStore(path=p)
    assert st.container() == {} and st.sessions() == {}


# ---------------------------------------------------------------------------
# T10：sessions LRU 上限（只淘汰覆盖，不动会话数据）
# ---------------------------------------------------------------------------

def test_t10_session_lru_eviction(tmp_path):
    st = ConfigStore(path=tmp_path / "cfg.json", session_limit=3)
    for i in range(5):
        st.patch_session(f"s{i}", {"plugins.budget": {"max_tokens_per_run": 1000 + i}})
    sess = st.sessions()
    assert len(sess) == 3, f"未按上限淘汰：{list(sess)}"
    assert "s0" not in sess and "s1" not in sess, "应淘汰最久未用者"
    assert "s4" in sess, "最新写入的会话不得被淘汰"
    assert st.view("budget", "s4")["max_tokens_per_run"] == 1004
    # 淘汰只删覆盖：容器覆盖与其它会话数据不受影响
    st.patch_container({"plugins.context": {"max_context_tokens": 12345}})
    assert st.view("context", "s4")["max_context_tokens"] == 12345


def test_t10b_pure_rules_touch_and_evict():
    sessions = {"a": {}, "b": {}, "c": {}}
    touch(sessions, "a")                                       # a 变为最近使用
    assert list(sessions) == ["b", "c", "a"]
    dropped = evict_lru(sessions, keep="a", limit=2)
    assert dropped == ["b"] and list(sessions) == ["c", "a"]
    assert evict_lru(sessions, keep="a", limit=0) == []        # 不限


def test_t10c_merge_overrides_inherits_not_replaces():
    container = {"plugins.context": {"max_context_tokens": 24000},
                 "plugins.budget": {"max_tokens_per_run": 100}}
    session = {"plugins.context": {"max_context_tokens": 8000}}
    merged = merge_overrides(container, session)
    assert section_of(merged, "context") == {"max_context_tokens": 8000}
    assert section_of(merged, "budget") == {"max_tokens_per_run": 100}, "未提的节应继承"
    # 原对象不被修改（视图返回副本）
    assert container["plugins.context"]["max_context_tokens"] == 24000


# ---------------------------------------------------------------------------
# S4：压缩阈值按会话取值
# ---------------------------------------------------------------------------

def test_s4_compactor_threshold_is_session_scoped(tmp_path):
    """``compact_trigger_tokens`` 经同一 cfg_view 接缝按会话取值；无视图 ⇒ 容器值。"""
    from agent.plugins.short_term.compactor import HistoryCompactor

    store = ConfigStore(path=tmp_path / "cfg.json")
    store.patch_session("u:A", {"plugins.short_term": {"compact_trigger_tokens": 300}})
    comp = HistoryCompactor(store=None, cfg={"compact_trigger_tokens": 12000})

    class _Ctx:
        state = {StateKeys.SESSION_KEY: "u:A"}

        def get(self, key, default=None):
            return store.view if key == SESSION_CFG_VIEW else default

    class _NoView:
        state = {StateKeys.SESSION_KEY: "u:A"}

        def get(self, key, default=None):
            return default

    assert comp._threshold(_Ctx(), "u:A") == 300, "会话覆盖未生效"
    assert comp._threshold(_Ctx(), "u:B") == 12000, "无覆盖会话应继承容器值"
    assert comp._threshold(_NoView(), "u:A") == 12000, "无接缝应退容器值（不劣化）"
    assert comp._threshold(None, None) == 12000, "无 ctx 时应退容器值"


# ---------------------------------------------------------------------------
# S5：设置表合并（单一真相源）与作用域白名单
# ---------------------------------------------------------------------------

def test_s5_setting_meta_is_single_source():
    """三张派生视图与 ``_SETTING_META`` 严格一致；容器级键不得出现在可会话化名单。"""
    from agent.run import chat as srv

    assert set(srv._SETTING_SCHEMA) == set(srv._SETTING_META)
    assert set(srv._PLUGIN_OF_KEY) == set(srv._SETTING_META)
    assert set(srv._SETTING_DEFAULTS) == set(srv._SETTING_META)
    for k, meta in srv._SETTING_META.items():
        assert srv._SETTING_SCHEMA[k] == meta["spec"]
        assert srv._PLUGIN_OF_KEY[k] == meta["plugin"]
        assert srv._SETTING_DEFAULTS[k] == meta["default"]
        assert meta.get("scope") in ("session", "container")
        srv._coerce_setting(k, meta["default"])                # 默认值必须自洽

    # 会话级：窗口/配比/层 cap/治理动词/预算额度/压缩阈值
    for k in ("max_context_tokens", "memory_ratio", "resident_cap_tokens",
              "compact_mode", "compact_trigger_tokens", "max_tokens_per_run",
              "finalize_reserve_tokens"):
        assert k in srv._SETTING_SCOPABLE, f"{k} 应为会话级"
    # 容器级：校准（模型属性）、装配开关、审批通道
    for k in ("cost_calibration", "lazy_tools", "tool_domains", "tool_domains_enabled",
              "alignment_enabled", "provider", "timeout_s", "ask"):
        assert k not in srv._SETTING_SCOPABLE, f"{k} 不应可会话化"
