"""agent.run.chat —— Agent 通用对话外设（web + 无头）。

前端参考 agent 对话形态（step/tool 轨迹、事件/权限/记忆/工具视图），**不特化为任何领域**：
通用能力为 ping / tool_call / chat；领域能力经「已启用的工具插件」（如 windows-mcp）挂入工具目录。

用法：
    python -m agent.run.chat                    # 网页（默认 127.0.0.1:8720，deepseek）
    python -m agent.run.chat --fixture           # 离线夹具（不动真 API）
    python -m agent.run.chat --once "你好"        # 无头跑一次

前端（B4 起）：页面已独立为 ``web/`` 工程（Vue3 + TS + Vite），本服务在
``_serve`` 末尾以 ``StaticFiles`` 单端口托管其构建产物 ``web/dist/``。
开发期仍可另起 Vite dev server（默认 5173/5174）经 proxy 转发 ``/api``。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
import uuid
from pathlib import Path

from agent.api import (AgentError, Services, SessionIdError, StateKeys,
                       make_identity)
from agent.profiles import BASE_AGENT_CONFIG, build_agent
from agent.sys.progress import tap_progress

# 本模块开了 ``from __future__ import annotations`` ⇒ 路由签名里的注解是**字符串**，
# FastAPI 用**模块全局**解析它们。故 ``Request``/``HTTPException`` 必须在模块级可见，
# 只在 create_app 内局部 import 会被当成普通查询参数（实测：路由报 `query.request`
# Field required，422）。fastapi 仍是**软依赖**（--once 无头形态不需要）：缺失时置 None。
try:
    from fastapi import HTTPException, Request          # noqa: F401
except Exception:                                       # pragma: no cover
    Request = None                                      # type: ignore[assignment]
    HTTPException = None                                # type: ignore[assignment]


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
    # ---- B3 前后端分离：跨域与鉴权（**均默认关闭，不改变既有行为**） ----
    p.add_argument("--cors-origin", action="append", default=[], metavar="ORIGIN",
                   help="允许跨域的来源（可多次，如 http://127.0.0.1:5174）；"
                        "留空 = 不启用 CORS 中间件")
    p.add_argument("--token", default="", metavar="TOKEN",
                   help="API 访问令牌；留空 = 不鉴权。启用后仅保护 `/api/*`，需 "
                        "`Authorization: Bearer <TOKEN>` 或 `?token=<TOKEN>`；"
                        "`/` 与 `/api/status` 始终放行（健康检查），"
                        "非 `/api/` 的前端静态产物亦放行（浏览器无法为其附加令牌）")
    p.add_argument("--hardened", action="store_true",
                   help="生产加固画像：权限 deny-by-default（task_allow=[]）+ 强制要求 --token；"
                        "默认不开启（本地 dev 与既有测试零影响）。")
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
    if getattr(args, "hardened", False):
        # 生产改造 #4：加固画像 —— deny-by-default，显式白名单才放行（不破坏其它配置节）。
        # build_agent 内的 deep_merge 递归合并，仅覆盖 permissions 的 default / task_allow，
        # 保留 task_deny / llm 等键。
        perms = agent.setdefault("permissions", {})
        perms["default"] = "deny"
        perms["task_allow"] = []
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
    # ---- 域级裁剪（plugins.react；路线 2：投影层按域裁剪工具暴露面）----
    # 硬前提：仅 lazy_tools=True 时生效（非懒加载无 tool_search/tool_load，
    # 裁剪即硬隐藏＝不可回溯）。空白名单视同未配置（不做全隐藏）。
    "tool_domains_enabled": ("bool",),
    "tool_domains": ("list",),                             # 放行域白名单（owner，如 plugins.windows_mcp）
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
    "tool_domains_enabled": "plugins.react",
    "tool_domains": "plugins.react",
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
    # 域级裁剪：与 profiles.BASE_AGENT_CONFIG agent.react 同源。
    # 陷阱：tool_domains 的默认值**必须是 []，不能是 None** —— _coerce_setting 的
    # "list" 分支对非 str/list 走 ``parts=[raw]``，None 会被收成字符串 "None"。
    "tool_domains_enabled": (((BASE_AGENT_CONFIG.get("agent") or {}).get("react") or {})
                             .get("tool_domains_enabled", False)),
    "tool_domains": (((BASE_AGENT_CONFIG.get("agent") or {}).get("react") or {})
                     .get("tool_domains", [])),
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


def _domain_options(tools: list[dict], selected: object) -> list[dict]:
    """可用工具域清单（供设置页以「可勾选」方式渲染 ``tool_domains``）。

    = 当前已注册域（附工具数） ∪ 配置里已选但当前未注册的域（``available=False``）。

    后半段是**必要**的而非补充：域随插件启用/停用而增减，若只渲染已注册域，
    「配置里已勾选、但该插件当前停用」的域会因控件不存在而在保存时被静默丢弃
    —— 用户会看到自己的选择无声消失。

    域 ≡ 工具注册来源 ``owner``（见 ``plugins/react/loop.py::_domain_of``）；
    ``owner`` 为空串的工具不计入清单（异常数据，不引导用户去勾选）。
    """
    counts: dict[str, int] = {}
    for t in tools:
        d = str(t.get("owner") or "").strip()
        if d:
            counts[d] = counts.get(d, 0) + 1
    opts = [{"name": d, "count": n, "available": True} for d, n in counts.items()]
    # 与 plugins/react/loop.py::allowed_domains 同款容忍：裸字符串包成单元素，
    # 否则会被逐字符迭代成一堆单字候选项；None/空串 ⇒ 无选中。
    if isinstance(selected, (list, tuple)):
        picked = list(selected)
    elif selected:
        picked = [selected]
    else:
        picked = []
    for raw in picked:
        d = str(raw).strip()
        if d and d not in counts:
            counts[d] = 0                      # 记账，防重复追加
            opts.append({"name": d, "count": 0, "available": False})
    opts.sort(key=lambda o: o["name"])
    return opts


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
#
# B4：前端已独立为 web/ 工程（Vue3 + TS + Vite），
# 服务端不再内联页面；改由 _serve() 末尾挂载 StaticFiles
# 托管 web/dist/（详释见 _serve 内 “B4”段挂载处）。
# 开发期仍走 Vite dev server + proxy，托管不参与。
# ---------------------------------------------------------------------------



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
    from fastapi import FastAPI, HTTPException, Request
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
    # 2.2：**不再有「进程级全局会话」**——原 ``session_id = uuid4().hex[:8]`` 是启动时生成
    # 一次的闭包常量，所有请求共用（多会话下即串数据）。现改为每请求经 ``_identity()`` 解析。
    # B6/B5 共用：run_id -> (在跑的任务, 归属会话键)。取消端点据此 cancel **并校验归属**
    # （H4：否则任一会话都能取消别人的 run）；断连时清理，防孤儿任务。
    active_runs: dict[str, tuple[asyncio.Task, str]] = {}

    # ---- 阶段 2（2.2）：请求级身份解析 -----------------------------------------
    # 信任边界：**tenant/user 只来自服务端配置**（客户端可伪造 ⇒ 不下放）；
    # session 来自请求头 ``X-Session-Id``（无隔离语义，伪造只影响伪造者自己）。
    # 键构造/净化的唯一真源是 ``core/session.py``——任何非法标识在此 fail-fast（400），
    # 不静默回退（否则用户以为在私密会话，实则落进共享桶 / 前缀扫描越权命中他人）。

    def _session_cfg() -> dict:
        return ((c.root.config or {}).get("agent") or {}).get("session") or {}

    def _identity(request: Request | None = None, body: dict | None = None):
        cfg = _session_cfg()
        raw = ""
        if request is not None:
            raw = str(request.headers.get("x-session-id") or "")
        if not raw and isinstance(body, dict):
            raw = str(body.get("session") or "")
        try:
            return make_identity(tenant=cfg.get("tenant"), user=cfg.get("user"),
                                 session=raw)
        except SessionIdError as exc:
            raise HTTPException(status_code=400, detail=f"非法会话标识：{exc}")

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

    # ---- B3.1 跨域（opt-in）------------------------------------------------
    # 默认「不启用」：沿用 dev 下的 Vite proxy（同源），零行为变化。
    # 前后端**直连**部署（前端静态托管在别的源）时显式传 --cors-origin 打开。
    # 注意：allow_credentials=True 时 **不可** 用 "*"（浏览器会拒绝），
    #       故此处按实际来源逐一回显——这也是「默认空 = 不启用」的另一层理由。
    if args.cors_origin:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(args.cors_origin),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            # SSE：浏览器 fetch 流读不暴露这些头也能用，但显式登记便于前端读取
            expose_headers=["Cache-Control", "X-Accel-Buffering"],
        )

    # ---- B3.2 访问令牌（opt-in）--------------------------------------------
    # 默认「不鉴权」：本地 dev 与既有测试零影响。
    # 启用后支持两种携带方式（**两种都要**：EventSource 无法自定义请求头，
    # SSE / 静态资源走 `?token=`；其余 fetch 走 `Authorization`）。
    _AUTH_EXEMPT = {"/", "/api/status"}       # 健康探针：首页与状态接口始终放行
    _token = str(args.token or "")

    def _extract_token(request) -> str:
        """按 `Authorization: Bearer x` → `?token=x` 顺序取令牌（headers 优先）。"""
        h = request.headers.get("authorization") or ""
        if h[:7].lower() == "bearer ":
            return h[7:].strip()
        return str(request.query_params.get("token") or "")

    if _token:
        import hmac

        from fastapi import Request
        from fastapi.responses import JSONResponse as _JSONResponse

        @app.middleware("http")
        async def _auth_mw(request: Request, call_next):
            path = request.url.path
            # 三类放行：
            #   1) OPTIONS 预检 —— CORS 预检不带 Authorization（浏览器规范），拦了 CORS 全废；
            #   2) 显式豁免的探针 —— `/` 首页与 `/api/status`；
            #   3) **非 `/api/` 路径** —— B4 起这些是前端静态产物（index.html / assets/*）。
            #      浏览器用 <script>/<link> 加载它们时**无法携带 token**，若拦则整页白屏；
            #      且静态产物本就是发给浏览器的公开资源，不构成额外信息泄露。
            #      真正的数据面（/api/*）一律照旧校验，安全强度不变。
            if (request.method == "OPTIONS"
                    or path in _AUTH_EXEMPT
                    or not path.startswith("/api/")):
                return await call_next(request)
            # 常量时间比较：避免按字符计时侧信道（本地/内网场景亦无理由削弱）
            if not hmac.compare_digest(_extract_token(request), _token):
                return _JSONResponse({"error": "未授权：缺少或无效的 token"},
                                     status_code=401)
            return await call_next(request)

    # ---- 生产改造 #2：健康/就绪/指标探针（顶层路径，免 /api/ 鉴权）----
    # 必须在 SPA mount("/")（约 904 行）之前注册，否则被 catch-all 吞成 index.html。
    # 顶层路径（非 /api/）即便启用 --token 也由中间件豁免（见 _AUTH_EXEMPT：非 /api/ 直接放行）。
    from datetime import datetime, timezone
    from fastapi.responses import Response

    @app.get("/health")
    async def _health() -> dict:
        """liveness 探针：恒 200，不依赖任何内部状态（K8s livenessProbe）。"""
        return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}

    @app.get("/ready")
    async def _ready():
        """readiness 探针：只查硬依赖（DB 可达 + 核心服务已注册），不查可选插件层。
        用 container.has 查**注册态**，不触发懒加载实例化；任一硬依赖缺失即 503。"""
        try:
            await c.root.db.query(c.root, "SELECT 1")
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"status": "degraded", "reason": f"db unreachable: {exc}"},
                                status_code=503)
        for svc in (Services.LLM, Services.REACT, Services.GUARD):
            if not c.root.has(svc):
                return JSONResponse({"status": "degraded", "reason": f"{svc} not registered"},
                                    status_code=503)
        return {"status": "ok"}

    @app.get("/metrics")
    async def _metrics() -> Response:
        """极简 Prometheus 文本指标（核心指标；多会话后加 tenant/user label）。"""
        budget_cfg = ((c.root.config or {}).get("agent") or {}).get("budget") or {}
        spent = c.root.state.get(StateKeys.BUDGET_SPENT, 0)
        catalog = c.root.tasks.catalog()
        tool_total = len(catalog)
        # TODO(multi-session): 加 tenant/user label，当前仅全局 session_id
        from collections import Counter
        owners = Counter(t.get("owner", "") for t in catalog)
        lines = [
            "# HELP agent_up 进程存活",
            "# TYPE agent_up gauge",
            "agent_up 1",
            "# HELP agent_budget_spent 已用 token",
            "# TYPE agent_budget_spent gauge",
            f"agent_budget_spent {spent}",
            "# HELP agent_budget_max token 上限",
            "# TYPE agent_budget_max gauge",
            f"agent_budget_max {budget_cfg.get('max_tokens_per_run', 0)}",
            "# HELP agent_runs_active 在跑任务数",
            "# TYPE agent_runs_active gauge",
            f"agent_runs_active {len(active_runs)}",
            "# HELP agent_tools_total 工具总数",
            "# TYPE agent_tools_total gauge",
            f"agent_tools_total {tool_total}",
        ]
        for owner, n in sorted(owners.items()):
            if owner:
                lines.append(f'agent_tools_by_owner{{owner="{owner}"}} {n}')
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.post("/api/chat")
    async def _chat(body: dict, request: Request):
        ident = _identity(request, body)
        try:
            reply = await c.root.chat.send(c.root, str(body.get("message") or ""),
                                           session_id=ident.session_key,
                                           identity=ident)
            return JSONResponse({"text": reply.text, "ok": reply.ok, "error": reply.error,
                                 "notice": reply.notice,
                                 "evidence": {"plan": reply.plan, "evidence": reply.evidence,
                                              "tokens_in": reply.tokens_in,
                                              "tokens_out": reply.tokens_out}})
        except Exception as exc:  # noqa: BLE001 —— 回显给前端，不裸崩
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.post("/api/chat/stream")
    async def _chat_stream(body: dict, request: Request):
        """B5 流式：SSE 逐帧推送（start → progress… → final / error / cancelled）。

        与 B6 共用同一 run 生命周期：本端点持有任务并登记 run_id（**带归属会话**），
        取消端点据此 cancel 并校验归属。``/api/chat`` 保留不动（--once 与离线测试依赖它）。

        2.2：会话标识经 ``X-Session-Id`` 请求头（本端点是 fetch POST，可自定义头；
        与 EventSource 不同，无需退化到 query）。
        """
        ident = _identity(request, body)
        message = str(body.get("message") or "")
        run_id = uuid.uuid4().hex[:8]
        q: asyncio.Queue = asyncio.Queue()

        async def _worker() -> None:
            try:
                reply = await c.root.chat.send(c.root, message,
                                               session_id=ident.session_key,
                                               identity=ident)
                await q.put({"event": "final", "payload": {
                    "text": reply.text, "ok": reply.ok, "error": reply.error,
                    "notice": reply.notice,
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
                    active_runs[run_id] = (task, ident.session_key)
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
    async def _chat_cancel(body: dict, request: Request):
        """B6 取消：按 run_id 取消在跑的那轮（`asyncio.Task.cancel()`，任意 await 点即时生效）。

        2.2（H4）：**校验归属**——run 只可被它所属的会话取消；跨会话取消返回 403
        （否则任一会话凭 run_id 就能掐掉别人的任务）。
        """
        ident = _identity(request, body)
        rid = str(body.get("run_id") or "")
        entry = active_runs.get(rid)
        if entry is None:
            return JSONResponse({"ok": False, "error": "run_id 不存在或该轮已结束"},
                                status_code=404)
        task, owner = entry
        if owner != ident.session_key:
            return JSONResponse({"ok": False, "error": "该 run 不属于当前会话，无权取消"},
                                status_code=403)
        task.cancel()
        return JSONResponse({"ok": True, "run_id": rid})

    # ---- 审批（人在环中，第 2 期）：入站应答 + 在途快照 + 会话档位 ----
    # 解耦纪律：只经 Services.APPROVAL 门面（ctx.approval），不 import 插件内部模块。

    def _approval_or_none():
        if not c.root.has(Services.APPROVAL):
            return None
        return c.root.get(Services.APPROVAL)

    @app.post("/api/approval/answer")
    async def _approval_answer(body: dict, request: Request):
        """人对在途请求的应答：审批（批/驳）与澄清问询（choice/text）共用。

        经待答表 resolve 唤醒在途等待者；附加键（answered/choice/text/note/
        always/kwargs）原样透传，由等待方按自身语义消费。
        未知/已了结的 call_id → 404（前端据此提示「已超时，默认拒绝」）。
        fail-safe 在插件侧：无人应答/超时一律默认拒绝，本端点只投喂不裁决。

        2.2（H2）：**校验归属**——只能应答**本会话**的在途请求，跨会话应答返回 403
        （此前凭 call_id 可应答任意会话的调用，等于替别人批准工具执行）。
        """
        svc = _approval_or_none()
        if svc is None:
            return JSONResponse({"ok": False, "error": "审批插件未启用"}, status_code=404)
        ident = _identity(request, body)
        call_id = str(body.get("call_id") or "")
        if not call_id:
            return JSONResponse({"ok": False, "error": "缺少 call_id"}, status_code=400)
        owner = svc.pending.owner_of(call_id)
        if owner is not None and owner != ident.session_key:
            return JSONResponse(
                {"ok": False, "error": "该请求不属于当前会话，无权应答"}, status_code=403)
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
        resolved = svc.pending.resolve(call_id, answer, session=ident.session_key)
        if not resolved:
            return JSONResponse(
                {"ok": False, "resolved": False,
                 "error": "请求不存在或已了结（超时默认拒绝）"}, status_code=404)
        return JSONResponse({"ok": True, "resolved": True, "call_id": call_id})

    @app.get("/api/approval/pending")
    async def _approval_pending(request: Request):
        """在途审批快照（页面刷新后恢复弹窗用）。

        2.2（H2）：**只返回本会话**的在途请求（此前返回全局，B 能看见 A 的待审批项）。
        """
        ident = _identity(request)
        svc = _approval_or_none()
        if svc is None:
            return {"items": [], "enabled": False}
        return {"items": svc.pending.snapshot(ident.session_key), "enabled": True,
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
    async def _status(request: Request) -> dict:
        ident = _identity(request)
        svc = c.root.get(Services.LLM)
        budget_cfg = ((c.root.config or {}).get("agent") or {}).get("budget") or {}
        return {"provider_label": getattr(svc, "provider_label", "?"),
                "model": getattr(svc, "model", "?"),
                "version": __import__("agent").__version__,
                "session_id": ident.session_key,
                # 2.2：回显身份（tenant/user 服务端配置决定，便于前端确认自己落在哪个桶）
                "tenant": ident.tenant, "user": ident.user, "session": ident.session,
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
    async def _session(request: Request) -> dict:
        """当前会话（2.2：按请求身份取，**不再**是进程级全局会话）。"""
        ident = _identity(request)
        return {"session_id": ident.session_key, "tenant": ident.tenant,
                "user": ident.user,
                "history": await c.root.chat.history(c.root, ident.session_key)}

    @app.post("/api/session/reset")
    async def _session_reset(request: Request):
        """清空**当前会话**的转写（2.2：不再清空全局——那会连别人的会话一起清掉）。"""
        ident = _identity(request)
        cleared = await c.root.chat.reset(c.root, ident.session_key)
        return JSONResponse({"ok": True, "cleared": cleared})

    @app.get("/api/tools")
    async def _tools() -> dict:
        return {"tools": c.root.tasks.catalog()}

    # ---- 记忆（分层总览：系统 / 短期 / 工作 + 长期子块） ----
    # 解耦纪律：消费面只用 ctx.has(Services.X) 守卫 + 门面方法，不 import 任何插件内部模块。

    @app.get("/api/memory")
    async def _memory(request: Request) -> dict:
        ident = _identity(request)
        sid = ident.session_key                    # 2.2：各层按**当前会话**统计
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
                cnt = await short.count(c.root, sid)
                meta = await short.summary_meta(c.root, sid)
                covers = int(meta["covers_up_to_seq"]) if meta else 0
                latest = await short.latest_summary(c.root, sid)
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
                active = await wm.list_active(sid)
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
                "approval_enabled": ap_enabled, "values": values,
                # 域清单：设置页据此把 tool_domains 渲染为「可勾选」而非手填
                "domains": _domain_options(c.root.tasks.catalog(),
                                           values.get("tool_domains"))}

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

    # ---- B4：单端口托管前端产物（web/dist） ------------------------------------
    # 放在**全部 /api/* 路由注册之后**：Starlette 按注册顺序匹配，先注册者优先。
    # 故 `/api/*` 照旧命中上面的路由，只有剩余路径落到这里。
    #
    # 开发期不受影响：日常开发仍走 Vite dev server（5174）+ proxy，本挂载只在
    # 「单端口部署」形态下生效；产物缺失时给出**明确指引**而非静默 404。
    _DIST = Path(__file__).resolve().parents[2] / "web" / "dist"

    if _DIST.is_dir():
        from fastapi.staticfiles import StaticFiles

        class _SPAStaticFiles(StaticFiles):
            """StaticFiles + 对 HTML 响应加 ``Cache-Control: no-store``。

            为什么必须 no-store：产物文件名带**内容哈希**（``index-<hash>.js``）。
            浏览器若启发式缓存了 ``index.html``，改版后会继续引用**已不存在的旧 hash
            文件** → 整页白屏。B2 期实测过同类问题（旧 JS 被缓存导致"看起来没有流式"）。
            带哈希的静态资源本身可长缓存，故只干预 text/html。
            """

            async def get_response(self, path, scope):  # type: ignore[override]
                resp = await super().get_response(path, scope)
                ctype = resp.headers.get("content-type", "")
                if ctype.startswith("text/html"):
                    resp.headers["Cache-Control"] = "no-store"
                return resp

        app.mount("/", _SPAStaticFiles(directory=str(_DIST), html=True), name="web")
        print(f"[web] 托管前端产物：{_DIST}", file=sys.stderr)
    else:
        @app.get("/")
        async def _home_missing():
            # 不静默 404：告诉使用者缺什么、怎么补（避免"页面打不开但不知为啥"）
            return HTMLResponse(
                "<h1>前端产物缺失</h1>"
                "<p>后端未能找到 <code>web/dist/</code>。请先构建前端：</p>"
                "<pre>cd web &amp;&amp; npm install &amp;&amp; npm run build</pre>"
                "<p>开发期也可直接使用 Vite dev server（默认 5173/5174），"
                "它经 proxy 转发 <code>/api</code> 到本服务。</p>",
                status_code=503)

        print(f"[web] 未找到前端产物 {_DIST} —— 首页将返回 503 与构建指引",
              file=sys.stderr)

    # ---- 生产改造 #4：安全姿态告警 ----
    # 无论是否 hardened，只要「无 token 且权限开放」即打印醒目 WARNING（部署风险）。
    # hardened 模式下若仍缺 token，deny-by-default 会让所有工具调用（含 chat）被拒，
    # 故额外强告警提示必须同时提供 --token。
    _perms_cfg = ((c.root.config or {}).get("agent") or {}).get("permissions") or {}
    if _perms_cfg.get("default") != "deny":
        if _token:
            logging.getLogger("agent.chat").warning(
                "权限默认开放（default=open）但已启用 token：匿名访问已挡，但工具调用仍走 "
                "guard 默认放行；生产加固请用 --hardened 开启 deny-by-default。")
        else:
            logging.getLogger("agent.chat").warning(
                "未启用 token 且权限默认开放（default=open）：部署到网络即任何人可写插件/删文件。"
                "生产环境请加 --token 并启用 --hardened。")
    if getattr(args, "hardened", False) and not _token:
        logging.getLogger("agent.chat").warning(
            "--hardened 已启用但未设置 --token：deny-by-default 下所有工具调用将被拒绝"
            "（包括 chat）。请务必同时提供 --token。")

    url = f"http://{args.host}:{args.port}"
    print(f"Agent Harness 就绪: {url}   （Ctrl+C 退出）", file=sys.stderr)
    config = uvicorn.Config(app, host=args.host, port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # 禁用 uvicorn 自带信号处理器，下面自己接管
    task = asyncio.create_task(server.serve())

    # ---- #1 SIGTERM/SIGINT 优雅退出（容器/K8s/systemd 默认发 SIGTERM） ----
    # uvicorn 自带处理器已被禁用，这里注册双信号；收到后设置 should_exit 并取消 serve 任务，
    # 使 finally 中的 c.teardown()（含 MCP 子进程回收）一定执行，避免孤儿进程累积。
    loop = asyncio.get_running_loop()
    _svc_log = logging.getLogger("agent.chat")

    def _graceful() -> None:
        _svc_log.warning("received shutdown signal, tearing down (SIGTERM/SIGINT)")
        server.should_exit = True
        # 即便 serve() 正阻塞在 accept，也强制取消该任务以确保退出路径被触发
        loop.call_soon_threadsafe(task.cancel)

    try:
        # POSIX：handler 直接调度在事件循环线程内执行
        loop.add_signal_handler(signal.SIGTERM, _graceful)
        loop.add_signal_handler(signal.SIGINT, _graceful)
        _sig_mode = "loop.add_signal_handler"
    except (NotImplementedError, RuntimeError):
        # Windows：asyncio 不支持 add_signal_handler（NotImplementedError）。
        # 回退 signal.signal：handler 在主线程同步执行，必须经 call_soon_threadsafe
        # 把置位动作扔回事件循环线程，否则正 await server.serve() 的循环不会被唤醒。
        signal.signal(signal.SIGTERM, lambda *_: _graceful())
        signal.signal(signal.SIGINT, lambda *_: _graceful())
        # 控制台下 CTRL_BREAK / taskkill 不带 /F（CTRL_CLOSE_EVENT）走 SIGBREAK，一并接管
        try:
            signal.signal(signal.SIGBREAK, lambda *_: _graceful())  # type: ignore[attr-defined]
        except (ValueError, OSError, AttributeError):
            pass
        _sig_mode = "signal.signal"
    _svc_log.info("shutdown signal handlers installed (%s)", _sig_mode)
    # ---- /#1 ----

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