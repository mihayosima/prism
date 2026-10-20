"""工具税「域级裁剪」端到端验收（P5）。

依据：``docs/reports/context/工具税域级裁剪实施方案-2026-09-30.md`` §6/§10、
上游 ``docs/context/工具税域级治理方案-2026-09-30.md``。

与 ``test_followup_topics.py`` 里的纯函数单测**分层**：本文件在**真实容器内真跑
``ctx.react.run(...)``**，用 ``_SpyReasoner`` 替换推理接缝（**不调 LLM、完全离线**），
从而对「每一步实际喂给模型的 native tools 与 system 文本」做**确定性断言**。

覆盖判据：
- G1 默认态零变化（总开关关 ⇒ 不裁剪、无域清单）
- G2 裁剪生效（隐藏域不出现在 native tools；放行域在；元工具恒在）
- G3 隐藏但可检索（隐藏域工具不可见 → ``tool_load`` → **下一步**拿到完整定义；
      ``tool_search`` 仍取全量目录）
- G4 可发现性（系统提示出现「其它工具域」清单，且**名册本身也被裁**）
- G5 正交性（plan 的 ``risk`` 裁剪先做，域裁剪叠加在其后）
- G6 非懒加载安全（``lazy_tools=False`` 时配了白名单也自动禁用，无死锁）
- 安全阀（白名单拼错 ⇒ 回退为不裁剪）
- G7 设置页生效链路（覆盖文件 → ``_merge_cfg`` → 容器 config → ``allowed_domains``）
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
from pathlib import Path

from agent.api import StateKeys
from agent.core.request_cost import estimate_request_cost
from agent.plugins.react import React
from agent.plugins.react.components import ReasonOutput, ToolCall
from agent.plugins.react.loop import ReactLoop, allowed_domains
from agent.profiles import build_agent
from agent.run import chat as srv

# --------------------------------------------------------------------------
# 探测工具：两个域 + 一个只读工具（供 G5 的 plan 裁剪用）
# --------------------------------------------------------------------------
PROBE_TOOLS = [
    # (name, owner, risk, description, params)
    ("alpha_ping", "dom.alpha", "read", "ping the alpha service", {}),
    ("alpha_echo", "dom.alpha", "write", "echo back a message", {"msg": {"type": "str"}}),
    ("alpha_time", "dom.alpha", "write", "return current time", {}),
    ("beta_read_file", "dom.beta", "write",
     "读取本地文件的文本内容（local file read）", {"path": {"type": "str"}}),
    ("beta_list_dir", "dom.beta", "write", "列出目录内容", {"path": {"type": "str"}}),
    ("beta_write_file", "dom.beta", "write", "写入本地文件", {"path": {"type": "str"}}),
]


def _handler(text: str):
    def h(**_kw):
        return text
    return h


class _SpyReasoner:
    """记录每步窗口（tools + system），再按脚本吐工具调用或最终答复；不触网。"""

    def __init__(self, script: list | None = None):
        self.script = script or [None]     # 每步预设的 ToolCall 列表；None = 直接收尾
        self.seen: list[dict] = []

    async def reason(self, ctx, messages, tools):
        self.seen.append({
            "step": len(self.seen) + 1,
            "names": [t.get("name") for t in (tools or [])],
            "tools": tools or [],
            "system": next((m.get("content") for m in messages
                            if m.get("role") == "system"
                            and isinstance(m.get("content"), str)), ""),
            "est": estimate_request_cost(messages, tools),
        })
        calls = self.script[min(len(self.seen) - 1, len(self.script) - 1)]
        if not calls:
            return ReasonOutput(text="（脚本：收尾）")
        return ReasonOutput(text="", tool_calls=list(calls))


def _run(coro):
    return asyncio.run(coro)


def _drive(tmp_path: Path, *, lazy: bool, enabled: bool, domains, spy: _SpyReasoner,
           prompt: str = "（脚本驱动）", plan: bool = False):
    """装配隔离容器 → 注册探测工具 → 跑一次 react run（全程离线）。"""
    cfg = {"db_path": str(tmp_path / "e2e.db"),
           "agent": {"react": {"lazy_tools": lazy, "tool_domains_enabled": enabled,
                               "tool_domains": domains}}}

    async def s():
        c = build_agent(config=cfg, extra_plugins=(React,),
                        builtin_data={"react.reasoner": spy})
        await c.assemble()
        await c.start()
        root = c.root
        for name, owner, risk, desc, params in PROBE_TOOLS:
            root.tasks.register(root, name,
                                schema={k: {**v, "required": True} for k, v in params.items()},
                                handler=_handler(f"[{name} 执行结果]"), owner=owner,
                                description=desc, risk=risk)
        if plan:
            root.state[StateKeys.PERMISSION_MODE] = "plan"
        try:
            return await root.react.run(root, prompt, session_id="e2e")
        finally:
            await c.teardown()

    return _run(s())


# --------------------------------------------------------------------------- G1
def test_g1_default_off_is_transparent(tmp_path):
    """G1：总开关关（默认）⇒ 不裁剪、无域清单。"""
    spy = _SpyReasoner()
    _drive(tmp_path, lazy=True, enabled=False, domains=["dom.alpha"], spy=spy)
    s = spy.seen[0]
    assert {"alpha_ping", "beta_read_file", "beta_list_dir", "tool_search",
            "tool_load", "wm_read", "agent.ping"} <= set(s["names"])
    assert "其它工具域" not in s["system"], "总开关关 ⇒ 系统提示不得出现域清单"


# --------------------------------------------------------------------------- G2/G4
def test_g2_g4_clip_takes_effect_and_stays_discoverable(tmp_path):
    """G2 裁剪生效 + G4 可发现性：两处（native + 系统提示目录）都要裁。"""
    spy = _SpyReasoner()
    _drive(tmp_path, lazy=True, enabled=True, domains=["dom.alpha"], spy=spy)
    s = spy.seen[0]
    assert not [n for n in s["names"] if n.startswith("beta_")], "隐藏域不得出现"
    assert {"alpha_ping", "alpha_echo", "alpha_time"} <= set(s["names"])
    assert {"tool_search", "tool_load", "wm_read"} <= set(s["names"]), "检索通道恒在"
    assert "其它工具域（当前未展开，可用 tool_search 检索）" in s["system"]
    assert "- dom.beta（3 个）" in s["system"], "域清单须给出域名 + 数量"
    assert "dom.alpha（" not in s["system"], "已放行的域不进域清单"
    assert "beta_read_file" not in s["system"], "只裁 native 不够——名册也必须裁"


# --------------------------------------------------------------------------- G3
def test_g3_hidden_but_searchable(tmp_path):
    """G3：隐藏域工具经 tool_load 后，**下一步** native 拿到完整定义（跨域放行）。"""
    spy = _SpyReasoner(script=[
        [ToolCall(name="tool_load", arguments={"names": "beta_read_file"}, id="c1")],
        None,
    ])
    _drive(tmp_path, lazy=True, enabled=True, domains=["dom.alpha"], spy=spy)
    first, second = spy.seen[0], spy.seen[1]
    assert "beta_read_file" not in first["names"]
    assert "beta_read_file" in second["names"], "已加载 ⇒ 跨域放行，无需另写域解禁逻辑"
    got = next(t for t in second["tools"] if t.get("name") == "beta_read_file")
    assert got.get("params") == {"path": {"type": "str"}}, "必须是完整定义而非目录卡"


def test_g3_tool_search_uses_full_catalog(tmp_path):
    """G3（检索通道）：``_tool_search`` 取全量目录 ⇒ 隐藏域工具仍可被搜到。"""
    cat = [{"name": n, "description": d, "params": {}, "required": [], "risk": r, "owner": o}
           for n, o, r, d, _p in PROBE_TOOLS]
    hit = ReactLoop._tool_search(
        ToolCall(name="tool_search", arguments={"keyword": "file"}, id="c9"), cat).content
    assert "beta_read_file" in hit
    # 契约守卫：_partition_meta 必须拿到全量 tools（而非已裁剪的 tools_step）
    src = inspect.getsource(ReactLoop._partition_meta)
    assert "tools)" in src and "tools_step" not in src, \
        "隐藏域一旦同时从检索目录消失，工具将永远找不回来（死锁）"


# --------------------------------------------------------------------------- G5
def test_g5_plan_risk_clip_then_domain_clip(tmp_path):
    """G5：顺序锁定为「risk 裁剪（visible_tools）→ 域裁剪（_tools_for_step）」。"""
    spy = _SpyReasoner()
    _drive(tmp_path, lazy=True, enabled=True, domains=["dom.alpha"], spy=spy, plan=True)
    names = set(spy.seen[0]["names"])
    assert "alpha_ping" in names, "只读工具在 plan 下仍在（且属放行域）"
    assert "alpha_echo" not in names and "alpha_time" not in names, "write 工具被 risk 裁掉"
    assert not [n for n in names if n.startswith("beta_")], "域裁剪叠加在其后"


# --------------------------------------------------------------------------- G6
def test_g6_disabled_automatically_without_lazy(tmp_path):
    """G6：非懒加载没有检索通道 ⇒ 配了白名单也必须自动禁用（否则＝硬隐藏＝死锁）。"""
    spy = _SpyReasoner()
    _drive(tmp_path, lazy=False, enabled=True, domains=["dom.alpha"], spy=spy)
    s = spy.seen[0]
    assert {"alpha_ping", "beta_read_file", "beta_list_dir"} <= set(s["names"])
    assert "其它工具域" not in s["system"]


# --------------------------------------------------------------------------- 安全阀
def test_typo_whitelist_falls_back_to_no_clip(tmp_path):
    """安全阀：白名单与任何已注册域都不相交（拼错）⇒ 回退不裁剪，不静默全隐藏。"""
    spy = _SpyReasoner()
    _drive(tmp_path, lazy=True, enabled=True, domains=["dom.tpyo"], spy=spy)
    names = set(spy.seen[0]["names"])
    assert {"alpha_ping", "beta_read_file"}.issubset(names), "不得发生「全隐藏」"
    assert {"tool_search", "tool_load", "wm_read"} <= names


# --------------------------------------------------------------------------- G7
def test_g7_settings_round_trip(tmp_path, monkeypatch):
    """G7：覆盖文件 → ``_merge_cfg`` → 容器 config → ``allowed_domains`` 全链打通。

    注意：真实覆盖文件 ``data/agent_plugin_config.json`` 是**扁平点号键**
    （``{"plugins.react": {...}}``），不是嵌套结构——本用例固化这一点。
    """
    monkeypatch.setattr(srv, "_OVERRIDE_FILE", tmp_path / "cfg.json")
    srv._OVERRIDE_FILE.write_text(json.dumps({
        "plugins.react": {"lazy_tools": True, "tool_domains_enabled": True,
                          "tool_domains": ["dom.alpha"]}}, ensure_ascii=False),
        encoding="utf-8")
    args = argparse.Namespace(db=str(tmp_path / "g7.db"), provider="deepseek",
                              model="deepseek-flash", fixture=False)
    cfg = srv._merge_cfg(args)
    node = ((cfg.get("agent") or {}).get("react") or {})
    assert node.get("tool_domains_enabled") is True
    assert node.get("tool_domains") == ["dom.alpha"]

    class _C:
        config = cfg

    assert allowed_domains(_C()) == {"dom.alpha"}

    # 默认值语义 + 陷阱回归
    assert srv._SETTING_DEFAULTS["tool_domains"] == [], "_SETTING_DEFAULTS 不可给 None"
    assert srv._SETTING_DEFAULTS["tool_domains_enabled"] is False
    assert srv._coerce_setting("tool_domains", "plugins.windows_mcp, agent.tasks") == \
        ["plugins.windows_mcp", "agent.tasks"]
    # 写回语义：插件节点是**合并**（不清同节点其它键）；reset 才整节点 pop
    src = inspect.getsource(srv)
    assert "cur = dict(ov.get(pid, {}))" in src and "cur.update(b)" in src
    assert "ov.pop(pid, None)" in src
