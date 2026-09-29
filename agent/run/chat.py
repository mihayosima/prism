"""agent.run.chat —— Agent 通用对话外设（web + 无头）。

前端参考 agent 对话形态（step/tool 轨迹、事件/权限/记忆/工具视图），**不特化为任何领域**：
通用能力为 ping / tool_call / chat；领域能力经「已启用的工具插件」（如 windows-mcp）挂入工具目录。

用法：
    python -m agent.run.chat                    # 网页（默认 127.0.0.1:8720，deepseek）
    python -m agent.run.chat --fixture           # 离线夹具（不动真 API）
    python -m agent.run.chat --once "你好"        # 无头跑一次
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path

from agent.api import AgentError, Services, StateKeys
from agent.profiles import BASE_AGENT_CONFIG, build_agent
from agent.sys.progress import tap_progress


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--provider", default="deepseek", help="llm provider（deepseek | fixture）")
    p.add_argument("--model", default="deepseek-flash",
                   help="deepseek 模型名（线上支持 deepseek-flash / deepseek-v4-pro）")
    p.add_argument("--fixture", action="store_true", help="强制离线夹具（不触真 API）")
    p.add_argument("--db", default="data/agent_chat.db")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8720)
    p.add_argument("--once", default=None, help="无头模式：跑一条消息后退出并打印")
    return p.parse_args(argv)


def _cfg(args: argparse.Namespace) -> dict:
    provider = "fixture" if args.fixture else args.provider
    if provider == "fixture":
        return {"db_path": args.db, "agent": {"llm": {"provider": "fixture"}}}
    return {"db_path": args.db,
            "agent": {"llm": {"provider": provider, "model": args.model}}}


# ---- 插件配置覆盖（前端「设置」持久化；与插件启停期望态分离，单文件存储） ----
_OVERRIDE_FILE = Path("data/agent_plugin_config.json")


def _load_overrides() -> dict:
    """读取用户对插件运行时配置的覆盖（data/agent_plugin_config.json）。

    形态：``{ "plugins.<名>": {键: 值, ...}, ... }``；缺失/损坏返回空 dict（不裸崩）。
    """
    if not _OVERRIDE_FILE.exists():
        return {}
    try:
        return json.loads(_OVERRIDE_FILE.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 —— 损坏按空处理，不阻断启动
        return {}


def _save_overrides(data: dict) -> None:
    _OVERRIDE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _OVERRIDE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_OVERRIDE_FILE)


def _merge_cfg(args: argparse.Namespace) -> dict:
    """基础 config（_cfg）+ 用户覆盖，注入 build_agent。

    覆盖按 ``plugins.<名>`` → ``agent.<名>`` 映射并入对应插件配置节（均为 _any，免改 schema）。
    """
    cfg = _cfg(args)
    ov = _load_overrides()
    agent = cfg.setdefault("agent", {})
    for pid, pconf in (ov or {}).items():
        if not isinstance(pid, str) or not pid.startswith("plugins."):
            continue
        if not isinstance(pconf, dict):
            continue
        base = pid[len("plugins."):]
        node = agent.setdefault(base, {})
        node.update(pconf)
    return cfg


# ---- 上下文管理设置项 schema（前端「设置 → 上下文管理」可调） ----
# 校验/钳制：键 → (类型, 下限, 上限)；归属插件由 _PLUGIN_OF_KEY 路由。
_SETTING_SCHEMA = {
    "compact_trigger_tokens": (int, 256, 1_000_000),      # 来自 plugins.short_term
    "max_context_tokens": (int, 1024, 1_000_000),
    "reserve_output_tokens": (int, 0, 1_000_000),
    "memory_ratio": (float, 0.01, 0.99),
    "history_ratio": (float, 0.01, 0.99),
    "source_clip_tokens": (int, 0, 1_000_000),
    # ---- S3：循环内（react）上下文治理 ----
    "clip_max_tokens": (int, 0, 1_000_000),            # 单条观察超此值即首尾折叠
    "clip_head_tokens": (int, 0, 1_000_000),           # 折叠后头部保留
    "clip_tail_tokens": (int, 0, 1_000_000),           # 折叠后尾部保留
    "react_keep_recent": (int, 0, 1000),                  # 最近 N 条链原文保留
    "react_min_chain_tokens": (int, 0, 1_000_000),        # 循环内 chain 保底额度
    # ---- G2.0（P1）：双窗口解耦 + 水位 + 层 cap ----
    "working_window_tokens": (int, 1, 1_000_000),          # 经济窗口（水位分母）
    "window_high_ratio": (float, 0.01, 0.99),              # 触发水位
    "window_low_ratio": (float, 0.01, 0.99),               # 回落目标（记录用）
    "resident_cap_tokens": (int, 0, 1_000_000),            # 常驻层 cap（0=无）
    "on_demand_cap_tokens": (int, 0, 1_000_000),           # 按需层 cap（0=无）
    "prompt_cap_tokens": (int, 0, 1_000_000),              # 输入层 cap（0=无）
    # ---- G2.0（P2）：治理动词（模式 + clear 最小释放量）----
    "compact_mode": ("choice", ["off", "shadow", "on"]),   # 默认 shadow（只算不做）
    "clear_at_least_tokens": (int, 0, 1_000_000),          # 单次清除的最小释放量
    # ---- 审批插件（plugins.approval）：通道 / 超时 / 必问名单 ----
    "provider": ("choice", ["auto-deny", "auto-allow", "web", "cli"]),
    "timeout_s": (int, 1, 3600),
    "ask": ("list",),                                      # fnmatch 工具名名单
    # ---- 成本护栏（agent.budget 内核托管节；经 plugins.budget 覆盖映射，见 _merge_cfg）----
    "max_tokens_per_run": (int, 1000, 10_000_000),         # 容器生命周期累计 LLM token 上限
    "finalize_reserve_tokens": (int, 0, 1_000_000),        # 方案A：收尾预留额度（收尾专用）
    "cost_calibration": ("bool",),                        # F1：口径自校准（EWMA 因子）
    # ---- 工具懒加载（plugins.react；L1 灰度开关）----
    "lazy_tools": ("bool",),
    # ---- P0-2：续接前缀对齐（plugins.react；头部快照冻结，可回退）----
    "alignment_enabled": ("bool",),
}
_PLUGIN_OF_KEY = {
    "compact_trigger_tokens": "plugins.short_term",
    "max_context_tokens": "plugins.context",
    "reserve_output_tokens": "plugins.context",
    "memory_ratio": "plugins.context",
    "history_ratio": "plugins.context",
    "source_clip_tokens": "plugins.context",
    "clip_max_tokens": "plugins.context",
    "clip_head_tokens": "plugins.context",
    "clip_tail_tokens": "plugins.context",
    "react_keep_recent": "plugins.context",
    "react_min_chain_tokens": "plugins.context",
    "working_window_tokens": "plugins.context",
    "window_high_ratio": "plugins.context",
    "window_low_ratio": "plugins.context",
    "resident_cap_tokens": "plugins.context",
    "on_demand_cap_tokens": "plugins.context",
    "prompt_cap_tokens": "plugins.context",
    "compact_mode": "plugins.context",
    "clear_at_least_tokens": "plugins.context",
    "provider": "plugins.approval",
    "timeout_s": "plugins.approval",
    "ask": "plugins.approval",
    "max_tokens_per_run": "plugins.budget",
    "finalize_reserve_tokens": "plugins.budget",
    "cost_calibration": "plugins.budget",
    "lazy_tools": "plugins.react",
    "alignment_enabled": "plugins.react",
}
# G2.0（P1）：context 节默认值单一真相源（与 profiles.BASE_AGENT_CONFIG 同源，避免漂移）
_CTX_DEFAULTS = ((BASE_AGENT_CONFIG.get("agent") or {}).get("context") or {})
_SETTING_DEFAULTS = {
    "compact_trigger_tokens": 12000,
    "max_context_tokens": 24000,
    "reserve_output_tokens": 4000,
    "memory_ratio": 0.10,
    "history_ratio": 0.60,
    "source_clip_tokens": 8192,
    "clip_max_tokens": 1000,
    "clip_head_tokens": 500,
    "clip_tail_tokens": 500,
    "react_keep_recent": 6,
    "react_min_chain_tokens": 2048,
    # G2.0（P1）：与 profiles.BASE_AGENT_CONFIG.agent.context 同源
    "working_window_tokens": _CTX_DEFAULTS.get("working_window_tokens", 96000),
    "window_high_ratio": _CTX_DEFAULTS.get("window_high_ratio", 0.70),
    "window_low_ratio": _CTX_DEFAULTS.get("window_low_ratio", 0.40),
    "resident_cap_tokens": _CTX_DEFAULTS.get("resident_cap_tokens", 8000),
    "on_demand_cap_tokens": _CTX_DEFAULTS.get("on_demand_cap_tokens", 8000),
    "prompt_cap_tokens": _CTX_DEFAULTS.get("prompt_cap_tokens", 2000),
    # G2.0（P2）：与 profiles.BASE_AGENT_CONFIG.agent.context 同源
    "compact_mode": _CTX_DEFAULTS.get("compact_mode", "shadow"),
    "clear_at_least_tokens": _CTX_DEFAULTS.get("clear_at_least_tokens", 4000),
    "provider": "web",
    "timeout_s": 30,
    "ask": [],
    # 与 profiles.BASE_AGENT_CONFIG 的 budget 默认同源（避免两处硬编码漂移）
    "max_tokens_per_run": (((BASE_AGENT_CONFIG.get("agent") or {}).get("budget") or {})
                           .get("max_tokens_per_run", 200000)),
    "finalize_reserve_tokens": (((BASE_AGENT_CONFIG.get("agent") or {}).get("budget") or {})
                                .get("finalize_reserve_tokens", 0)),
    "cost_calibration": (((BASE_AGENT_CONFIG.get("agent") or {}).get("budget") or {})
                         .get("cost_calibration", True)),
    "lazy_tools": (((BASE_AGENT_CONFIG.get("agent") or {}).get("react") or {})
                   .get("lazy_tools", False)),
    # P0-2：与 profiles.BASE_AGENT_CONFIG agent.react 同源；默认开启
    "alignment_enabled": (((BASE_AGENT_CONFIG.get("agent") or {}).get("react") or {})
                          .get("alignment_enabled", True)),
}


def _coerce_setting(key: str, raw):
    """按 schema 校验设置项：数字钳制 / 枚举校验 / 列表规整。"""
    if key not in _SETTING_SCHEMA:
        raise ValueError(f"未知设置项: {key}")
    spec = _SETTING_SCHEMA[key]
    kind = spec[0]
    if kind == "bool":                                     # 开关：on/true/1/是/开 → True
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().lower() in ("1", "true", "on", "yes", "是", "开")
    if kind == "choice":                                   # 枚举：provider 通道名
        val = str(raw or "").strip()
        if val not in spec[1]:
            raise ValueError(f"{key} 须 ∈ {spec[1]}")
        return val
    if kind == "list":                                     # 列表：ask 名单（fnmatch）
        if isinstance(raw, str):
            parts = raw.replace("，", ",").replace("、", ",").split(",")
        elif isinstance(raw, (list, tuple)):
            parts = list(raw)
        else:
            parts = [raw]
        return [str(x).strip() for x in parts if str(x).strip()]
    typ, lo, hi = spec
    val = typ(raw)
    if val < lo or val > hi:
        raise ValueError(f"{key} 须落在 [{lo}, {hi}]")
    return val


async def _boot(args: argparse.Namespace):
    from agent.sys.plugin_manager import PluginManager

    pm = PluginManager(plugin_dir="agent/plugins", state_file="data/agent_plugin_state.json")
    c = build_agent(config=_merge_cfg(args), plugin_mgr=pm)
    await c.assemble()
    await c.start()
    return c, pm


# ---------------------------------------------------------------------------
# 无头模式
# ---------------------------------------------------------------------------

async def _once(args: argparse.Namespace) -> int:
    c, _pm = await _boot(args)
    try:
        reply = await c.root.chat.send(c.root, args.once)
        print("\n" + ("=" * 30) + "\n" + reply.text)
        print(f"\n[步骤] intent={reply.plan.get('intent')} ok={reply.ok}"
              f" tokens={reply.tokens_in}in/{reply.tokens_out}out")
        if reply.error:
            print(f"[error] {reply.error}")
        print("=" * 30)
        return 0 if reply.ok else 1
    finally:
        await c.teardown()


# ---------------------------------------------------------------------------
# Web 形态（dsh 风格通用对话面）
# ---------------------------------------------------------------------------

_PAGE = r"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>prism</title>
<style>
:root{
  --brand:#4176e6; --brand2:#5686fe; --brand-soft:#edf3ff; --brand-ink:#2b53b8;
  --bg:#f5f6f8; --card:#fff; --line:#e2e6ee; --line2:#eef1f6;
  --ink:#111827; --muted:#6b7280; --muted2:#9aa1ad;
  --dark:#151517; --dark2:#202227; --dark-ink:#e2e8f0;
  --ok:#22c55e; --ok-soft:#e9f9ef; --warn:#f7ad31; --warn-soft:#fff4e0; --err:#ef4444; --err-soft:#fdecec;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --r:10px; --r2:6px;
}
*{box-sizing:border-box}
html,body{height:100%;margin:0}
body{font:14px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:var(--bg);color:var(--ink)}
.top{display:flex;align-items:center;gap:14px;padding:0 20px;height:56px;background:var(--card);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:5}
.brand{display:flex;align-items:center;gap:9px;font-weight:700;font-size:15px}
.brand .mark{width:26px;height:26px;border-radius:7px;background:linear-gradient(135deg,var(--brand),var(--brand2));color:#fff;display:grid;place-items:center;font-size:13px;font-weight:800}
.mono{font-family:var(--mono);font-size:11.5px;color:var(--muted)}
.top .spacer{flex:1}
.pill{background:var(--brand-soft);color:var(--brand-ink);border:1px solid #d7e5ff;border-radius:999px;padding:4px 12px;font-size:12px;font-family:var(--mono)}
.wrap{display:flex;height:calc(100vh - 56px)}
.nav{width:190px;border-right:1px solid var(--line);background:var(--card);padding:12px 10px;display:flex;flex-direction:column;gap:2px}
.nav h6{font-size:10.5px;color:var(--muted2);font-weight:600;letter-spacing:.04em;margin:10px 8px 4px}
.nav-item{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:8px;color:var(--ink);cursor:pointer;font-size:13px;border:0;background:none;text-align:left;width:100%}
.nav-item:hover{background:var(--bg)}
.nav-item.active{background:var(--brand);color:#fff}
.nav-item .ic{width:16px;height:16px;opacity:.85}
.nav-foot{margin-top:auto;padding:12px 10px;font-size:11px;color:var(--muted2);white-space:pre-wrap;font-family:var(--mono)}
.main{flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden}
section[data-view]{display:none;flex:1;overflow:auto;min-height:0}
section[data-view].active{display:flex;flex-direction:column}
/* ------- chat ------- */
.chat{max-width:920px;width:100%;margin:0 auto;flex:1;display:flex;flex-direction:column;padding:18px 20px 0}
.messages{flex:1;overflow:auto;display:flex;flex-direction:column;gap:16px;padding:4px 2px 18px}
.msg{max-width:82%}
.msg.user{align-self:flex-end}
.msg.user .bubble{background:var(--brand);color:#fff;border-radius:14px 14px 4px 14px;padding:10px 14px}
.msg.agent{align-self:flex-start;width:100%}
.msg .who{font-size:11px;color:var(--muted);margin-bottom:5px}
.msg.user .who{text-align:right}
.text{background:var(--card);border:1px solid var(--line);border-radius:4px 14px 14px 14px;padding:12px 14px;white-space:normal;line-height:1.65;font-size:13.5px}
.text pre{display:inline-block;background:var(--dark);color:var(--dark-ink);border-radius:6px;padding:2px 7px;font-family:var(--mono);font-size:12px}
.text code{background:var(--brand-soft);color:var(--brand-ink);border-radius:4px;padding:1px 5px;font-family:var(--mono);font-size:12px}
.text b{font-weight:600}
.msg.user .text{display:none}
/* step trace（tool calls，dsh 签名） */
.steps{margin-top:8px;border:1px solid var(--line);border-radius:var(--r);background:var(--card);overflow:hidden}
.step{border-bottom:1px solid var(--line);font-size:12.5px}
.step:last-child{border-bottom:0}
.step .top{display:flex;align-items:center;gap:8px;padding:7px 12px;height:auto;background:var(--card);border:0}
.step .name{font-family:var(--mono);color:var(--ink)}
.step .dot{width:7px;height:7px;border-radius:50%}
.step .dot.ok{background:var(--ok)} .step .dot.err{background:var(--err)} .step .dot.run{background:var(--warn)}
.step details{margin:0 12px 8px}
.step summary{font-size:11.5px;color:var(--muted);cursor:pointer}
.step pre{margin:6px 0 0;background:#0d0f13;color:#c9d6ec;padding:10px 12px;border-radius:8px;overflow:auto;font-family:var(--mono);font-size:11.5px;white-space:pre-wrap}
.chip{background:var(--card);border:1px solid var(--line);color:var(--ink);border-radius:999px;padding:6px 13px;font-size:12.5px;cursor:pointer;transition:.12s}
.chip:hover{border-color:var(--brand2);color:var(--brand-ink);background:var(--brand-soft)}
.inputrow{padding:0 2px 18px;display:flex;gap:10px;align-items:flex-end}
textarea{flex:1;font:inherit;border:1px solid var(--line);border-radius:12px;padding:11px 13px;resize:none;min-height:48px;max-height:160px;outline:none}
textarea:focus{border-color:var(--brand2)}
.btn{background:var(--brand);color:#fff;border:0;padding:11px 20px;border-radius:12px;cursor:pointer;font-size:13.5px;font-weight:600}
.btn:hover{background:var(--brand-ink)}
.btn:disabled{opacity:.55;cursor:not-allowed}
/* ------- generic views ------- */
.panel{max-width:960px;width:100%;margin:0 auto;padding:18px 20px}
h3{font-size:13.5px;margin:0 0 12px;color:var(--ink)}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:var(--r);overflow:hidden;border:1px solid var(--line)}
th,td{padding:8px 11px;border-bottom:1px solid var(--line2);text-align:left;font-size:12.5px;vertical-align:top}
th{background:#f8fafc;color:var(--muted);font-weight:600}
tr:last-child td{border-bottom:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:13px 15px;margin-bottom:8px}
.card b{font-weight:600}
.tag{font-size:11px;border-radius:6px;padding:2px 8px;font-family:var(--mono)}
.tag.ok{background:var(--ok-soft);color:#188a46} .tag.err{background:var(--err-soft);color:var(--err)}
.tag.run{background:var(--warn-soft);color:#a16207} .tag.blue{background:var(--brand-soft);color:var(--brand-ink)}
.tag.off{background:var(--bg);color:var(--muted2);border:1px solid var(--line)}
.muted{color:var(--muted)} .e{color:var(--err)}
.empty{color:var(--muted2);font-size:12.5px}
.filters{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px}
.filters .chip.active{background:var(--brand);color:#fff;border-color:var(--brand)}
pre.json{background:#0d0f13;color:#c9d6ec;padding:10px 12px;border-radius:8px;overflow:auto;font-family:var(--mono);font-size:11.5px}
/* ------- human-in-the-loop cards（workbuddy 风格：内嵌对话流、扁平白卡、无阴影） ------- */
/* 权限档位：折叠式收纳在输入框上方（默认只留一行摘要，选完自动收起） */
.modebar{margin:0 2px 8px}
.modehd{display:flex;align-items:center;gap:7px;width:100%;background:none;border:0;padding:3px 2px;
  cursor:pointer;text-align:left;font:inherit;color:var(--muted2);font-size:11.5px;border-radius:8px}
.modehd:hover{color:var(--ink)}
.modebar .modecur{color:var(--ink);font-weight:500}
.modebar .modecur.danger{color:var(--err)}
.modecv{font-size:10px;color:var(--muted2);transition:transform .15s}
.modebar.open .modecv{transform:rotate(180deg)}
.modebd{display:none;flex-wrap:wrap;gap:6px;align-items:center;padding:7px 0 1px}
.modebar.open .modebd{display:flex}
.modebar .mlabel{font-size:11.5px;color:var(--muted2)}
.modebar .chip{cursor:pointer;user-select:none}
.modebar .chip.active{background:var(--brand);color:#fff;border-color:var(--brand)}
.modebar .chip[data-mode="bypass"].active{background:var(--err);border-color:var(--err)}
.modebar .chip[data-mode="bypass"]:not(.active){color:var(--err);border-color:#f3c1c1}
.hitcard{width:100%;background:var(--card);border:.5px solid var(--line);border-radius:12px;padding:14px 16px}
.hithd{display:flex;align-items:center;gap:10px;margin-bottom:10px}
.hitic{width:34px;height:34px;border-radius:9px;display:grid;place-items:center;font-size:12px;font-weight:500;flex:none}
.hitic.approval{background:var(--warn-soft);color:#a16207}
.hitic.ask{background:var(--brand-soft);color:var(--brand-ink)}
.hittt{flex:1;min-width:0}
.hittt b{display:block;font-size:13.5px;font-weight:500}
.hittt span{font-size:11.5px;color:var(--muted)}
.hitbd{font-size:13px;line-height:1.6;margin:0 0 10px}
.hitmeta{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.hitkw{margin:0 0 10px;background:#0d0f13;color:#c9d6ec;padding:10px 12px;border-radius:8px;overflow:auto;font-family:var(--mono);font-size:11.5px;white-space:pre-wrap}
.hitops{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px}
.hitops .chip.selected{background:var(--brand);color:#fff;border-color:var(--brand)}
.hitgrp{margin:0 0 10px}
.hitgrp .gtt{font-size:11.5px;color:var(--muted2);margin:0 0 6px}
.hitopt{display:flex;gap:10px;align-items:flex-start;padding:9px 12px;margin-bottom:6px;background:var(--card);border:.5px solid var(--line);border-radius:9px;cursor:pointer;transition:.12s}
.hitopt:last-child{margin-bottom:0}
.hitopt:hover{border-color:var(--brand2);background:var(--brand-soft)}
.hitopt.selected{border-color:var(--brand);background:var(--brand-soft)}
.hitopt .mk{width:15px;height:15px;flex:none;margin-top:1px;border:1px solid #c8cfdb;border-radius:50%;background:var(--card)}
.hitopt.multi .mk{border-radius:4px}
.hitopt.selected .mk{border-color:var(--brand);background:var(--brand);box-shadow:inset 0 0 0 2.5px var(--card)}
.hitopt .tt{flex:1;min-width:0}
.hitopt .tt b{display:block;font-size:13px;font-weight:500}
.hitopt .tt em{display:block;font-style:normal;font-size:11.5px;color:var(--muted);line-height:1.5;margin-top:2px}
.hitsub{margin:2px 0 12px}
.hitcard.done .hitopt,.hitcard.done .hitops .chip,.hitcard.done .hitinput{cursor:default;opacity:.62;pointer-events:none}   /* 了结后选项只留痕、不再可点 */
.hitrow{display:flex;gap:8px;align-items:center}
.hitinput{flex:1;font:inherit;font-size:13px;border:1px solid var(--line);border-radius:8px;padding:8px 10px;outline:none;min-width:120px}
.hitinput:focus{border-color:var(--brand2)}
.hitft{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.hitft .btn{padding:7px 16px;font-size:12.5px;border-radius:8px}
.hitft .btn.ghost2{background:var(--card);color:var(--brand-ink);border:1px solid #d7e5ff}
.hitft .btn.ghost2:hover{background:var(--brand-soft)}
.hitft .btn.danger{background:var(--card);color:var(--err);border:1px solid #f3c1c1}
.hitft .btn.danger:hover{background:var(--err-soft)}
.hitst{font-size:12px;color:var(--muted)}
</style></head><body>
<div class="top">
  <div class="brand"><div class="mark">◈</div>prism</div>
  <span class="mono" id="session"></span>
  <div class="spacer"></div>
  <span class="pill" id="model">…</span>
  <button class="chip" id="newSession" style="margin-left:10px">新会话</button>
</div>
<div class="wrap">
<nav class="nav">
  <h6>工作台</h6>
  <button class="nav-item active" data-view="chat"><span class="ic">💬</span>会话</button>
  <h6>机器</h6>
  <button class="nav-item" data-view="events"><span class="ic">📡</span>事件</button>
  <button class="nav-item" data-view="perms"><span class="ic">🔐</span>权限</button>
  <button class="nav-item" data-view="plugins"><span class="ic">🔌</span>插件</button>
  <button class="nav-item" data-view="memory"><span class="ic">🧠</span>记忆</button>
  <button class="nav-item" data-view="tools"><span class="ic">🛠️</span>工具</button>
  <button class="nav-item" data-view="settings"><span class="ic">⚙️</span>设置</button>
  <div class="nav-foot" id="machine">…</div>
</nav>
<div class="main">
  <section data-view="chat" class="active">
    <div class="chat">
      <div class="messages" id="messages"></div>
      <div class="modebar" id="modebar">
        <button class="modehd" id="modehd" type="button" aria-expanded="false" title="点击展开/收起权限档位">
          <span class="mlabel">权限档位</span>
          <span class="modecur" id="modecur">默认</span>
          <span class="modecv">▾</span>
        </button>
        <div class="modebd" id="modebd">
          <button class="chip" data-mode="default">默认</button>
          <button class="chip" data-mode="plan" title="只读浏览，工具被裁剪，模型只能提交计划等你批">规划</button>
          <button class="chip" data-mode="auto" title="非只读工具都要你审批">审批</button>
          <button class="chip" data-mode="bypass" title="跳过一切审批（高危）">跳过</button>
          <span class="mlabel" id="modehint"></span>
        </div>
      </div>
      <div class="inputrow">
        <textarea id="txt" rows="2" placeholder="告诉 Agent 你要做什么……"></textarea>
        <button class="btn" id="send">发送</button>
      </div>
    </div>
  </section>
  <section data-view="events">
    <div class="panel">
      <div class="filters" id="evFilters">
        <button class="chip active" data-ev="all">全部</button>
        <button class="chip" data-ev="agent/permission/decision">权限</button>
        <button class="chip" data-ev="agent/chat">会话</button>
        <button class="chip" data-ev="agent/turn">运行</button>
      </div>
      <div id="eventsBox"></div>
    </div>
  </section>
  <section data-view="perms"><div class="panel" id="permsBox"></div></section>
  <section data-view="memory"><div class="panel" id="memoryBox"></div></section>
  <section data-view="tools"><div class="panel" id="toolsBox"></div></section>
  <section data-view="settings"><div class="panel" id="settingsBox"></div></section>
  <section data-view="plugins">
    <div class="panel">
      <div class="rowline" style="display:flex;gap:10px;align-items:center;margin-bottom:12px">
        <input id="pname" placeholder="插件名（如 hello_probe）" style="flex:1;min-width:160px">
        <input id="pprovides" placeholder="provides（逗号分隔，留空默认 svc）" style="flex:1">
        <button class="btn ghost" id="pscaffold">新增插件</button>
      </div>
      <div id="pluginsBox"></div>
    </div>
  </section>
</div>
</div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const json2=(o)=>JSON.stringify(o,null,2);
function inlineLn(ln){                       // 单行行内规则（围栏内部不走这里）
  let h=esc(ln);
  h=h.replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');
  return h.replace(/^#{1,3} (.*)$/,'<b>$1</b>')
          .replace(/^- (.*)$/,'<span style="color:var(--muted)">•</span>&nbsp;$1');}
function fenceOpen(b){                       // 块内围栏是否未闭合（奇数个围栏行）
  let n=0;String(b||'').split('\n').forEach(l=>{if(/^\s*```/.test(l))n++;});
  return n%2===1;}
function mdInner(t){                         // 一个块 → HTML：行间补 <br>（.text 是 normal 空白），围栏内原样
  const out=[];let code=null;
  String(t||'').split('\n').forEach(ln=>{
    if(/^\s*```/.test(ln)){if(code===null){code=[];}else{out.push('<pre>'+esc(code.join('\n'))+'</pre>');code=null;}return;}
    if(code!==null){code.push(ln);return;}
    out.push(inlineLn(ln));});
  if(code!==null){out.push('<pre>'+esc(code.join('\n'))+'</pre>');}   // 未闭合围栏：乐观渲染（只追加、不回溯）
  return out.join('<br>');}
function mdBlock(b){return '<div class="blk">'+mdInner(b)+'</div>';}
function partBlocks(src){                    // 围栏感知的块切分：空行分界，末块恒为"活动尾块"
  const lines=String(src||'').split('\n');
  let end=lines.length;if(end&&lines[end-1]==='')end--;    // 末尾单个换行只是"这行写完了"，不算空行
  const blocks=[];let cur=[],fence=false;
  for(let i=0;i<end;i++){
    const ln=lines[i];
    if(/^\s*```/.test(ln)){fence=!fence;cur.push(ln);continue;}
    if(!fence&&/^[ \t]*$/.test(ln)){if(cur.length){blocks.push(cur.join('\n'));cur=[];}continue;}
    cur.push(ln);}
  if(cur.length){blocks.push(cur.join('\n'));
    return {stable:blocks.slice(0,-1),tail:blocks[blocks.length-1]};}
  return {stable:blocks,tail:''};}
function holdTail(b){                        // 行内标记未闭合 → 只压住活动块末尾一小段（滞后一拍）
  if(fenceOpen(b))return b;                  // 围栏内是字面量，无需让步
  const i=b.lastIndexOf('\n')+1,line=b.slice(i);
  if(/^\s*`{1,2}$/.test(line))return b.slice(0,i);   // 半截围栏（` / ``）：压住等它长成围栏
  if(/^\s*```/.test(line))return b;                  // 完整围栏行：本身是结构，不参与行内判定
  let cut=-1;
  ['**','`'].forEach(m=>{if((line.split(m).length-1)%2===1){const p=line.lastIndexOf(m);if(p>cut)cut=p;}});
  return cut<0?b:b.slice(0,i+cut);}
function md(t){                              // 整篇渲染：历史消息与终帧对账共用同一口径
  const p=partBlocks(t),all=p.stable.concat(p.tail?[p.tail]:[]);
  return '<div class="text">'+all.map(mdBlock).join('')+'</div>';}
function stepRow(title,kind,status,payload){
  const ok=status==='ok', dot=ok?'ok':(status==='run'?'run':'err');
  return '<div class="step"><div class="top"><span class="dot '+dot+'"></span><span class="name">'+esc(title)+'</span>'
    +'<span class="tag '+(ok?'ok':'err')+'">'+esc(status)+'</span></div>'
    +'<details><summary>查看 '+esc(kind)+' 载荷</summary><pre>'+esc(payload)+'</pre></details></div>';}
function addMsg(who, text, evid){
  const d=document.createElement('div');d.className='msg '+who;
  if(who==='user'){d.innerHTML='<div class="who">你</div><div class="bubble">'+esc(text)+'</div>';}
  else{
    let steps='';
    if(evid){
      const plan=evid.plan||{}, exec=evid.evidence||{};
      steps=stepRow('plan','plan.json',exec.error?'err':'ok',
        json2({intent:plan.intent, tool:plan.tool||null, args:plan.args||null}));
      if(exec.intent){
        const name = exec.intent==='tool_call' ? ('tool:'+(exec.tool||plan.tool||'?')) : 'ctx.'+exec.intent;
        const brief = exec.ok ? (exec.result||exec.ping||exec.note||'ok') : String(exec.error||'denied');
        steps+=stepRow('execute: '+name,'evidence.json',exec.ok?'ok':'err', json2({ok:exec.ok, brief}));
      }
      steps+=stepRow('narrate','evidence.evidence', evid.tokens_out>0?'ok':'err',
        json2({tokens_in:evid.tokens_in, tokens_out:evid.tokens_out}));
    }
    d.innerHTML='<div class="who">Agent</div>'+md(text)+(steps?'<div class="steps">'+steps+'</div>':'');
  }
  $('#messages').appendChild(d);
  follow(true);}
// ---- 流式渲染合批（性能）：帧先进缓冲，一个渲染帧只刷一次 DOM ----
// 旧实现每帧都做 bub.innerHTML +=（整树重解析）并读 scrollHeight（强制同步布局），
// 实测峰值 206 帧/秒 ⇒ 主线程被打满。现在 token/thinking 只拼字符串，rAF 内合并写入。
let _msgs=null,_raf=0;
const _dirty=new Set();
const msgBox=()=>_msgs||(_msgs=$('.messages'));
const atBottom=()=>{const m=msgBox();return (m.scrollHeight-m.scrollTop-m.clientHeight)<48;};
function follow(force){        // force：新消息/卡片等用户主动行为；否则仅贴底时跟随（不把上翻的视图拽回）
  const m=msgBox();if(force||atBottom())m.scrollTop=m.scrollHeight;}
function appendText(node,text){              // 文本节点追加：不动已有节点，避免整树重解析
  const last=node.lastChild;
  if(last&&last.nodeType===3)last.nodeValue+=text;
  else node.appendChild(document.createTextNode(text));}
function scheduleFlush(){
  if(_raf)return;
  _raf=requestAnimationFrame(()=>{
    _raf=0;
    _dirty.forEach(u=>u.flush());
    _dirty.clear();
    follow(false);});}
let _ctrl=null,_streaming=false;
function addStreamMsg(){
  const d=document.createElement('div');d.className='msg agent';
  d.innerHTML='<div class="who">Agent</div>'
    +'<details class="think" open><summary>💭 思考过程</summary><div class="tk" style="white-space:pre-wrap;font-size:12px;color:#8a8a8a;"></div></details>'
    +'<div class="bubble sb">…</div><div class="steps ss"></div>';
  $('#messages').appendChild(d);
  follow(true);
  const bub=d.querySelector('.sb'),stp=d.querySelector('.ss'),think=d.querySelector('.think'),tk=d.querySelector('.tk');
  let gotToken=false,gotThink=false,gotStep=false,raw='',live=false;
  let bufThink='',wrap=null,blocks=[],tailEl=null,tailHtml='';
  const unit={
    node:d,
    renderText(src,settle){      // 增量渲染：已定稿块只追加、永不重绘；只重绘活动尾块
      src=String(src||'');
      if(!wrap){                 // 首片即建 .text 外壳：卡片框不再等到终帧才出现
        wrap=document.createElement('div');wrap.className='text';
        bub.innerHTML='';bub.appendChild(wrap);live=true;}
      const p=partBlocks(src);
      if(tailEl&&p.stable.length>blocks.length){        // 上一活动尾块已定稿 → 就地转正，不重建节点
        tailEl.innerHTML=mdInner(p.stable[blocks.length]);
        blocks.push(p.stable[blocks.length]);tailEl=null;tailHtml='';}
      for(let i=blocks.length;i<p.stable.length;i++){   // 追加新定稿块（定稿后永不重绘）
        const el=document.createElement('div');el.className='blk';el.innerHTML=mdInner(p.stable[i]);
        if(tailEl)wrap.insertBefore(el,tailEl);else wrap.appendChild(el);
        blocks.push(p.stable[i]);}
      if(p.tail){                                       // 活动尾块：每渲染帧至多重绘一次
        if(!tailEl){tailEl=document.createElement('div');tailEl.className='blk';
          wrap.appendChild(tailEl);tailHtml='';}
        const h=mdInner(settle?p.tail:holdTail(p.tail));
        if(h!==tailHtml){tailEl.innerHTML=h;tailHtml=h;}}
      else if(tailEl){wrap.removeChild(tailEl);tailEl=null;tailHtml='';}},
    flush(){                     // 一个渲染帧内只做一次 DOM 写入（首片到位即撤掉占位省略号）
      if(gotToken)unit.renderText(raw,false);
      if(bufThink){appendText(tk,bufThink);bufThink='';}},
    addStep(p){
      gotStep=true;
      const el=document.createElement('div');el.className='step';
      const src=(p&&p.source)?String(p.source):'';
      const nm=(src?src.replace(/^agent\//,''):'progress')
        +((p&&p.data&&p.data.step)?(' · step '+p.data.step):'');
      el.innerHTML='<div class="top"><span class="dot run"></span><span class="name">'+esc(nm)+'</span></div>'
        +'<details><summary>载荷</summary><pre>'+esc(json2((p&&p.data)||{}))+'</pre></details>';
      stp.appendChild(el);follow(false);},
    appendToken(text){                     // B5.2 打字机：只累积源码，渲染交给 rAF 内的增量渲染
      gotToken=true;raw+=String(text||'');
      _dirty.add(unit);scheduleFlush();},
    appendThinking(text){                  // B5.2 思考流（D4）：同样走缓冲合批
      gotThink=true;bufThink+=String(text||'');
      _dirty.add(unit);scheduleFlush();},
    finish(text,ev){
      _dirty.delete(unit);bufThink='';   // 终稿：丢弃未刷缓冲，防止覆盖后又被追加
      // 思考区：若从未收到思考帧则移除（避免空折叠块），否则收敛后折叠
      if(!gotThink&&think&&think.parentNode){think.parentNode.removeChild(think);}
      else if(think){think.open=false;}
      const t=String(text||'');
      if(gotToken&&t===raw){unit.renderText(raw,true);}   // 与权威文本一致 → 只收尾活动块，零重排
      else{bub.innerHTML=md(t||'（无回复）');}         // 有分叉 → 用权威文本整篇重建（对账，安全网）
      if(ev){stp.innerHTML+=stepRow('narrate','evidence.json',(ev.tokens_out>0)?'ok':'err',
        json2({tokens_in:ev.tokens_in,tokens_out:ev.tokens_out}));}
      follow(false);},
    cancel(){                             // 保留已打出部分，仅追加标注
      _dirty.delete(unit);bufThink='';
      if(!live&&!gotToken){live=true;bub.innerHTML='';}  // 尚未出字：不留占位省略号
      else if(gotToken){unit.renderText(raw,true);}      // 收尾活动块，避免尾部被"滞后"截掉
      bub.innerHTML+='<span class="cancelled">（已取消）</span>';
      follow(false);},
    isEmpty(){return !gotToken&&!gotThink&&!gotStep;},  // 空段（尚未产出任何内容）
    seal(){                               // 封段：卡片出现前定稿，其后输出另起新段落在卡片下方
      _dirty.delete(unit);bufThink='';
      if(think&&think.parentNode){if(!gotThink)think.parentNode.removeChild(think);else think.open=false;}
      if(!gotToken){bub.style.display='none';}         // 无正文则不显示空气泡
      else{unit.renderText(raw,true);}                  // 活动块收尾，不再整篇重建
      follow(false);}
  };
  return unit;}// ---- 流式分段：卡片帧先封段再插卡，保证 DOM 顺序即发生顺序（不再把卡片后的输出写回卡片上方）----
let _seg=null;
function beginTurnStream(){_seg=addStreamMsg();return _seg;}
function useSeg(){if(!_seg)_seg=addStreamMsg();return _seg;}
function sealSeg(){
  if(!_seg)return;
  const s=_seg;_seg=null;
  if(s.isEmpty()){const n=s.node;if(n.parentNode)n.parentNode.removeChild(n);return;}
  s.seal();}
function handleFrame(ev,p){
  if(ev==='progress'){useSeg().addStep(p);}
  else if(ev==='thinking'){useSeg().appendThinking(p.data?p.data.text:'');}
  else if(ev==='token'){useSeg().appendToken(p.data?p.data.text:'');}
  else if(ev==='final'){useSeg().finish(p.text,p.evidence);}
  else if(ev==='cancelled'){if(_seg)_seg.cancel();}
  else if(ev==='approval'){sealSeg();showApproval(p.data||{});}
  else if(ev==='approval_done'){dismissApproval(p.data||{});}
  else if(ev==='ask_user'){sealSeg();showAskUser(p.data||{});}
  else if(ev==='ask_user_done'){dismissAskUser(p.data||{});}
  else if(ev==='error'){useSeg().finish('请求失败：'+(p.error||''),null);}
}
async function send(){
  if(_streaming&&_ctrl){_ctrl.abort();return;}
  const txt=$('#txt').value.trim();if(!txt)return;
  addMsg('user',txt);$('#txt').value='';
  _streaming=true;_ctrl=new AbortController();
  $('#send').disabled=false;$('#send').textContent='停止';
  beginTurnStream();
  try{
    const r=await fetch('/api/chat/stream',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:txt}),signal:_ctrl.signal});
    if(!r.ok||!r.body)throw new Error('HTTP '+r.status);
    const reader=r.body.getReader(),dec=new TextDecoder();let buf='';
    for(;;){
      const {done,value}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});
      let i;
      while((i=buf.indexOf('\n\n'))>=0){
        const line=buf.slice(0,i);buf=buf.slice(i+2);
        if(!line.startsWith('data:'))continue;
        let f;try{f=JSON.parse(line.slice(5).trim());}catch(e){continue;}
        const ev=f.event,p=f.payload||{};
        handleFrame(ev,p);
      }
    }
  }catch(e){
    if(e&&e.name==='AbortError'){useSeg().finish('（已取消）',null);}
    else{useSeg().finish('请求失败：'+((e&&e.message)||e),null);}
  }finally{
    _streaming=false;_ctrl=null;_seg=null;
    $('#send').disabled=false;$('#send').textContent='发送';$('#txt').focus();}}
// ---- approval popup（人在环中，第 2 期）：SSE pending 帧弹出，应答经 /api/approval/answer ----
const appr={};
function scrollMsg(){follow(true);}                     // 强制贴底（新消息/卡片/答复等用户主动行为）
function addUserNote(text){                             // 答复留痕：紧跟卡片，与前后各段构成单一时间序
  if(!text)return;
  const d=document.createElement('div');d.className='msg user';
  d.innerHTML='<div class="who">你</div><div class="bubble">'+esc(text)+'</div>';
  $('#messages').appendChild(d);scrollMsg();}
function hitCard(kind,badge,title,sub){                 // workbuddy 风格：内嵌对话流的扁平卡片
  const el=document.createElement('div');el.className='hitcard';
  el.innerHTML='<div class="hithd"><div class="hitic '+kind+'">'+badge+'</div>'
    +'<div class="hittt"><b>'+esc(title)+'</b><span>'+esc(sub)+'</span></div>'
    +'<span class="tag run">待处理</span></div>';
  $('#messages').appendChild(el);scrollMsg();
  return el;}
function showApproval(d){
  if(!d||!d.call_id||appr[d.call_id])return;           // 同 call_id 只弹一次
  const el=hitCard('approval','审批','请求执行工具',d.tool?('tool · '+d.tool):'需要你的批准后才会执行');
  const tag=el.querySelector('.tag');
  const riskCls=d.risk==='danger'?'err':(d.risk==='write'?'run':'ok');
  el.insertAdjacentHTML('beforeend',
    '<div class="hitmeta">'
    +(d.risk?'<span class="tag '+riskCls+'">风险 · '+esc(d.risk)+'</span>':'')
    +(d.mode?'<span class="tag off">档位 · '+esc(d.mode)+'</span>':'')
    +'</div>'
    +(d.kwargs&&Object.keys(d.kwargs).length?'<pre class="hitkw">'+esc(json2(d.kwargs))+'</pre>':'')
    +'<div class="hitft"><button class="btn hityes">批准</button>'
    +'<button class="btn ghost2 hitalways">总是允许</button>'
    +'<button class="btn danger hitno">拒绝</button>'
    +'<span class="hitst"></span></div>');
  appr[d.call_id]=el;
  const st=el.querySelector('.hitst');
  const done=(txt,cls)=>{el.querySelectorAll('button,input').forEach(x=>x.disabled=true);
    tag.className='tag '+cls;tag.textContent=txt;st.textContent='';scrollMsg();};
  const answer=async(approved,always)=>{
    st.textContent='提交中…';
    try{const r=await fetch('/api/approval/answer',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({call_id:d.call_id,approved,always:!!always})});
      const j=await r.json();
      if(j.ok&&j.resolved){done(approved?(always?'已批准 · 本会话同类不再问':'已批准'):'已拒绝',approved?'ok':'err');
        addUserNote(approved?(always?'总是允许（本会话同类不再询问）':'批准执行'):'拒绝执行');}
      else done('已超时 · 默认拒绝','err');
    }catch(e){st.textContent='提交失败：'+e.message;}};
  el.querySelector('.hityes').onclick=()=>answer(true);
  el.querySelector('.hitalways').onclick=()=>answer(true,true);
  el.querySelector('.hitno').onclick=()=>answer(false);
}
function dismissApproval(d){
  const id=d&&d.call_id,el=id&&appr[id];if(!el)return;  // 他处应答/超时：原卡片转为已了结态
  el.querySelectorAll('button,input').forEach(x=>x.disabled=true);
  const tag=el.querySelector('.tag');
  const ok=d&&d.approved;
  tag.className='tag '+(ok?'ok':'err');
  tag.textContent=ok?'已批准':'已了结 · 默认拒绝';
  const st=el.querySelector('.hitst');if(st)st.textContent='';
}
const asku={};                                         // call_id → 问询卡片元素
function optRow(o,multi){                               // 卡片行形态：标题 + 说明（可多选）
  return '<div class="hitopt'+(multi?' multi':'')+'" data-v="'+esc(o.value||o.label)+'" data-l="'+esc(o.label)+'">'
    +'<span class="mk"></span><span class="tt"><b>'+esc(o.label)+'</b>'
    +(o.desc?'<em>'+esc(o.desc)+'</em>':'')+'</span></div>';}
function optList(opts,multi){                           // 选择栏：形态由选择规格决定
  const rich=multi||opts.some(o=>o.desc)||opts.some(o=>o.group);
  if(!rich)                                             // 只有候选名 → 紧凑 chip 行（原观感）
    return '<div class="hitops">'+opts.map(o=>'<button class="chip apopt" data-v="'+esc(o.value||o.label)
      +'" data-l="'+esc(o.label)+'">'+esc(o.label)+'</button>').join('')+'</div>';
  const order=[],byG={};                                // 有说明/有分组/多选 → 卡片行，按 group 分区
  opts.forEach(o=>{const g=o.group||'';if(!(g in byG)){byG[g]=[];order.push(g);}byG[g].push(o);});
  return order.map(g=>'<div class="hitgrp">'+(g?'<div class="gtt">'+esc(g)+'</div>':'')
      +byG[g].map(o=>optRow(o,multi)).join('')+'</div>').join('')
    +(multi?'<button class="btn hitsub" disabled>提交选择</button>':'');}
function showAskUser(d){
  if(!d||!d.call_id||asku[d.call_id])return;
  const opts=Array.isArray(d.options)?d.options:[],multi=!!d.multi;
  const sub=opts.length?(multi?'可多选，勾选后一次提交；也可输入文字指示'
                               :'点选方案或输入文字指示，答复后将继续执行')
                        :'输入文字指示后，Agent 将继续执行';
  const el=hitCard('ask','问询','Agent 想确认你的想法',sub);
  const tag=el.querySelector('.tag');
  let h='<div class="hitbd">'+esc(d.question||'（无问题文本）')+'</div>';
  if(opts.length)h+=optList(opts,multi);
  if(d.allow_free_text!==false)
    h+='<div class="hitrow"><input class="hitinput" placeholder="或输入文字指示…"><button class="btn hitsend">发送</button></div>';
  h+='<div class="hitst" style="margin-top:8px"></div>';
  el.insertAdjacentHTML('beforeend',h);
  asku[d.call_id]=el;
  const st=el.querySelector('.hitst');
  const done=(txt,cls)=>{el.classList.add('done');      // 选项行是 div，单靠 disabled 拦不住
    el.querySelectorAll('button,input').forEach(x=>x.disabled=true);
    tag.className='tag '+(cls||'ok');tag.textContent=txt;st.textContent='';scrollMsg();};
  const answer=async(payload,note)=>{
    st.textContent='提交中…';
    try{const r=await fetch('/api/approval/answer',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({call_id:d.call_id,...payload})});
      const j=await r.json();
      if(j.ok&&j.resolved){done('已答复 · Agent 继续中','ok');
        addUserNote(note||payload.choice||payload.text||'');}
      else done('已超时 · Agent 将按假设继续','run');
    }catch(e){st.textContent='提交失败：'+e.message;}};
  el.querySelectorAll('.apopt').forEach(b=>b.onclick=()=>{   // 紧凑形态：点选即提交
    el.querySelectorAll('.apopt').forEach(x=>x.classList.toggle('selected',x===b));
    answer({answered:true,choice:b.dataset.v},b.dataset.l);});
  const rows=Array.from(el.querySelectorAll('.hitopt'));      // 卡片行形态
  if(rows.length){
    const sub2=el.querySelector('.hitsub');
    rows.forEach(r=>r.onclick=()=>{
      if(!multi){rows.forEach(x=>x.classList.toggle('selected',x===r));
        answer({answered:true,choice:r.dataset.v},r.dataset.l);return;}
      r.classList.toggle('selected');
      if(sub2)sub2.disabled=!rows.some(x=>x.classList.contains('selected'));
      st.textContent='';});
    if(sub2)sub2.onclick=()=>{
      const pick=rows.filter(x=>x.classList.contains('selected'));
      if(!pick.length){st.textContent='请至少选择一项';return;}
      answer({answered:true,choices:pick.map(x=>x.dataset.v)},
             pick.map(x=>x.dataset.l).join('、'));};}
  const send=()=>{const t=el.querySelector('.hitinput');const v=t?t.value.trim():'';
    if(!v)return;answer({answered:true,text:v},v);};
  const sb=el.querySelector('.hitsend');if(sb)sb.onclick=send;
  const tx=el.querySelector('.hitinput');
  if(tx)tx.addEventListener('keydown',e=>{if(e.key==='Enter'){e.preventDefault();send();}});
}
function dismissAskUser(d){
  const id=d&&d.call_id,el=id&&asku[id];if(!el)return;
  el.classList.add('done');
  el.querySelectorAll('button,input').forEach(x=>x.disabled=true);
  const tag=el.querySelector('.tag');
  const ok=d&&d.answered;
  tag.className='tag '+(ok?'ok':'run');
  tag.textContent=ok?'已答复':'已超时 · Agent 将按假设继续';
  const st=el.querySelector('.hitst');if(st)st.textContent='';
}
function fmt(x){return x===null||x===undefined?'-':(typeof x==='object'?json2(x):x);}
function tableFrom(rows,cols){return '<table><tr>'+cols.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr>'
  +rows.map(r=>'<tr>'+cols.map(c=>'<td>'+esc(fmt(r[c]))+'</td>').join('')+'</tr>').join('')+'</table>';}
async function loadStatus(){
  try{const j=await (await fetch('/api/status')).json();
    $('#model').textContent=j.provider_label+' · '+j.model;
    $('#session').textContent='session '+j.session_id;
    $('#machine').textContent='v'+j.version+'\n预算 '+(j.budget?j.budget.spent+'/'+j.budget.max:'-');}catch(e){}}
async function loadSession(){
  try{const j=await (await fetch('/api/session')).json();
    $('#messages').innerHTML='';
    (j.history||[]).forEach(m=>addMsg(m.role==='user'?'user':'agent', m.content, null));
  }catch(e){}}
function renderPlugins(rows){
  let h='<h3>插件（agent/plugins · 改后立即重建生效）</h3><table><tr><th>id</th><th>layer</th><th>provides</th><th>版本</th><th>状态</th><th>操作</th></tr>';
  h+=rows.map(p=>{const key=esc(p.name||p.id);let ops='—';
    if(p.delta!=='system/builtin'){
      ops='<button class="chip" data-plug-act="enable" data-key="'+key+'" data-enable="'+(p.desired?'false':'true')+'">'+(p.desired?'停用':'启用')+'</button> '
        +'<button class="chip" data-plug-act="remove" data-key="'+key+'" style="color:var(--err)">移除</button>';}
    let st,cls;
    if(p.delta==='system/builtin'){st='系统/内置';cls='ok';}
    else if(p.desired&&p.actual){st='已启用';cls='ok';}
    else if(p.desired&&!p.actual){st='启用中（重启生效）';cls='run';}
    else if(!p.desired&&p.actual){st='未启用（重启生效）';cls='run';}
    else {st='未启用';cls='';}
    return '<tr><td>'+key+'</td><td>'+esc(p.layer||'')+'</td><td>'+esc((p.provides||[]).join(','))+'</td><td>'+esc(p.version||'')+'</td>'
      +'<td><span class="tag '+cls+'">'+esc(st)+'</span></td><td>'+ops+'</td></tr>';}).join('');
  h+='</table><div class="muted" style="margin-top:6px;font-size:11.5px">新增插件：填名字与 provides → 新增插件（自动启用并重建生效）；系统/内置业务不可管理（与引擎同语义）。</div>';
  $('#pluginsBox').innerHTML=h;
}
async function load(name){
  try{const j=await (await fetch('/api/'+name)).json();
    if(name==='events'){
      const rows=(j.rows||[]).filter(r=>{
        const f=(evFilter==='all');if(f)return true;return (r.event||'').startsWith(evFilter);});
      $('#eventsBox').innerHTML=(rows.length?tableFrom(rows,['seq','event','at','payload']):'<div class="empty">无匹配事件</div>');}
    else if(name==='perms'){
      const jj=j.policy||{};
      let h='';
      if(j.mode){                                        // approval 插件启用时才显示档位切换
        const modes=[['default','默认'],['plan','计划'],['auto','自动'],['bypass','跳过']];
        h+='<h3>会话档位（approval mode）</h3><div class="filters" id="modeBar">'
          +modes.map(([k,l])=>'<button class="chip'+(j.mode===k?' active':'')+'" data-mode="'+k+'">'+l+'</button>').join('')
          +'</div><div class="muted" style="margin:-6px 0 12px;font-size:11.5px">plan=只读计划 · auto=写类触发审批 · bypass=跳过审批（高危，需确认）</div>';
      }
      h+='<h3>权限矩阵（config.agent.permissions，默认 open）</h3>'
        +'<div class="card"><pre class="json">'+esc(json2(jj))+'</pre></div>'
        +'<h3>最近裁决（agent/permission/decision，durable）</h3>'
        +(j.recent&&j.recent.length?tableFrom(j.recent.slice(0,20),['seq','payload','at']):'<div class="empty">暂无裁决</div>');
      $('#permsBox').innerHTML=h;}
        else if(name==='plugins'){ renderPlugins(j.plugins||[]); }
    else if(name==='memory'){
      const lt=j.long_term||{};
      let h='<h3>记忆 · 分层总览</h3>'
        +'<div class="muted" style="margin:-4px 0 12px">三层记忆（系统 / 短期 / 工作）始终可见，无论长期记忆是否启用；长期记忆（plugins.memory）为可选子块。</div>';
      (j.layers||[]).forEach(L=>{
        h+='<div class="card" style="margin-bottom:12px"><div style="display:flex;justify-content:space-between;align-items:center">'
          +'<b>'+esc(L.name)+'</b><span class="tag '+(L.available?'blue':'off')+'">'+(L.available?'已装配':'未启用')+'</span></div>'
          +'<div class="muted" style="font-size:11px;margin:2px 0 8px">'+esc(L.subtitle)+'</div>';
        if(!L.available){ h+='<div class="empty">该层未启用 —— 在「插件」页点「启用」后生效。</div>'; }
        else{
          const s=L.summary||{};
          if(L.key==='system_memory'){
            h+='<div class="mono muted">字符数 '+(s.chars||0)+'</div>';
            if(s.preview) h+='<pre class="json" style="max-height:160px;overflow:auto">'+esc(s.preview)+'</pre>';
          } else if(L.key==='short_term'){
            h+='<span class="tag blue" style="margin:0 6px 4px 0;display:inline-block">消息 '+(s.messages||0)+'</span>'
              +'<span class="tag blue" style="margin:0 6px 4px 0;display:inline-block">已摘要覆盖至 seq '+(s.summary_covers_seq||0)+'</span>'
              +'<span class="tag '+(s.has_summary?'blue':'off')+'" style="margin:0 6px 4px 0;display:inline-block">'+(s.has_summary?'有滚动摘要':'无滚动摘要')+'</span>';
            if(s.compact_stats) h+='<div class="mono muted" style="margin-top:4px">'+esc(json2(s.compact_stats))+'</div>';
            if(s.latest_summary_preview) h+='<pre class="json" style="max-height:140px;overflow:auto">'+esc(s.latest_summary_preview)+'</pre>';
          } else if(L.key==='work_memory'){
            h+='<span class="tag blue" style="display:inline-block">活跃任务 '+(s.count||0)+'</span>';
            if(s.active_tasks&&s.active_tasks.length) h+='<div class="mono" style="margin-top:4px">'+esc(json2(s.active_tasks))+'</div>';
            else h+='<div class="empty" style="margin-top:4px">当前无活跃任务</div>';
          }
        }
        h+='</div>';
      });
      h+='<h3 style="margin-top:18px">长期记忆（plugins.memory · opt-in）</h3>';
      if(!lt.enabled){ h+='<div class="empty">未启用 —— 在「插件」页点「启用」后，下方显示其 Zep 式记忆内容。</div>'; }
      else{
        const st=lt.stats||{};
        h+='<div class="card">'+(Object.entries(st.by_kind||{}).map(([k,v])=>'<span class="tag blue" style="margin:0 6px 4px 0;display:inline-block">'+esc(k)+' '+v+'</span>').join('')||'<span class="muted">空</span>')+'<span class="mono muted" style="margin-left:10px">total='+(st.total||0)+'</span></div>';
        h+=(lt.rows&&lt.rows.length?tableFrom(lt.rows,['id','kind','content','score_observed','created_at','accessed_count','invalid_at','source']):'<div class="empty">暂无记忆</div>');
      }
      $('#memoryBox').innerHTML=h;}
    else if(name==='tools'){
      const rows=j.tools||[];
      let h='<h3>工具目录（ctx.tasks.catalog → 供 tool_call 调用）</h3>';
      h+=rows.length?rows.map(t=>'<div class="card"><div style="display:flex;justify-content:space-between;gap:10px"><b>'+esc(t.name)+'</b><span class="tag blue">'+esc(t.owner||'-')+'</span></div>'
        +'<div class="muted" style="margin-top:4px">'+esc(t.description||'')+'</div>'
        +'<div class="mono" style="margin-top:4px">params='+esc(json2(t.params||{}))+' · required='+esc((t.required||[]).join(','))+'</div></div>').join('')
        :'<div class="empty">暂无工具（接入 windows-mcp 等工具插件后出现）</div>';
      $('#toolsBox').innerHTML=h;}
    else if(name==='settings'){ renderSettings(j); }
  }catch(e){$('#'+name+'Box').innerHTML='<div class="card e">'+esc(e.message)+'</div>';}}
let evFilter='all';
let plugBusy=false;
async function plugAction(payload){
  if(plugBusy)return;plugBusy=true;
  // 立即禁用所有插件按钮并显示“处理中…”，消除“点击后没反应”的观感（重建容器本身需要数秒）
  document.querySelectorAll('#pluginsBox [data-plug-act]').forEach(b=>{b.disabled=true;b.dataset.lb=b.textContent;b.textContent='处理中…';});
  try{
    const r=await fetch('/api/plugins/'+payload.act,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload.body||{})});
    const j=await r.json();
    if(j.plugins) renderPlugins(j.plugins); else await load('plugins');
    if(!j.ok||j.rebuild_error){alert('注意：'+(j.rebuild_error||j.error||r.status)+' （期望态已保存，实际装配可能需重启）');}
    // 后台重建完成后再刷新一次，使状态从“启用中（重启生效）”更新为“已启用”
    setTimeout(()=>{ load('plugins'); loadStatus(); }, 4000);
  }catch(e){alert('请求失败：'+e.message);}finally{plugBusy=false;await loadStatus();}
}
$('#pluginsBox').addEventListener('click',e=>{
  const b=e.target.closest('[data-plug-act]');if(!b)return;
  const act=b.dataset.plugAct, key=b.dataset.key;
  if(act==='remove'&&!confirm('移除 '+key+' ？'))return;
  if(act==='enable'&&b.dataset.enable==='false'&&!confirm('确定停用 '+key+' ？\n停用后需重启服务才会彻底卸载，期间仍装配于当前进程。'))return;
  plugAction(act==='enable'
    ?{act:'enable',body:{id:key,enable:b.dataset.enable==='true'}}
    :{act:'remove',body:{id:key}});
});
function renderSettings(j){
  const box=$('#settingsBox');
  const v=j.values||{};
  // 预算卡片恒渲染（budget 是 sys 层护栏，不随 context 插件启停）
  let h='<div class="card"><h3 style="margin-top:0">成本护栏（budget）</h3>'
    +'<div class="muted" style="margin-bottom:10px">LLM 调用的生命周期累计 token 上限：每次调用前先按估算预借、结束后按服务商真实用量对账；超限即拒绝调用。保存后立即重建容器生效（<b>已用量重计</b>）。覆盖写入 data/agent_plugin_config.json，重启后保持。</div>'
    +'<label style="display:block;margin:12px 0 0"><div style="font-weight:600;margin-bottom:3px">运行预算上限 max_tokens_per_run (token)</div>'
    +'<input id="set_max_tokens_per_run" type="number" step="1000" min="1000" value="'+esc(v.max_tokens_per_run)+'" style="width:240px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit">'
    +'<div class="muted" style="font-size:11px;margin-top:3px">当前默认 200000；网页「新会话」也会把已用量归零重计</div></label>'
    +'<label style="display:block;margin:12px 0 0"><div style="font-weight:600;margin-bottom:3px">收尾预留额度 finalize_reserve_tokens (token)</div>'
    +'<input id="set_finalize_reserve_tokens" type="number" step="1000" min="0" value="'+esc(v.finalize_reserve_tokens)+'" style="width:240px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit">'
    +'<div class="muted" style="font-size:11px;margin-top:3px">预算末段的收尾专用额度：正常调用只能用到「上限 − 预留」，触顶后的总结陈词可动用预留段，保证耗尽时也有体面收尾（0 = 关闭预留）。须不小于收尾窗口体积（建议 ≥ 24000）</div></label>'
    +'<label style="display:block;margin:12px 0 0"><div style="font-weight:600;margin-bottom:3px"><input id="set_cost_calibration" type="checkbox"'+(v.cost_calibration?' checked':'')+'> 口径自校准 cost_calibration</div>'
    +'<div class="muted" style="font-size:11px">用服务商真实用量持续修正字符估算的偏差（实测约 30%），使预算判定与窗口装配贴近真实计费；样本不足 5 次时自动退回裸估算。关闭 = 回到纯估算口径。</div></label></div>'
    +'<div class="card" style="margin-top:16px"><h3 style="margin-top:0">循环内上下文治理（plugins.react）</h3>'
    +'<div class="muted" style="margin-bottom:10px">开启后每次请求只携带「工具目录卡」（名称+一行描述+参数名，无完整 JSON Schema），模型经 tool_search / tool_load 元工具按需加载完整定义，可显著降低工具定义的 token 开销。默认关闭（灰度）。</div>'
    +'<label style="display:block;margin:12px 0 0"><div style="font-weight:600;margin-bottom:3px"><input id="set_lazy_tools" type="checkbox"'+(v.lazy_tools?' checked':'')+'> 启用懒加载 lazy_tools</div>'
    +'<div class="muted" style="font-size:11px">模型对未加载工具的调用会得到「未加载」错误观察并自行纠正；执行面闸门（审批/只读裁剪）不受影响。</div></label>'
    +'<label style="display:block;margin:12px 0 0"><div style="font-weight:600;margin-bottom:3px"><input id="set_alignment_enabled" type="checkbox"'+(v.alignment_enabled?' checked':'')+'> 续接前缀对齐 alignment_enabled</div>'
    +'<div class="muted" style="font-size:11px">续接时冻结头部快照（system/前置上下文/首轮提示词）跨 run 逐字节稳定，新消息追加链尾，使请求构成前序请求的严格前缀，命中提示缓存（实测前缀断裂时整链按 miss 计价）。关闭 = 回退旧行为，续接时头部随当前提示词重建（并发出 prefix-unstable 事件）。</div></label></div>';
  if(j.context_plugin_enabled!==true){
    h+='<div class="card muted" style="margin-top:16px">上下文管理插件（plugins.context）未启用 —— 在「插件」页点「启用」后，可在此调整参数。</div>';
  }else{
  const fields=[
    ['compact_trigger_tokens','压缩触发阈值 (token)','未摘要历史达到此值即压缩（压缩时机由该阈值单独决定）'],
    ['max_context_tokens','上下文窗口上限 (token)','治理器输入窗口总预算上限'],
    ['reserve_output_tokens','输出余量 (token)','预留给模型输出的 token，不计入输入预算'],
    ['memory_ratio','记忆块占比','长期记忆注入配额占输入预算的比例 (0~1)'],
    ['history_ratio','历史窗口占比','历史消息窗口占输入预算的比例 (0~1)'],
    ['source_clip_tokens','MCP 源头护栏 (token)','外部源头内容的最大裁剪上限'],
    ['clip_max_tokens','循环内 · 单条观察上限 (token)','多步 ReAct 循环内，单条工具观察超过此值即做首尾折叠'],
    ['clip_head_tokens','循环内 · 折叠保留头部 (token)','折叠后保留开头部分（含关键结论/参数）'],
    ['clip_tail_tokens','循环内 · 折叠保留尾部 (token)','折叠后保留结尾部分（错误码通常在尾部）'],
    ['react_keep_recent','循环内 · 最近保留条数','最近 N 条推理链保留原文，更早的才折叠'],
    ['react_min_chain_tokens','循环内 · 链保底额度 (token)','装配会话历史时为多步循环预留的额度，防止循环无空间'],
    ['working_window_tokens','经济窗口 (token)','水位判定的分母；与能力上限 max_context_tokens 解耦。默认 96k——保证普通任务永不触发压缩（压缩是超长任务兜底）'],
    ['window_high_ratio','触发水位 (0~1)','load_ratio ≥ 此值即判 high（默认 0.70）'],
    ['window_low_ratio','回落目标 (0~1)','水位回落参考值（记录用，默认 0.40）'],
    ['resident_cap_tokens','常驻层 cap (token)','系统提示 / 输出格式等常驻块上限；绝对量级（0 = 无 cap）'],
    ['on_demand_cap_tokens','按需层 cap (token)','工具 schema / 记忆召回等按需块上限（0 = 无 cap）'],
    ['prompt_cap_tokens','输入层 cap (token)','用户本轮输入 / 附件上限（0 = 无 cap）'],
    ['clear_at_least_tokens','清除最小释放量 (token)','单次 clear 动作的最小释放量门槛；低于此值不触发清除（对标 Anthropic clear_at_least）'],
  ];
  const cm=['off','shadow','on'];
  const cml={off:'不治理（不做任何动作）',shadow:'只算不做：照常计算决策并写遥测，用于校准阈值（推荐）',on:'实际执行结构性改变（clear/compact 落位）'};
  const isFloat=k=>(k==='memory_ratio'||k==='history_ratio'||k==='window_high_ratio'||k==='window_low_ratio');
    h+='<h3 style="margin-top:16px">上下文管理 <span class="tag blue">plugins.context 已启用</span></h3>'
    +'<div class="card"><div class="muted" style="margin-bottom:10px">启用上下文管理插件时可调；保存后立即重建容器生效。覆盖写入 data/agent_plugin_config.json，重启后保持。</div>';
  h+=fields.map(([k,label,hint])=>'<label style="display:block;margin:12px 0 0"><div style="font-weight:600;margin-bottom:3px">'+esc(label)+'</div>'
    +'<input id="set_'+k+'" type="number" step="'+(isFloat(k)?'0.01':'1')+'" value="'+esc(v[k])+'" style="width:240px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit">'
    +'<div class="muted" style="font-size:11px;margin-top:3px">'+esc(hint)+'</div></label>').join('');
  h+='<label style="display:block;margin:12px 0 0"><div style="font-weight:600;margin-bottom:3px">压缩模式 compact_mode</div>'
    +'<select id="set_compact_mode" style="width:340px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff">'
    +cm.map(o=>'<option value="'+o+'"'+(v.compact_mode===o?' selected':'')+'>'+o+'（'+cml[o]+'）</option>').join('')
    +'</select><div class="muted" style="font-size:11px;margin-top:3px">治理动词（clear/compact）的执行开关；默认 shadow（只观测不改变装配行为）。链压缩仅在 run 收尾存档点、且过回本闸时发生。</div></label>';
  h+='</div>';                                             // 上下文管理卡片收口
  }                                                        // context 启用分支收口
  if(j.approval_enabled===false){
    h+='<div class="card muted" style="margin-top:16px">审批插件（plugins.approval）未启用 —— 在「插件」页点「启用」后，可在此调整。</div>';
  }else{
    const aps=['auto-deny','auto-allow','web','cli'];
    const apl={'auto-deny':'问人即拒（fail-safe，无弹窗）','auto-allow':'问人即批（开发用，无弹窗）','web':'网页弹窗审批（推荐）','cli':'终端问答（y=批 / a=本会话总是允许）'};
    h+='<div class="card" style="margin-top:16px"><h3 style="margin-top:0">审批 · 人在循环（plugins.approval）</h3>'
      +'<div class="muted" style="margin-bottom:10px">调整审批通道与名单；保存后立即重建容器生效（会话档位回到默认）。覆盖写入 data/agent_plugin_config.json，重启后保持。</div>'
      +'<label style="display:block;margin:12px 0 0"><div style="font-weight:600;margin-bottom:3px">问人通道 provider</div>'
      +'<select id="set_provider" style="width:340px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit;background:#fff">'
      +aps.map(o=>'<option value="'+o+'"'+(v.provider===o?' selected':'')+'>'+o+'（'+apl[o]+'）</option>').join('')
      +'</select><div class="muted" style="font-size:11px;margin-top:3px">仅 web / cli 为交互式，会真正等人答复（web 才有网页弹窗）</div></label>'
      +'<label style="display:block;margin:12px 0 0"><div style="font-weight:600;margin-bottom:3px">等待时限 timeout_s（秒）</div>'
      +'<input id="set_timeout_s" type="number" step="1" min="1" value="'+esc(v.timeout_s)+'" style="width:240px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit">'
      +'<div class="muted" style="font-size:11px;margin-top:3px">无人应答超过此时限 → 默认拒绝（fail-safe）</div></label>'
      +'<label style="display:block;margin:12px 0 0"><div style="font-weight:600;margin-bottom:3px">必问名单 ask（fnmatch，逗号分隔）</div>'
      +'<input id="set_ask" type="text" value="'+esc((v.ask||[]).join(', '))+'" placeholder="如 t.write*, *delete*" style="width:360px;padding:7px 9px;border:1px solid var(--line);border-radius:8px;font:inherit">'
      +'<div class="muted" style="font-size:11px;margin-top:3px">命中名单的工具执行前必问人；配合对话页「审批」档可让所有非只读工具都问</div></label></div>';
  }
  h+='<div style="margin-top:16px"><button class="btn" id="saveSettings">保存</button> '
    +'<button class="btn ghost" id="resetSettings">恢复默认</button> <span class="muted" id="setMsg"></span></div>';
  box.innerHTML=h;
  $('#saveSettings').onclick=saveSettings;
  $('#resetSettings').onclick=resetSettings;
}
async function saveSettings(){
  const isFloat=k=>(k==='memory_ratio'||k==='history_ratio'||k==='window_high_ratio'||k==='window_low_ratio');
  const keys=['max_tokens_per_run','finalize_reserve_tokens','compact_trigger_tokens','max_context_tokens','reserve_output_tokens','memory_ratio','history_ratio','source_clip_tokens','clip_max_tokens','clip_head_tokens','clip_tail_tokens','react_keep_recent','react_min_chain_tokens','working_window_tokens','window_high_ratio','window_low_ratio','resident_cap_tokens','on_demand_cap_tokens','prompt_cap_tokens','clear_at_least_tokens'];
  const patch={};
  for(const k of keys){const el=$('#set_'+k);if(el&&el.value!==''){patch[k]=isFloat(k)?parseFloat(el.value):parseInt(el.value,10);}}
  const cc=$('#set_cost_calibration');if(cc)patch.cost_calibration=cc.checked;   // F1 开关
  const lt=$('#set_lazy_tools');if(lt)patch.lazy_tools=lt.checked;               // L1 开关
  const al=$('#set_alignment_enabled');if(al)patch.alignment_enabled=al.checked; // P0-2 续接前缀对齐
  const cmx=$('#set_compact_mode');if(cmx)patch.compact_mode=cmx.value;  // P2 治理模式（枚举）
  const pv=$('#set_provider');if(pv)patch.provider=pv.value;      // 审批插件三项
  const tv=$('#set_timeout_s');if(tv&&tv.value!=='')patch.timeout_s=parseInt(tv.value,10);
  const av=$('#set_ask');if(av)patch.ask=av.value.split(',').map(s=>s.trim()).filter(Boolean);
  $('#setMsg').textContent='保存中…';
  try{const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({patch})});
    const j=await r.json();if(!j.ok){$('#setMsg').textContent='失败：'+(j.error||r.status);return;}
    $('#setMsg').textContent='已保存并重建生效 ✓';renderSettings(j.settings);}
  catch(e){$('#setMsg').textContent='失败：'+e.message;}
}
async function resetSettings(){
  if(!confirm('恢复默认参数？当前覆盖（成本护栏 / 上下文管理 / 审批插件）将被清除。'))return;
  $('#setMsg').textContent='恢复中…';
  try{const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({reset:true})});
    const j=await r.json();if(!j.ok){$('#setMsg').textContent='失败：'+(j.error||r.status);return;}
    $('#setMsg').textContent='已恢复默认 ✓';renderSettings(j.settings);}
  catch(e){$('#setMsg').textContent='失败：'+e.message;}
}
const MODE_LABEL={default:'默认',plan:'规划：模型只读浏览，计划需你批准',auto:'审批：非只读工具都要你确认',bypass:'跳过审批（高危）'};
const MODE_SHORT={default:'默认',plan:'规划',auto:'审批',bypass:'跳过'};
function setModeOpen(open){                            // 折叠式收纳：默认收起，选完自动收起
  $('#modebar').classList.toggle('open',open);
  $('#modehd').setAttribute('aria-expanded',open?'true':'false');}
function setModeUI(m){
  document.querySelectorAll('#modebar .chip').forEach(x=>x.classList.toggle('active',x.dataset.mode===m));
  $('#modehint').textContent=MODE_LABEL[m]||m;
  const cur=$('#modecur');cur.textContent=MODE_SHORT[m]||m;
  cur.classList.toggle('danger',m==='bypass');}
async function switchMode(m){
  const post=c=>fetch('/api/approval/mode',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:m,confirm:c})}).then(r=>r.json());
  try{
    if(m==='bypass'&&!confirm('bypass 将跳过所有审批确认（高危），确定切换？'))return;
    const j=await post(m==='bypass');
    if(!j.ok)return alert('切换失败：'+(j.error||''));
    setModeUI(j.mode);setModeOpen(false);                // 选完自动收起
  }catch(e){alert('请求失败：'+e.message);}
}
document.addEventListener('DOMContentLoaded',()=>{
  loadStatus();loadSession();
  $('#newSession').onclick=async()=>{await fetch('/api/session/reset',{method:'POST'});$('#messages').innerHTML='';loadSession();};
  $('#send').onclick=send;
  $('#txt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
  $('#modehd').addEventListener('click',()=>setModeOpen(!$('#modebar').classList.contains('open')));
  document.querySelectorAll('.nav-item').forEach(b=>b.onclick=()=>{
    document.querySelectorAll('.nav-item').forEach(x=>x.classList.remove('active'));b.classList.add('active');
    const vn=b.dataset.view;
    document.querySelectorAll('section[data-view]').forEach(s=>s.classList.toggle('active',s.dataset.view===vn));
    if(vn!=='chat')load(vn);});
  $('#evFilters').addEventListener('click',e=>{
    if(!e.target.classList.contains('chip'))return;
    document.querySelectorAll('#evFilters .chip').forEach(x=>x.classList.remove('active'));
    e.target.classList.add('active');evFilter=e.target.dataset.ev;load('events');});
  $('#permsBox').addEventListener('click',e=>{          // 会话档位切换（bypass 需二次确认）
    const b=e.target.closest('[data-mode]');if(!b)return;
    const m=b.dataset.mode;
    const doSet=c=>fetch('/api/approval/mode',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode:m,confirm:c})}).then(r=>r.json()).then(j=>{
        if(!j.ok)alert('失败：'+(j.error||''));else setModeUI(j.mode);load('perms');}).catch(e=>alert('请求失败：'+e.message));
    if(m==='bypass'){if(confirm('bypass 将跳过所有审批确认（高危），确定切换？'))doSet(true);}else doSet(false);});
  (async()=>{try{                                        // 刷新后恢复档位显示 + 在途弹窗/问询卡
    const j=await (await fetch('/api/approval/pending')).json();
    if(j.mode)setModeUI(j.mode);else setModeUI('default');
    (j.items||[]).forEach(it=>{const q=it.req||{};
      if(q.kind==='ask_user')showAskUser(q);
      else if(q.call_id)showApproval(q);});}catch(e){setModeUI('default');}})();
  $('#modebar').addEventListener('click',e=>{            // 对话页内直接切换权限档位
    const b=e.target.closest('[data-mode]');if(b)switchMode(b.dataset.mode);});
  $('#pscaffold').onclick=async()=>{
    const name=$('#pname').value.trim();if(!name)return alert('填插件名');
    const provides=$('#pprovides').value.split(',').map(s=>s.trim()).filter(Boolean);
    plugAction({act:'scaffold',body:{name,provides:provides.length?provides:null}});
    $('#pname').value='';$('#pprovides').value='';};
  load('perms');load('plugins');load('memory');load('tools');load('settings');
});
</script></body></html>
"""


async def _context_view_payload(root, *, limit: int = 60) -> dict:
    """**Inspector ``/context`` 视图**（G2.0 P3-1；模块级，供端点与测试共用）。

    纯只读旁路：从审计脊柱 ``agent_events`` 读 ``agent/context/governed`` 载荷 → 经治理器
    **门面软调用**（``gov.ledger``）重建 ``RoundLedger`` → 渲染分类视图 + 逐端点 I-4 对账。
    消费方**不 import 插件内部**（解耦纪律）。轮次归集（消费方语义）：每个 ``stage`` 取
    **最近一次**装配，即「这一轮钱花在哪」。无治理器 / 渲染失败 → ``available=False``（绝不崩）。
    """
    try:
        n = max(1, min(200, int(limit)))
    except (TypeError, ValueError):
        n = 60
    rows = await root.db.query(
        root, "SELECT seq, payload, at FROM agent_events"
              " WHERE event='agent/context/governed' ORDER BY seq DESC LIMIT ?", (n,))
    payloads: list[dict] = []
    for r in rows:                                  # 读入为新 → 旧
        try:
            p = json.loads(r.get("payload") or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(p, dict):
            payloads.append(p)
    payloads.reverse()                              # 回正序
    gov = root.get(Services.CONTEXT) if root.has(Services.CONTEXT) else None
    ledger_fn = getattr(gov, "ledger", None)
    if not callable(ledger_fn):
        return {"available": False, "reason": "no_governor", "stages": [], "text": ""}
    latest: dict[str, dict] = {}
    order: list[str] = []
    for p in payloads:                              # 每个 stage 取最近一次（保序去重）
        st = str(p.get("stage", "") or "")
        if st not in latest:
            order.append(st)
        latest[st] = p
    try:
        ledger = ledger_fn([latest[s] for s in order])
        return {"available": True, "stages": ledger.stages(), "totals": ledger.totals(),
                "reconcile": ledger.reconcile(), "violations": ledger.violations(),
                "text": ledger.render(), "events_considered": len(payloads)}
    except Exception as exc:                        # noqa: BLE001 —— 视图失败不阻断
        logging.getLogger("agent.chat").warning("context view failed: %s", exc)
        return {"available": False, "reason": "render_failed", "stages": [], "text": ""}


async def _serve(args: argparse.Namespace) -> int:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

    c, pm = await _boot(args)

    class _Box:
        """可换容器的句柄：handlers 引用 c.root 不受「重建容器」影响。"""

        def __init__(self, c):
            self.set(c)

        def set(self, c):
            self._c = c

        @property
        def root(self):
            return self._c.root

        @property
        def container(self):
            return self._c

        async def teardown(self):
            await self._c.teardown()

    c = _Box(c)
    session_id = uuid.uuid4().hex[:8]
    # B6/B5 共用：run_id -> 在跑的任务。取消端点据此 cancel；断连时清理，防孤儿任务。
    active_runs: dict[str, asyncio.Task] = {}

    def _sse(event: str, payload: dict) -> str:
        """一帧 SSE（data: {...}\\n\\n）。帧类型见设计方案 §4.5。"""
        return "data: " + json.dumps({"event": event, "payload": payload or {}},
                                     ensure_ascii=False) + "\n\n"

    async def _apply_rebuild() -> None:
        """按当前 config（含用户覆盖）+ 当前插件期望态重建容器（管理操作即时生效）。"""
        old = c._c
        c2 = build_agent(config=_merge_cfg(args), plugin_mgr=pm)
        await c2.assemble()
        await c2.start()
        try:
            await old.teardown()
        except Exception:  # noqa: BLE001
            pass
        c.set(c2)

    app = FastAPI(title="Agent Harness", docs_url=None, redoc_url=None)

    @app.get("/")
    async def _home():
        # no-store：前端改版后，防止浏览器用启发式缓存继续喂旧版 JS（曾导致"看起来没有流式"）
        return HTMLResponse(_PAGE, headers={"Cache-Control": "no-store"})

    @app.post("/api/chat")
    async def _chat(body: dict):
        try:
            reply = await c.root.chat.send(c.root, str(body.get("message") or ""),
                                           session_id=session_id)
            return JSONResponse({"text": reply.text, "ok": reply.ok, "error": reply.error,
                                 "evidence": {"plan": reply.plan, "evidence": reply.evidence,
                                              "tokens_in": reply.tokens_in,
                                              "tokens_out": reply.tokens_out}})
        except Exception as exc:  # noqa: BLE001 —— 回显给前端，不裸崩
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/chat/stream")
    async def _chat_stream(body: dict):
        """B5 流式：SSE 逐帧推送（start → progress… → final / error / cancelled）。

        与 B6 共用同一 run 生命周期：本端点持有任务并登记 run_id，取消端点据此 cancel。
        ``/api/chat`` 保留不动（--once 与离线测试依赖它）。
        """
        message = str(body.get("message") or "")
        run_id = uuid.uuid4().hex[:8]
        q: asyncio.Queue = asyncio.Queue()

        async def _worker() -> None:
            try:
                reply = await c.root.chat.send(c.root, message, session_id=session_id)
                await q.put({"event": "final", "payload": {
                    "text": reply.text, "ok": reply.ok, "error": reply.error,
                    "evidence": {"plan": reply.plan, "evidence": reply.evidence,
                                 "tokens_in": reply.tokens_in, "tokens_out": reply.tokens_out}}})
            except asyncio.CancelledError:
                await q.put({"event": "cancelled", "payload": {"run_id": run_id}})
                raise                                  # 保持取消语义，不吞
            except Exception as exc:                   # noqa: BLE001
                await q.put({"event": "error", "payload": {"error": str(exc)}})

        async def _gen():
            task: asyncio.Task | None = None
            try:
                yield _sse("start", {"run_id": run_id})
                # 先订阅（不漏帧），再起任务
                async with tap_progress(c.root, queue=q):
                    task = asyncio.create_task(_worker())
                    active_runs[run_id] = task
                    try:
                        while True:
                            frame = await q.get()
                            yield _sse(frame["event"], frame["payload"])
                            if frame["event"] in ("final", "error", "cancelled"):
                                return
                    finally:
                        active_runs.pop(run_id, None)
                        if task is not None and not task.done():
                            task.cancel()              # 断连/提前退出 → 取消，防孤儿
            except asyncio.CancelledError:
                return                                 # 客户端断连：不逃逸，任务已在 finally 取消
            except Exception as exc:                   # noqa: BLE001
                yield _sse("error", {"error": str(exc)})

        return StreamingResponse(_gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.post("/api/chat/cancel")
    async def _chat_cancel(body: dict):
        """B6 取消：按 run_id 取消在跑的那轮（`asyncio.Task.cancel()`，任意 await 点即时生效）。"""
        rid = str(body.get("run_id") or "")
        task = active_runs.get(rid)
        if task is None:
            return JSONResponse({"ok": False, "error": "run_id 不存在或该轮已结束"},
                                status_code=404)
        task.cancel()
        return JSONResponse({"ok": True, "run_id": rid})

    # ---- 审批（人在环中，第 2 期）：入站应答 + 在途快照 + 会话档位 ----
    # 解耦纪律：只经 Services.APPROVAL 门面（ctx.approval），不 import 插件内部模块。

    def _approval_or_none():
        if not c.root.has(Services.APPROVAL):
            return None
        return c.root.get(Services.APPROVAL)

    @app.post("/api/approval/answer")
    async def _approval_answer(body: dict):
        """人对在途请求的应答：审批（批/驳）与澄清问询（choice/text）共用。

        经待答表 resolve 唤醒在途等待者；附加键（answered/choice/text/note/
        always/kwargs）原样透传，由等待方按自身语义消费。
        未知/已了结的 call_id → 404（前端据此提示「已超时，默认拒绝」）。
        fail-safe 在插件侧：无人应答/超时一律默认拒绝，本端点只投喂不裁决。
        """
        svc = _approval_or_none()
        if svc is None:
            return JSONResponse({"ok": False, "error": "审批插件未启用"}, status_code=404)
        call_id = str(body.get("call_id") or "")
        if not call_id:
            return JSONResponse({"ok": False, "error": "缺少 call_id"}, status_code=400)
        answer: dict = {"approved": bool(body.get("approved")), "by": "user"}
        if body.get("always"):                       # 第 3 期：批量确认（本会话同类不再问）
            answer["always"] = True
        note = body.get("note")                      # 第 3 期：计划拒绝意见（回灌给模型）
        if isinstance(note, str) and note.strip():
            answer["note"] = note.strip()[:500]
        rw = body.get("kwargs")                      # 第 3 期：updated_input（参数修订）
        if isinstance(rw, dict):
            answer["kwargs"] = rw
        if "answered" in body:                       # 澄清问询：answered/choice/text 透传
            answer["answered"] = bool(body.get("answered"))
        if "choice" in body:
            answer["choice"] = body.get("choice")
        cs = body.get("choices")                     # 澄清问询（多选）：取值列表透传
        if isinstance(cs, list):
            answer["choices"] = [str(c)[:200] for c in cs][:24]
        if isinstance(body.get("text"), str):
            answer["text"] = body["text"][:2000]
        resolved = svc.pending.resolve(call_id, answer)
        if not resolved:
            return JSONResponse(
                {"ok": False, "resolved": False,
                 "error": "请求不存在或已了结（超时默认拒绝）"}, status_code=404)
        return JSONResponse({"ok": True, "resolved": True, "call_id": call_id})

    @app.get("/api/approval/pending")
    async def _approval_pending():
        """在途审批快照（页面刷新后恢复弹窗用）。"""
        svc = _approval_or_none()
        if svc is None:
            return {"items": [], "enabled": False}
        return {"items": svc.pending.snapshot(), "enabled": True,
                "mode": svc.mode()}

    @app.post("/api/approval/mode")
    async def _approval_mode(body: dict):
        """切换会话档位（default/plan/auto/bypass）。

        OPEN-5：切 bypass 属高危（跳过一切问人），需 ``confirm=true`` 二次确认。
        """
        svc = _approval_or_none()
        if svc is None:
            return JSONResponse({"ok": False, "error": "审批插件未启用"}, status_code=404)
        mode = str(body.get("mode") or "")
        if mode == "bypass" and not bool(body.get("confirm")):
            return JSONResponse(
                {"ok": False, "error": "bypass 将跳过所有审批，需二次确认"},
                status_code=409)
        try:
            await svc.set_mode(c.root, mode, by="user")
        except AgentError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "mode": svc.mode()})

    @app.get("/api/status")
    async def _status() -> dict:
        svc = c.root.get(Services.LLM)
        budget_cfg = ((c.root.config or {}).get("agent") or {}).get("budget") or {}
        return {"provider_label": getattr(svc, "provider_label", "?"),
                "model": getattr(svc, "model", "?"),
                "version": __import__("agent").__version__,
                "session_id": session_id,
                "budget": {"spent": c.root.state.get(StateKeys.BUDGET_SPENT, 0),
                           "max": budget_cfg.get("max_tokens_per_run", 0)}}

    @app.get("/api/events")
    async def _events() -> dict:
        rows = await c.root.db.query(
            c.root, "SELECT seq, event, payload, at FROM agent_events ORDER BY seq DESC LIMIT 120")
        return {"rows": rows}

    # ---- G2.0（P3-1）：Inspector ``/context`` 视图（纯只读旁路，不 import 插件内部）----

    @app.get("/api/context")
    async def _context_view(limit: int = 60) -> dict:
        """把 governed 遥测合并为**轮次全景账本**并渲染 ``/context`` 分类视图（薄封装）。"""
        return await _context_view_payload(c.root, limit=limit)

    @app.get("/api/perms")
    async def _perms() -> dict:
        recent = await c.root.db.query(
            c.root, "SELECT seq, payload, at FROM agent_events"
                    " WHERE event='agent/permission/decision' ORDER BY seq DESC LIMIT 30")
        approval = c.root.get(Services.APPROVAL) if c.root.has(Services.APPROVAL) else None
        return {"policy": c.root.guard.policy(c.root), "recent": recent,
                "mode": approval.mode() if approval else None}

    @app.get("/api/session")
    async def _session() -> dict:
        return {"session_id": session_id,
                "history": await c.root.chat.history(c.root, session_id)}

    @app.post("/api/session/reset")
    async def _session_reset():
        cleared = await c.root.chat.reset(c.root, session_id)
        return JSONResponse({"ok": True, "cleared": cleared})

    @app.get("/api/tools")
    async def _tools() -> dict:
        return {"tools": c.root.tasks.catalog()}

    # ---- 记忆（分层总览：系统 / 短期 / 工作 + 长期子块） ----
    # 解耦纪律：消费面只用 ctx.has(Services.X) 守卫 + 门面方法，不 import 任何插件内部模块。

    @app.get("/api/memory")
    async def _memory() -> dict:
        layers: list[dict] = []

        # 第1层 系统记忆（SYSTEM_MEMORY 提供的是纯文本字符串，无门面对象）
        if c.root.has(Services.SYSTEM_MEMORY):
            sm_text = c.root.get(Services.SYSTEM_MEMORY) or ""
            layers.append({"key": "system_memory", "name": "系统记忆",
                           "subtitle": "第1层 · 身份/边界叙事（启动全量注入）",
                           "available": True,
                           "summary": {"chars": len(sm_text),
                                       "preview": (sm_text[:240] + "…") if len(sm_text) > 240 else sm_text}})
        else:
            layers.append({"key": "system_memory", "name": "系统记忆",
                           "subtitle": "第1层 · 身份/边界叙事",
                           "available": False, "summary": {}})

        # 第2层 短期记忆（SHORT_TERM 门面 ShortTermMemory）
        if c.root.has(Services.SHORT_TERM):
            short = c.root.get(Services.SHORT_TERM)
            try:
                cnt = await short.count(c.root, session_id)
                meta = await short.summary_meta(c.root, session_id)
                covers = int(meta["covers_up_to_seq"]) if meta else 0
                latest = await short.latest_summary(c.root, session_id)
                st_stats = short.stats()
            except Exception:  # noqa: BLE001 —— 单窗诊断失败不影响整体展示
                cnt, covers, latest, st_stats = 0, 0, None, {}
            layers.append({"key": "short_term", "name": "短期记忆",
                           "subtitle": "第2层 · 会话转写 + 滚动摘要（当前会话）",
                           "available": True,
                           "summary": {"messages": cnt, "summary_covers_seq": covers,
                                       "has_summary": bool(latest),
                                       "latest_summary_preview": ((latest[:240] + "…") if latest and len(latest) > 240 else (latest or "")),
                                       "compact_stats": st_stats}})
        else:
            layers.append({"key": "short_term", "name": "短期记忆",
                           "subtitle": "第2层 · 会话转写 + 滚动摘要",
                           "available": False, "summary": {}})

        # 第3层 工作记忆（WORK_MEMORY 门面 WorkMemory）
        if c.root.has(Services.WORK_MEMORY):
            wm = c.root.get(Services.WORK_MEMORY)
            try:
                active = await wm.list_active(session_id)
            except Exception:  # noqa: BLE001
                active = []
            layers.append({"key": "work_memory", "name": "工作记忆",
                           "subtitle": "第3层 · 任务级进度缓存（Redis / 降级内存）",
                           "available": True,
                           "summary": {"active_tasks": active, "count": len(active)}})
        else:
            layers.append({"key": "work_memory", "name": "工作记忆",
                           "subtitle": "第3层 · 任务级进度缓存",
                           "available": False, "summary": {}})

        # 长期记忆（MEMORY 目录插件，opt-in）：独立子块，不影响三层展示
        long_term = {"enabled": c.root.has(Services.MEMORY), "stats": {}, "rows": []}
        if c.root.has(Services.MEMORY):
            try:
                long_term["stats"] = await c.root.memory.stats(c.root)
                long_term["rows"] = await c.root.memory.list_recent(c.root, limit=40)
            except Exception:  # noqa: BLE001
                long_term["stats"], long_term["rows"] = {"error": "stats failed"}, []

        return {"layers": layers, "long_term": long_term}

    # ---- 插件管理（引擎式：desired/actual/delta；改动即时重建生效） ----

    @app.get("/api/plugins")
    async def _plugins() -> dict:
        manifest = c.container.plugin_manifest()
        statuses = await c.container.plugin_statuses()
        status_map = {s["id"]: s.get("status") for s in statuses}
        discovered = pm.discovered()
        enabled = set(pm.enabled())
        assembled = {m["id"] for m in manifest}
        discovered_ids = {d["name"] for d in discovered}
        rows: list[dict] = []
        for d in discovered:
            e, a = d["name"] in enabled, d["name"] in assembled
            delta = ("enabled · assembled" if (e and a) else
                     ("enabled · 待重启装配" if (e and not a) else
                      ("disabled · 未装配" if (not e and not a) else "待停用")))
            # 行结构统一：目录插件与内置 manifest 两种来源都同时给 id + name（前端按键取 id）
            rows.append({**d, "id": d["name"], "desired": e, "actual": a, "delta": delta,
                         "status": status_map.get(d["name"])})
        for m in manifest:
            # 含 system 层插件一同展示（只读：前端对 delta=="system/builtin" 隐藏操作按钮）。
            # 仅按 discovered 去重，避免目录插件与其 manifest 项重复出现。
            if m["id"] in discovered_ids:
                continue
            rows.append({**m, "name": m["id"], "desired": True, "actual": True,
                         "delta": "system/builtin",
                         "status": status_map.get(m["id"])})
        return {"plugins": rows, "state_exists": pm.state_file.exists()}

    async def _bg_rebuild() -> None:
        """后台重建容器（不阻塞管理请求）。失败仅记录，不回滚期望态。"""
        try:
            await _apply_rebuild()
        except Exception as exc:  # noqa: BLE001
            print(f"[rebuild] 后台重建失败（期望态已保存，实际装配可能需重启）：{exc}",
                  file=sys.stderr)

    async def _mutate_then_fetch(background: bool = False) -> dict:
        # 先返回期望态已更新的当前快照（desired 已写入 state.json）。
        # 容器重建代价高（含 MCP 子进程拉起等，常需数十秒），故改为后台执行，
        # 避免管理请求长时间阻塞、前端“点了没反应”。前端会显示“处理中…”并在稍后自动刷新。
        res = await _plugins()
        if background:
            asyncio.create_task(_bg_rebuild())
            return res
        try:
            await _apply_rebuild()
            res = await _plugins()
        except Exception as exc:  # noqa: BLE001
            res = {**res, "rebuild_error": f"重建失败（期望态已保存，实际装配可能需重启）：{exc}"}
        return res

    @app.post("/api/plugins/enable")
    async def _plugin_enable(body: dict):
        pm.set_enabled(str(body.get("id") or ""), bool(body.get("enable", True)))
        # 无论重建成功与否都返回最新插件状态（含 plugins），保证前端即时刷新表格
        res = await _mutate_then_fetch(background=True)
        return JSONResponse({"ok": True, **res})

    @app.post("/api/plugins/scaffold")
    async def _plugin_scaffold(body: dict):
        info = pm.scaffold(str(body.get("name") or ""), body.get("provides") or None)
        res = await _mutate_then_fetch(background=True)
        return JSONResponse({"ok": True, "info": info, **res})

    @app.post("/api/plugins/remove")
    async def _plugin_remove(body: dict):
        info = pm.remove(str(body.get("id") or ""))
        res = await _mutate_then_fetch(background=True)
        return JSONResponse({"ok": True, "info": info, **res})

    # ---- 设置：上下文管理（前端「设置」页；覆盖持久化 + 即时重建生效） ----

    @app.get("/api/settings")
    async def _settings() -> dict:
        ctx_enabled = "plugins.context" in set(pm.enabled())
        ov = _load_overrides()
        values: dict = {}
        for k in _SETTING_SCHEMA:
            cur = (ov.get(_PLUGIN_OF_KEY.get(k, ""), {}) or {}).get(k, _SETTING_DEFAULTS[k])
            try:
                values[k] = _coerce_setting(k, cur)
            except (ValueError, TypeError):
                values[k] = _SETTING_DEFAULTS[k]
        ap_enabled = "plugins.approval" in set(pm.enabled())
        return {"context_plugin_enabled": ctx_enabled,
                "approval_enabled": ap_enabled, "values": values}

    @app.post("/api/settings")
    async def _settings_update(body: dict):
        try:
            if bool(body.get("reset")):
                ov = _load_overrides()
                for pid in ("plugins.context", "plugins.short_term", "plugins.approval",
                            "plugins.budget", "plugins.react"):
                    ov.pop(pid, None)
                _save_overrides(ov)
                await _apply_rebuild()
                return JSONResponse({"ok": True, "settings": await _settings()})
            patch = body.get("patch") or {}
            if not isinstance(patch, dict) or not patch:
                return JSONResponse({"ok": False, "error": "patch 为空"}, status_code=400)
            buckets: dict[str, dict] = {}
            for k, v in patch.items():
                if k not in _SETTING_SCHEMA:
                    return JSONResponse({"ok": False, "error": f"未知设置项: {k}"}, status_code=400)
                coerced = _coerce_setting(k, v)
                buckets.setdefault(_PLUGIN_OF_KEY[k], {})[k] = coerced
            ov = _load_overrides()
            for pid, b in buckets.items():
                cur = dict(ov.get(pid, {}))
                cur.update(b)
                ov[pid] = cur
            _save_overrides(ov)
            await _apply_rebuild()
            return JSONResponse({"ok": True, "settings": await _settings()})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": f"参数错误: {exc}"}, status_code=400)

    url = f"http://{args.host}:{args.port}"
    print(f"Agent Harness 就绪: {url}   （Ctrl+C 退出）", file=sys.stderr)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    task = asyncio.create_task(server.serve())
    try:
        await task
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await c.teardown()
    return 0


async def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    if args.once:
        return await _once(args)
    return await _serve(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))