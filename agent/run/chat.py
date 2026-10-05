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
import os
import signal
import time
import uuid
from pathlib import Path

from agent.api import (AgentError, SESSION_CFG_VIEW, Services, SessionIdError,
                       StateKeys, bind_request_id, configure_logging,
                       counters_snapshot, current_request_id, inc_counter,
                       make_identity, set_current_session_key)
from agent.profiles import BASE_AGENT_CONFIG, build_agent
from agent.run.config_store import ConfigStore
from agent.sys.progress import tap_progress

#: 服务器入口的 SQLite 库路径（``--db`` 默认��）。
#:
#: ⚠️ **刻意不同于** ``core/config_schema.DEFAULT_DB_PATH``（``data/agent.db``），不是笔误：
#:   - 后者服务 ``build_agent()`` 的离线/嵌入式用法（含 ``test_event_wiring`` 等不传
#:     ``config`` 的装配）——它们会真实连上项目根的库文件；
#:   - 长驻服务器若共用同一个文件，会与上述用法争 WAL、且被离线写入污染服务端账本。
#: 两库 schema 完全相同（同一套 ``MIGRATIONS``），换入口即可，无需迁移。
DEFAULT_CHAT_DB_PATH = "data/agent_chat.db"

#: SSE 帧队列容量（H4，2026-10-04）。与 ``sys/progress.py::tap_progress`` 的自建默认值
#: 保持一致（1024）：token 高频流 + 慢消费者不再无限占内存。
#: 满了走既有丢帧策略（丢最旧保新帧 + ``queue.dropped`` 计数），**不阻断业务主链路**。
_STREAM_QUEUE_MAXSIZE = 1024

#: 会话 run 登记锁（H2-TOCTOU 修复，2026-10-04）：``check-busy → register`` 必须原子，
#: 否则同会话并发两条请求可双双通过忙检、各自登记 ⇒ 两个 run 抢同一会话链互相覆盖。
#: 放在模块级：本服务进程内单实例，无需按容器分桶。
_RUN_LOCK = asyncio.Lock()

#: 后台容器重建任务登记（`_bg_rebuild` 经 `create_task` 发起）：仅用于退出时观测/排障，
#: 不阻塞主链路；任务完成自动从集合移除。
_BG_TASKS: set[asyncio.Task] = set()

#: 路由处理器日志器。★ **必须模块级**：`_register_probes`/`_register_approval` 等是
#: **模块级函数**，其内部 handler（`/api/chat` 的错误分支等）引用本名——此前只在 `_serve`
#: 内定义局部 `_err_log`，机械搬移路由到模块级函数时漏了这条依赖 ⇒ 那些错误分支调用时
#: `NameError`（AgentError 的面向用户消息被通用 500 顶掉）。提到模块级后两处同源。
_err_log = logging.getLogger("agent.chat")

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
    p.add_argument("--db", default=DEFAULT_CHAT_DB_PATH)
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
    # ---- #5 可观测性（**默认开启**；不改功能行为，只新增日志输出——此前全仓无 handler，
    #      INFO 级日志被静默丢弃，运维等于瞎的） --------------------------------
    p.add_argument("--log-level", default="INFO",
                   help="日志级别（DEBUG/INFO/WARNING/ERROR；默认 INFO）")
    p.add_argument("--log-format", default="text", choices=("text", "json"),
                   help="text=人读（行内含 rid/session/run）；json=单行 JSON 供采集侧解析")
    args = p.parse_args(argv)
    # 生产加固：token 与 hardened 支持**环境变量**注入（镜像/compose 默认形态不必把密钥写进 CMD）。
    # 显式命令行参数优先；env 只在未传时兜底。
    if not args.token:
        args.token = os.environ.get("PRISM_TOKEN", "")
    if not args.hardened and os.environ.get("PRISM_HARDENED", "").strip().lower() in (
            "1", "true", "on", "yes"):
        args.hardened = True
    return args


def _cfg(args: argparse.Namespace) -> dict:
    provider = "fixture" if args.fixture else args.provider
    if provider == "fixture":
        return {"db_path": args.db, "agent": {"llm": {"provider": "fixture"}}}
    return {"db_path": args.db,
            "agent": {"llm": {"provider": provider, "model": args.model}}}


# ---- 插件配置覆盖（前端「设置」持久化；与插件启停期望态分离，单文件存储） ----
# 会话级化（G2）：覆盖文件升级为 v2 分 scope 结构（container / sessions），
# 读写一律经 ``ConfigStore``（v1 平铺只读兼容 + 写前备份 + LRU 上限）。
# 两个旧函数名保留为薄封装 —— 语义不变（容器级整体读写），避免全仓调用点改动。
_OVERRIDE_FILE = Path("data/agent_plugin_config.json")

# 进程内唯一真源：``_merge_cfg`` / 设置接口 / ``session.cfg_view`` 接缝共用同一份。
# 惰性访问：``_OVERRIDE_FILE`` 可能被重定向（测试注入 tmp 路径 / 多环境部署），
# 直接固化路径会让重定向失效——故按「路径变了就重建」保持单一真源。
_STORE = ConfigStore(path=_OVERRIDE_FILE)


def _store() -> ConfigStore:
    """当前覆盖存储（路径变化即重建；单一真源语义不变）。"""
    global _STORE
    if Path(_STORE.path) != Path(_OVERRIDE_FILE):
        _STORE = ConfigStore(path=_OVERRIDE_FILE)
    return _STORE


def _load_overrides() -> dict:
    """读取用户对插件运行时配置的**容器级**覆盖（v1 形状 ``{plugins.X: {...}}``）。"""
    return _store().container()


def _save_overrides(data: dict) -> None:
    """整份写回容器级覆盖（v1 形状）。"""
    _store().replace_container(data)


# 覆盖节名 → ``config["agent"]`` 下的**实际配置节**的特例表。
# 通用规则是同名（``plugins.<名>`` → ``agent.<名>``），但 ``plugins.guard`` 是例外：
# guard 是**系统层硬装配**插件（不在目录插件之列），其矩阵的 SSOT 节叫 ``permissions``
# （``core/config_schema.py`` 的严格键集节，见 ``sys/guard.py::_matrix``）。
# 2026-10-03 策略收口后设置页的 ``targets``/``default``/``allow``/``deny`` 改挂 ``plugins.guard``，
# 若无此特例它们会落到不存在的 ``agent.guard`` ⇒ 静默失效（已实测：会抛 ConfigError）。
# ⚠️ 这条特例是**显式**的：通用规则保持干净，不让任何插件去猜自己的节名。
_SECTION_ALIAS: dict[str, str] = {"plugins.guard": "permissions"}


def _merge_cfg(args: argparse.Namespace) -> dict:
    """基础 config（``_cfg``）+ 用户覆盖，注入 build_agent。

    覆盖按 ``plugins.<名>`` → ``agent.<名>`` 映射并入对应插件配置节（均为 ``_any``，
    免改 schema）；``_SECTION_ALIAS`` 里的节名走特例（见其 docstring）。
    """
    cfg = _cfg(args)
    ov = _load_overrides()
    agent = cfg.setdefault("agent", {})
    for pid, pconf in (ov or {}).items():
        if not isinstance(pid, str) or not pid.startswith("plugins."):
            continue
        if not isinstance(pconf, dict):
            continue
        base = _SECTION_ALIAS.get(pid, pid[len("plugins."):])
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


# ---- 设置项元数据（**单一真相源**，前端「设置」页可调） ----
# G2（会话级化，S5-0）：校验/归属/默认/作用域**四件事实合并为一张表**。
# 此前散在 _SETTING_SCHEMA / _PLUGIN_OF_KEY / _SETTING_DEFAULTS 三处（外加一条
# 靠注释维持的同源纪律），加 scope 维度后三表 + scope 必然漂移——故先合并再扩展。
# 每键四项：
#   spec    : 校验规则（见 _coerce_setting；``(类型, 下, 上)`` / ``("choice", [...])`` / ``("bool",)`` / ``("list",)``）
#   plugin  : 归属插件节——决定落 ``plugins.<名>``（_merge_cfg 再映射到 ``agent.<名>``）
#   default : 默认值（与 profiles.BASE_AGENT_CONFIG 同源，避免硬编码漂移）
#   scope   : 允许写入的作用域层。``"session"`` = 可写到「本会话」（G2）；
#             ``"container"`` = 仅容器级（改装配/资源/进程语义，会话化无意义或有害，见方案 §3.2）。
# G2.0（P1）：context 节默认值单一真相源（与 profiles.BASE_AGENT_CONFIG 同源，避免漂移）
_CTX_DEFAULTS = ((BASE_AGENT_CONFIG.get("agent") or {}).get("context") or {})
_BUDGET_DEFAULTS = ((BASE_AGENT_CONFIG.get("agent") or {}).get("budget") or {})
_REACT_DEFAULTS = ((BASE_AGENT_CONFIG.get("agent") or {}).get("react") or {})

_SETTING_META: dict[str, dict] = {
    # ---- 短期记忆（plugins.short_term）----
    "compact_trigger_tokens": {
        "spec": (int, 256, 1_000_000), "plugin": "plugins.short_term",
        "default": 12000, "scope": "session"},
    # ---- 上下文窗口与配比（plugins.context）----
    "max_context_tokens": {
        "spec": (int, 1024, 1_000_000), "plugin": "plugins.context",
        "default": 24000, "scope": "session"},
    "reserve_output_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": 4000, "scope": "session"},
    "memory_ratio": {
        "spec": (float, 0.01, 0.99), "plugin": "plugins.context",
        "default": 0.10, "scope": "session"},
    "history_ratio": {
        "spec": (float, 0.01, 0.99), "plugin": "plugins.context",
        "default": 0.60, "scope": "session"},
    "source_clip_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": 8192, "scope": "session"},
    # ---- S3：循环内（react）上下文治理 ----
    "clip_max_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": 1000, "scope": "session"},          # 单条观察超此值即首尾折叠
    "clip_head_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": 500, "scope": "session"},           # 折叠后头部保留
    "clip_tail_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": 500, "scope": "session"},           # 折叠后尾部保留
    "react_keep_recent": {
        "spec": (int, 0, 1000), "plugin": "plugins.context",
        "default": 6, "scope": "session"},             # 最近 N 条链原文保留
    "react_min_chain_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": 2048, "scope": "session"},          # 循环内 chain 保底额度
    # ---- G2.0（P1）：双窗口解耦 + 水位 + 层 cap ----
    "working_window_tokens": {
        "spec": (int, 1, 1_000_000), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("working_window_tokens", 96000),
        "scope": "session"},                           # 经济窗口（水位分母）
    "window_high_ratio": {
        "spec": (float, 0.01, 0.99), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("window_high_ratio", 0.70),
        "scope": "session"},                           # 触发水位
    "window_low_ratio": {
        "spec": (float, 0.01, 0.99), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("window_low_ratio", 0.40),
        "scope": "session"},                           # 回落目标（记录用）
    "resident_cap_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("resident_cap_tokens", 8000),
        "scope": "session"},                           # 常驻层 cap（0=无）
    "on_demand_cap_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("on_demand_cap_tokens", 8000),
        "scope": "session"},                           # 按需层 cap（0=无）
    "prompt_cap_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("prompt_cap_tokens", 2000),
        "scope": "session"},                           # 输入层 cap（0=无）
    # ---- G2.0（P2）：治理动词（模式 + clear 最小释放量）----
    "compact_mode": {
        "spec": ("choice", ["off", "shadow", "on"]), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("compact_mode", "shadow"),
        "scope": "session"},                           # 默认 shadow（只算不做）
    "clear_at_least_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("clear_at_least_tokens", 4000),
        "scope": "session"},                           # 单次清除的最小释放量
    # ---- 审批插件（plugins.approval）：通道 / 超时 / 必问名单 ----
    # 容器级：provider/timeout 属「装配与运行通道」语义（改 provider 要重装通道），
    # 会话化只会让不同会话的审批行为不可预期；名单的会话级化留后续批次。
    "provider": {
        "spec": ("choice", ["auto-deny", "auto-allow", "web", "cli"]),
        "plugin": "plugins.approval", "default": "web", "scope": "container"},
    "timeout_s": {
        "spec": (int, 1, 3600), "plugin": "plugins.approval",
        "default": 30, "scope": "container"},
    "ask": {
        "spec": ("list",), "plugin": "plugins.approval",
        "default": [], "scope": "container"},          # fnmatch 工具名名单
    # ---- 成本护栏（agent.budget 内核托管节；经 plugins.budget 覆盖映射，见 _merge_cfg）----
    "max_tokens_per_run": {
        "spec": (int, 1000, 10_000_000), "plugin": "plugins.budget",
        "default": _BUDGET_DEFAULTS.get("max_tokens_per_run", 200000),
        "scope": "session"},                           # 本会话累计 LLM token 上限
    "finalize_reserve_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.budget",
        "default": _BUDGET_DEFAULTS.get("finalize_reserve_tokens", 0),
        "scope": "session"},                           # 方案A：收尾预留额度（收尾专用）
    "cost_calibration": {
        "spec": ("bool",), "plugin": "plugins.budget",
        "default": _BUDGET_DEFAULTS.get("cost_calibration", True),
        "scope": "container"},                         # F1：口径校准是模型属性，不随会话拆
    # ---- 工具懒加载（plugins.react；L1 灰度开关）----
    # 容器级：属「暴露层装配」开关，会话化会让同一模型的工具面在不同会话里漂移。
    "lazy_tools": {
        "spec": ("bool",), "plugin": "plugins.react",
        "default": _REACT_DEFAULTS.get("lazy_tools", False), "scope": "container"},
    # ---- 域级裁剪（plugins.react；路线 2：投影层按域裁剪工具暴露面）----
    # 硬前提：仅 lazy_tools=True 时生效（非懒加载无 tool_search/tool_load，
    # 裁剪即硬隐藏＝不可回溯）。空白名单视同未配置（不做全隐藏）。
    "tool_domains_enabled": {
        "spec": ("bool",), "plugin": "plugins.react",
        "default": _REACT_DEFAULTS.get("tool_domains_enabled", False),
        "scope": "container"},
    # 陷阱：tool_domains 的默认值**必须是 []，不能是 None** —— _coerce_setting 的
    # "list" 分支对非 str/list 走 ``parts=[raw]``，None 会被收成字符串 "None"。
    "tool_domains": {
        "spec": ("list",), "plugin": "plugins.react",
        "default": _REACT_DEFAULTS.get("tool_domains", []),
        "scope": "container"},                         # 放行域白名单（owner，如 plugins.windows_mcp）
    # ---- P0-2：续接前缀对齐（plugins.react；头部快照冻结，可回退）----
    "alignment_enabled": {
        "spec": ("bool",), "plugin": "plugins.react",
        "default": _REACT_DEFAULTS.get("alignment_enabled", True),
        "scope": "container"},
    # ---- 准入名单四键（2026-10-03 策略收口：plugins.sandbox → plugins.guard）----
    # 键名取**裸名**（与 approval 的 ask/provider 同款）：设置层是扁平单值命名空间，
    # 由 _PLUGIN_OF_KEY 保证不歧义。``risk`` 刻意**不进设置页**——已核实 MCP 工具
    # 全部是 write 档（两处 register 都没传 risk），放上去是个无效旋钮，只走配置文件。
    #
    # ⚠️ 归属改为 ``plugins.guard``：准入策略的**唯一持有者**是 guard（``sys/guard.py``，
    # 它同时是 ``LLM_PRE_CALL`` 的唯一挂载者）。此前挂 ``plugins.sandbox`` 造成两份名单
    # 并存、互相打洞（见 docs/…/沙箱与权限管理器重叠核定-2026-10-03.md）。
    # 落到 ``agent.permissions`` 靠 ``_SECTION_ALIAS`` 特例（见其 docstring）。
    # ``targets`` 默认从 ``["mcp.*"]`` 改为 ``["*"]``：与 ``sys.guard.DEFAULT_POLICY``
    # 同源（收口前 guard 管辖全部工具 ⇒ 行为逐位不变）。
    "targets": {
        "spec": ("list",), "plugin": "plugins.guard",
        "default": ["*"], "scope": "session"},
    "default": {
        "spec": ("choice", ["open", "closed"]), "plugin": "plugins.guard",
        "default": "open", "scope": "session"},
    "allow": {
        "spec": ("list",), "plugin": "plugins.guard",
        "default": [], "scope": "session"},
    "deny": {
        "spec": ("list",), "plugin": "plugins.guard",
        "default": [], "scope": "session"},
    # ---- 隔离档位（2026-10-03 B1：沙箱转 confinement 提供方后新增）----
    # 归属 ``plugins.sandbox``（沙箱**自己**的配置节）⇒ 经 ``_SECTION_ALIAS`` 同名映射到
    # ``agent.sandbox``，正是 ``sys`` 侧 ``_config()`` 读的那一节（**不是**特例）。
    # 取值域与 ``agent/plugins/sandbox/__init__.py::MODES`` 逐字一致（改一处必须改两处）。
    "mode": {
        "spec": ("choice", ["none", "read-only", "workspace-write"]),
        "plugin": "plugins.sandbox", "default": "none", "scope": "session"},
}

# 以下三张表为**派生视图**（保留旧名：既有测试与调用点零改动）——单一真相源是 _SETTING_META。
_SETTING_SCHEMA = {k: m["spec"] for k, m in _SETTING_META.items()}
_PLUGIN_OF_KEY = {k: m["plugin"] for k, m in _SETTING_META.items()}
_SETTING_DEFAULTS = {k: m["default"] for k, m in _SETTING_META.items()}
# 允许写到「本会话」层的键（G2）：设置接口据此拒绝把容器级键写进会话覆盖。
_SETTING_SCOPABLE = frozenset(k for k, m in _SETTING_META.items()
                              if m.get("scope") == "session")


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
        # ★ ``None`` 必须视为「空」而不是 ``["None"]``（2026-10-04 修）：
        # 前端把未填写的设置项以 ``null`` 提交时，原实现会落一个字面量 ``"None"`` 幽灵条目
        # ——对 ``approval.ask``（工具名 fnmatch 名单）/ ``tool_domains`` 这类键，
        # 表现为"多出一个永远匹配不到（或叫 None）的域"，且用户看不见来源。
        if raw is None:
            parts = []
        elif isinstance(raw, str):
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
    _attach_session_cfg_view(c.root)
    return c, pm


# ---- G2（会话级）：接缝注入 -------------------------------------------------
# 解耦纪律：插件**不得 import 适配层**（run/*）。会话配置经 ctx 接缝传递——
# ``view(section, session_key) -> dict``（纯读、返回副本）。插件侧 ctx.get 软取，
# 缺失即退化为容器级单实例 ⇒ 未启用会话覆盖时行为与改造前逐值等价。
# 接缝名取自 ``agent.api.SESSION_CFG_VIEW``（内核定义，双方可见、不产生反向依赖）。


def _budget_view(root, ident, container_cfg: dict) -> dict:
    """状态栏预算视图：**本会话**已用 + 本会话上限（会话覆盖优先于容器）。

    G2 前 ``BUDGET_SPENT`` 是容器单值 ⇒ 状态栏看到的其实是「所有会话合计」；
    现改为按会话取，上限亦走同一份会话配置视图（与 budget 插件的判定同源）。
    """
    sk = ident.session_key
    m = root.state.get(StateKeys.BUDGET_SPENT)
    mine = int(m.get(sk, 0) or 0) if isinstance(m, dict) else int(m or 0)
    cfg = dict(container_cfg or {})
    if sk:
        try:
            sess = _store().view("budget", sk) or {}
        except Exception:                    # noqa: BLE001 —— 视图异常回落容器值
            sess = {}
        if sess:
            cfg.update(sess)
    return {"spent": mine, "max": cfg.get("max_tokens_per_run", 0), "session": sk}


def _attach_session_cfg_view(root) -> None:
    """把会话配置视图挂到根容器（rebuild 后必须重新挂：新容器是新作用域）。"""
    if root is None:
        return
    try:
        # 传 lambda 而非 ``_store().view`` 绑定：存储被重定向（测试/多环境）后
        # 接缝仍指向**当前**真源，不会读到旧实例。
        root.provide(SESSION_CFG_VIEW, lambda section, sk=None: _store().view(section, sk))
    except Exception:                        # noqa: BLE001 —— 接缝不可用时降级为容器级
        pass


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


async def _obs_middleware(request, call_next):
    """#5 可观测性中间件：给**每个** HTTP 请求绑定 request id、回带响应头、打访问日志。

    刻意做成模块级函数（而非 ``create_app`` 内的闭包）：``_serve`` 返回时容器已
    teardown ⇒ 单测无法在进程内对真实 app 发请求；抽出来才能被测试**直接调用**，
    测的是生产实现本身而不是复刻品。

    - 绑定发生在 ``call_next`` **之前** ⇒ 下游（路由处理器、其派生 task、
      ``emit`` 的异步监听器）都能通过 ContextVar 读到同一个 id；
    - 计数：请求数 + 5xx/异常数（``/metrics`` 导出，**无标签**）；
    - 访问日志：每请求一行（method/path/status/耗时），字段由 record factory 自动
      带上 rid/session/run ⇒ **排查时先 grep  rid，再看这一行**。
      嫌吵可把 ``--log-level`` 提到 WARNING。

    ⚠️ **访问日志里的 ``session`` 恒为空**（如实登记）：会话键由下游路由任务绑定
    （``_identity``），而 ``BaseHTTPMiddleware`` 会把下游跑在**另一个 task** ⇒ 绑定
    不回流到本层。``rid`` 在本层绑定、对下游可见 ⇒ **关联键是 rid**；
    要看该请求的会话，查 ``/api/events?request_id=<rid>`` 的 ``session`` 列。
    """
    rid = bind_request_id()
    # 同时把 rid 暂存到 ``request.state``：ContextVar 在 ``BaseHTTPMiddleware`` 把下游
    # 跑在**另一个 task** 时不会回流到异常处理器上下文，直接读 ``current_request_id()``
    # 会拿到空串。异常处理器/端点改从 ``request.state.rid`` 取（兜底 current_request_id）。
    try:
        request.state.rid = rid
    except Exception:                              # noqa: BLE001 —— state 不可写不应阻断
        pass
    inc_counter("agent_requests_total")
    log = logging.getLogger("agent.access")
    started = time.perf_counter()
    try:
        resp = await call_next(request)
    except BaseException:
        inc_counter("agent_request_errors_total")
        log.error("%s %s -> 未完成（%.1fms） rid=%s",
                  getattr(request, "method", "?"),
                  getattr(getattr(request, "url", None), "path", "?"),
                  (time.perf_counter() - started) * 1000, rid)
        raise
    if resp.status_code >= 500:
        # 已注册的异常处理器在路由层内兜住 ⇒ 多数 5xx 不会走到上面的 except
        inc_counter("agent_request_errors_total")
    resp.headers["X-Request-Id"] = rid
    log.info("%s %s -> %s (%.1fms)",
             getattr(request, "method", "?"),
             getattr(getattr(request, "url", None), "path", "?"),
             resp.status_code, (time.perf_counter() - started) * 1000)
    return resp


def _req_rid(request) -> str:
    """从 ``request.state.rid`` 取请求标识（obs 中间件写入），兜底 ``current_request_id()``。

    ★ 为什么不用裸 ``current_request_id()``：``BaseHTTPMiddleware`` 把路由处理跑在独立 task，
    异常处理器上下文里 ContextVar 可能已回退为空串；``request.state.rid`` 在同一次请求内
    始终可读（异常处理器拿到的就是同一个 request 对象）。
    """
    rid = getattr(getattr(request, "state", None), "rid", "") or ""
    return rid or current_request_id()


async def _security_headers_middleware(request, call_next):
    """#4 生产级安全响应头（2026-10-04）。

    刻意做成模块级函数（与 ``_obs_middleware`` 同理由），便于单测直接调用。

    - ``X-Content-Type-Options: nosniff``：阻止 MIME 嗅探（防 content-sniffing XSS）。
    - ``X-Frame-Options: DENY``：禁止被嵌入 iframe（防 clickjacking）。
    - 对 ``/api/`` 数据面额外加：
        * ``Cache-Control: no-store``：响应含会话/事件等敏感数据，禁止任何缓存落盘；
        * ``Content-Security-Policy: default-src 'none'``：API 只返回 JSON，不需要任何
          子资源加载，收紧到最小（即便有注入点也无法加载外部脚本）。

    说明：HSTS 仅在 HTTPS 部署时有意义，本地/内网多为 http，故此处不强制加，避免
    误导运维；HTTPS 反向代理侧应自行加 ``Strict-Transport-Security``。
    """
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    path = getattr(getattr(request, "url", None), "path", "")
    if path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
        if "content-security-policy" not in resp.headers:
            resp.headers["Content-Security-Policy"] = "default-src 'none'"
    return resp


def _session_busy(active_runs: dict, session_key: str) -> str | None:
    """该会话是否已有 run 在跑；是则返回那个 run_id（否则 None）。

    ★ H2（2026-10-04）：同会话并发是**真实可达**的（单用户连发两条消息即可），
    后果有二，两条都是静默错误：
      1. 两个 run 抢同一个 ``react_chain:{session_key}`` ⇒ 链互相覆盖；
      2. 后一个请求的 ``_probe_resume`` 看到前一个的 ``running`` 快照 ⇒ 把它标
         ``orphaned``，而那个 run **还在跑**。
    故入口处查一次、忙则 409 诚实拒绝（排队会让用户以为"没反应"）。

    实现上刻意**反查现有 ``active_runs``** 而不另建一张表：
    少一份状态、且天然与取消端点共用同一真相（不会出现"表里有、run 表里没有"）。

    ★ 顺带修掉一个能力缺口：``/api/chat``（非流式）此前**不登记** ``active_runs``
    ⇒ 它既查不到别人、也**无法被 ``/api/chat/cancel`` 取消**。现已登记。
    """
    if not session_key:
        return None
    for rid, entry in list(active_runs.items()):
        try:
            _task, owner = entry
        except (TypeError, ValueError):       # pragma: no cover —— 形状异常不阻塞入口
            continue
        if owner == session_key:
            return rid
    return None


async def _acquire_session_run(active_runs: dict, session_key: str):
    """★ H2-TOCTOU 修复（2026-10-04）：原子的「查忙 → 登记」。

    此前 ``/api/chat`` 与 ``/api/chat/stream`` 先在入口 ``_session_busy`` 查忙，
    再在**若干 await 之后**才 ``active_runs[run_id] = ...`` 登记占位/真实任务。
    两条同会话请求可双双通过忙检、各自登记 ⇒ 两个 run 抢同一 ``react_chain``、
    后者还会把前者标 orphaned。

    本函数用 ``_RUN_LOCK`` 把「查忙 + 登记占位 marker」包成原子段：
    任一时刻只有一个协程能进入，登记完成前别的请求查到的仍是「未登记」，
    故第二个请求必然命中 busy 分支拿 409。

    返回：
      ``("ok", run_id, marker)``         —— 登记成功，marker 是占位任务（稍后由调用方替换为真实 task）
      ``("busy", busy_run_id, None)``    —— 该会话已有 run 在跑，调用方应回 409 并带上 busy_run_id
    """
    async with _RUN_LOCK:
        busy = _session_busy(active_runs, session_key)
        if busy is not None:
            return ("busy", busy, None)
        run_id = uuid.uuid4().hex[:8]
        marker = asyncio.ensure_future(asyncio.sleep(0))   # 占位任务，立即被替换
        active_runs[run_id] = (marker, session_key)
        return ("ok", run_id, marker)

def _register_probes(app, c, *, active_runs, pm, _identity, _apply_rebuild, _sse,
                    _session_cfg, JSONResponse, Request, StreamingResponse):
    """★ 探针 + 会话流路由（/health /ready /metrics /api/chat*）。

    从 ``_register_routes`` 拆出（2026-10-04，机械搬移）。调用顺序由
    ``_register_routes`` 固定：**探针 → 会话流 → approval → 资源**。
    """
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

    def _metrics_session_labels() -> bool:
        """per-session 指标开关（默认关，见 S7）。"""
        if os.environ.get("PRISM_METRICS_SESSION_LABELS", "").strip().lower() in (
                "1", "true", "on", "yes"):
            return True
        cfg = ((c.root.config or {}).get("agent") or {}).get("metrics") or {}
        return bool(cfg.get("session_labels"))

    def _prom_label(value: object) -> str:
        """Prometheus 标签值转义（反斜杠 / 双引号 / 换行）。"""
        return (str(value).replace("\\", "\\\\").replace('"', '\\"')
                .replace("\n", "\\n"))

    @app.get("/metrics")
    async def _metrics() -> Response:
        """极简 Prometheus 文本指标（核心指标；多会话后加 tenant/user label）。"""
        budget_cfg = ((c.root.config or {}).get("agent") or {}).get("budget") or {}
        # G2：BUDGET_SPENT 已按**会话分桶**。metrics 保持「容器总计」语义（既有图表不破），
        # 需要 per-session 明细时打开 PRISM_METRICS_SESSION_LABELS=1（默认关：防标签基数爆炸）。
        _spent_map = c.root.state.get(StateKeys.BUDGET_SPENT)
        spent = (sum(int(v or 0) for v in _spent_map.values())
                 if isinstance(_spent_map, dict) else int(_spent_map or 0))
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
        # ---- #5 可观测性：请求/事件计数（**无标签**，刻意不做 per-request/per-session
        # label——基数无界会撑爆 Prometheus；要按请求排查请用 rid 过滤 /api/events）----
        for name, value in sorted(counters_snapshot().items()):
            lines += [f"# HELP {name} 进程内累计计数",
                      f"# TYPE {name} counter",
                      f"{name} {value:g}"]
        for owner, n in sorted(owners.items()):
            if owner:
                lines.append(f'agent_tools_by_owner{{owner="{owner}"}} {n}')
        # G2（S7）：per-session 预算明细——**默认关闭**（标签基数随会话数无界增长，
        # 会撑爆 Prometheus）。开启方式二选一：环境变量 ``PRISM_METRICS_SESSION_LABELS=1``
        # 或配置 ``agent.metrics.session_labels=true``。
        if _metrics_session_labels() and isinstance(_spent_map, dict):
            lines += [
                "# HELP agent_budget_spent_by_session 各会话已用 token（默认关：基数受限）",
                "# TYPE agent_budget_spent_by_session gauge",
            ]
            for _sk, _v in sorted(_spent_map.items()):
                lines.append(
                    f'agent_budget_spent_by_session{{session="{_prom_label(_sk)}"}} '
                    f"{int(_v or 0)}")
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.post("/api/chat")
    async def _chat(body: dict, request: Request):
        """非流式对话。

        ★ H2（2026-10-04）：本端点此前**既不登记 active_runs、也不查会话是否在跑**
        ⇒ 同一会话连发两条 ⇒ 两个 run 抢 ``react_chain:{session_key}`` 互相覆盖，
        且第二个请求的 ``_probe_resume`` 会把**正在跑的**第一个 run 标 orphaned。
        现与流式端点同规则：登记 + 忙则 409。
        """
        ident = _identity(request, body)
        acq = await _acquire_session_run(active_runs, ident.session_key)
        if acq[0] == "busy":
            return JSONResponse(
                {"error": "该会话还有一轮任务在执行中，请等待完成或先取消",
                 "code": "session_busy", "run_id": acq[1]},
                status_code=409)
        _tag, run_id, marker = acq
        try:
            reply = await c.root.chat.send(c.root, str(body.get("message") or ""),
                                           session_id=ident.session_key,
                                           identity=ident)
            return JSONResponse({"text": reply.text, "ok": reply.ok, "error": reply.error,
                                 "notice": reply.notice,
                                 "run_id": run_id,
                                 "evidence": {"plan": reply.plan, "evidence": reply.evidence,
                                              "tokens_in": reply.tokens_in,
                                              "tokens_out": reply.tokens_out}})
        except AgentError as exc:                       # 内核受控错误：消息本身面向用户，可回显
            rid = _req_rid(request)
            _err_log.warning("AgentError @ /api/chat session=%s rid=%s: %s",
                             ident.session_key, rid, exc)
            return JSONResponse({"error": str(exc), "kind": type(exc).__name__,
                                 "request_id": rid}, status_code=500)
        except Exception:                        # noqa: BLE001 —— 内部错误不裸泄
            rid = _req_rid(request)
            _err_log.exception("未捕获异常 @ /api/chat session=%s rid=%s",
                               ident.session_key, rid)
            return JSONResponse({"error": "服务内部错误，请稍后重试或凭 request_id 报障",
                                 "request_id": rid}, status_code=500)
        finally:
            active_runs.pop(run_id, None)
            if not marker.done():
                marker.cancel()

    @app.post("/api/chat/stream")
    async def _chat_stream(body: dict, request: Request):
        """B5 流式：SSE 逐帧推送（start → progress… → final / error / cancelled）。

        与 B6 共用同一 run 生命周期：本端点持有任务并登记 run_id（**带归属会话**），
        取消端点据此 cancel 并校验归属。``/api/chat`` 保留不动（--once 与离线测试依赖它）。

        2.2：会话标识经 ``X-Session-Id`` 请求头（本端点是 fetch POST，可自定义头；
        与 EventSource 不同，无需退化到 query）。

        ★ H2（2026-10-04）：同会话已有 run 在跑时**诚实拒绝 409**（见 ``/api/chat`` 的说明）。
        """
        ident = _identity(request, body)
        acq = await _acquire_session_run(active_runs, ident.session_key)
        if acq[0] == "busy":
            return JSONResponse(
                {"error": "该会话还有一轮任务在执行中，请等待完成或先取消",
                 "code": "session_busy", "run_id": acq[1]},
                status_code=409)
        _tag, run_id, marker = acq
        message = str(body.get("message") or "")
        # ★ H4（2026-10-04）：此处曾传**无界**队列，绕过了 ``sys/progress.py`` 自建队列的
        # 1024 丢帧保护（``tap_progress`` 文档写明"传入自定义 queue 时以其实际容量为准"）
        # ⇒ 慢客户端/断连时 token 高频帧无限堆积，吃满内存。
        # 现在有界；``_push`` 满时丢最旧并计数（既有机制），worker 的终止帧另走
        # ``_put_terminal``（**不能阻塞**：满时也丢最旧，保证 final/error/cancelled 一定送出）。
        q: asyncio.Queue = asyncio.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)

        async def _put_terminal(frame: dict) -> None:
            """放终止帧：满了丢最旧再放，**绝不 await 阻塞**。

            ★ 为什么不能直接 ``await q.put``：队列满时它会挂住 ⇒ worker 永不返回 ⇒
            run 永远停在 active_runs 里（且取消端点也拿不到终态）。
            """
            try:
                q.put_nowait(frame)
                return
            except asyncio.QueueFull:
                pass
            try:
                q.get_nowait()                      # 丢最旧一帧
                q.put_nowait(frame)
                q.dropped = getattr(q, "dropped", 0) + 1
            except Exception:                      # noqa: BLE001 —— 极端情况不阻断主链路
                pass

        async def _worker() -> None:
            try:
                reply = await c.root.chat.send(c.root, message,
                                               session_id=ident.session_key,
                                               identity=ident)
                await _put_terminal({"event": "final", "payload": {
                    "text": reply.text, "ok": reply.ok, "error": reply.error,
                    "notice": reply.notice,
                    "evidence": {"plan": reply.plan, "evidence": reply.evidence,
                                 "tokens_in": reply.tokens_in, "tokens_out": reply.tokens_out}}})
            except asyncio.CancelledError:
                await _put_terminal({"event": "cancelled", "payload": {"run_id": run_id}})
                raise                                  # 保持取消语义，不吞
            except AgentError as exc:                  # 内核受控错误：消息面向用户，可回显
                rid = _req_rid(request)
                _err_log.warning("AgentError @ /api/chat/stream run=%s rid=%s: %s",
                                 run_id, rid, exc)
                await _put_terminal({"event": "error",
                                     "payload": {"error": str(exc),
                                                 "kind": type(exc).__name__,
                                                 "request_id": rid}})
            except Exception:                   # noqa: BLE001 —— 内部错误不裸泄
                rid = _req_rid(request)
                _err_log.exception("未捕获异常 @ /api/chat/stream run=%s rid=%s",
                                   run_id, rid)
                await _put_terminal({"event": "error",
                                     "payload": {"error": "服务内部错误，请稍后重试或凭 request_id 报障",
                                                 "request_id": rid}})

        async def _gen():
            task: asyncio.Task | None = None
            try:
                yield _sse("start", {"run_id": run_id})
                # 先订阅（不漏帧），再起任务
                async with tap_progress(c.root, queue=q):
                    if not marker.done():
                        marker.cancel()                # 占位任务使命结束，换真实 task
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
            except Exception:                   # noqa: BLE001 —— 内部错误不裸泄
                rid = _req_rid(request)
                _err_log.exception("未捕获异常 @ /api/chat/stream gen run=%s rid=%s",
                                   run_id, rid)
                yield _sse("error", {"error": "服务内部错误，请稍后重试或凭 request_id 报障",
                                     "request_id": rid})

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


def _register_approval(app, c, *, active_runs, pm, _identity, _apply_rebuild, _sse,
                       _session_cfg, _approval_or_none, JSONResponse, Request):
    """★ 审批 + 只读状态路由（11 条）。

    含 ``/api/approval/*``（answer/pending/mode）与**只读**状态端点
    ``/api/status`` ``events`` ``context`` ``perms`` ``session``
    ``session/reset`` ``tools`` ``memory``。

    划分依据：这三个域都是"读状态 / 走审批"，与 ``_register_probes``
    （健康探针 + 会话流）和 ``_register_resources``（插件与设置的**写操作**）区分。

    从 ``_register_routes`` 拆出（2026-10-04，机械搬移）。调用顺序由
    ``_register_routes`` 固定：**probes → approval(本函数) → resources**。
    ⚠️ 本函数必须在 ``mount("/")`` 之前调用（Starlette 按注册顺序匹配）。
    """
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
    async def _approval_mode(body: dict, request: Request):
        """切换**本会话**档位（default/plan/auto/bypass）。

        OPEN-5：切 bypass 属高危（跳过一切问人），需 ``confirm=true`` 二次确认。

        R1 补（2026-10-02）：档位已按会话分桶，故本端点**必须解析身份**（经 ``_identity``
        同时绑定请求作用域）——此前不解析，`set_mode` 写的是容器级单值 ⇒ 任何会话改档位
        都会改到全局（A 切 bypass 即放行 B 在跑 run 的审批闸）。
        """
        _identity(request, body)                  # 解析身份（副作用：绑定请求作用域）
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
                # G2：预算按会话统计（状态栏显示「本会话已用/上限」；max 亦取会话覆盖值）
                "budget": _budget_view(c.root, ident, budget_cfg)}

    @app.get("/api/events")
    async def _events(request: Request, request_id: str = "", limit: int = 120) -> dict:
        """durable 事件账本（倒序）。

        #5：``request_id`` 非空时**只回该请求的痕迹**——这就是"可排查、可串联"的落点：
        拿到响应头 ``X-Request-Id``（或用户报障给的 id）即可拉出那一轮的全部事件。

        ★ 生产级多租户隔离（2026-10-04）：``request_id`` 为空时**不再回吐全局账本**
        （那会把所有会话的事件一次性泄露给任意调用方），改为只回**本会话**最近事件
        （行级 ``WHERE session=?``）。要跨请求串联想用 ``request_id`` 精确查。
        ``limit`` 上限 500（防一次拉爆；``agent_events`` 无 per-request 索引退化保护）。
        """
        ident = _identity(request)                    # 解析身份（同时绑定请求作用域）
        cap = max(1, min(500, int(limit)))
        if request_id:
            rows = await c.root.db.query(
                c.root, "SELECT seq, event, payload, at, request_id, run_id, session"
                        " FROM agent_events WHERE request_id=? ORDER BY seq LIMIT ?",
                (request_id, cap))
        else:
            rows = await c.root.db.query(
                c.root, "SELECT seq, event, payload, at, request_id, run_id, session"
                        " FROM agent_events WHERE session=? ORDER BY seq DESC LIMIT ?",
                (ident.session_key, cap))
        return {"rows": rows, "request_id": request_id or None,
                "session": ident.session_key}

    # ---- G2.0（P3-1）：Inspector ``/context`` 视图（纯只读旁路，不 import 插件内部）----

    @app.get("/api/context")
    async def _context_view(request: Request, limit: int = 60) -> dict:
        """把 governed 遥测合并为**轮次全景账本**并渲染 ``/context`` 分类视图（薄封装）。

        ★ 2026-10-04：补 ``_identity(request)`` 解析身份——与 ``/api/perms`` 同理由，
        未绑定请求作用域会让 ContextVar 落到上一次聊天的会话残留态，视图口径不确定。
        """
        _identity(request)                        # 解析身份（副作用：绑定请求作用域）
        return await _context_view_payload(c.root, limit=limit)

    @app.get("/api/perms")
    async def _perms(request: Request) -> dict:
        """权限视图：策略 + 最近判定 + **本会话**档位。

        R1 补（2026-10-02）：``mode`` 已按会话分桶，故本端点必须解析身份——否则
        ContextVar 未绑定 ⇒ 走到回退分支读到**上一次聊天请求留下的会话**（不确定是哪个），
        前端显示的档位与实际生效的档位可能不是同一个会话的。
        """
        _identity(request)                        # 解析身份（副作用：绑定请求作用域）
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
    async def _tools(request: Request) -> dict:
        # ★ 2026-10-04：补 ``_identity(request)`` —— 与全量 /api 路由一致地解析请求作用域，
        # 避免 ContextVar 落到上一次聊天的会话残留态。
        _identity(request)
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


def _register_resources(app, c, *, active_runs, pm, _identity, _apply_rebuild, _sse,
                       _session_cfg, JSONResponse, Request):
    """★ 插件与设置路由（6 条，写操作域）。

    ``/api/plugins``（列表）、``/api/plugins/{enable,scaffold,remove}``、
    ``/api/settings``（GET 读 + POST 写）。

    与 ``_register_approval`` 的分界：那边是**审批 + 只读状态**，这边是
    **会触发插件重建的配置写入**（``_apply_rebuild``）。

    从 ``_register_routes`` 拆出（2026-10-04，机械搬移）。调用顺序由
    ``_register_routes`` 固定：**probes → approval → resources(本函数)**。
    ⚠️ 本函数必须在 ``mount("/")`` 之前调用（Starlette 按注册顺序匹配）。
    """
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
            # #5：改走 logger（此前 print ⇒ 无级别控制、无请求标识，且与日志不同流）
            logging.getLogger("agent.chat").error(
                "后台重建失败（期望态已保存，实际装配可能需重启）：%s", exc)

    async def _mutate_then_fetch(background: bool = False) -> dict:
        # 先返回期望态已更新的当前快照（desired 已写入 state.json）。
        # 容器重建代价高（含 MCP 子进程拉起等，常需数十秒），故改为后台执行，
        # 避免管理请求长时间阻塞、前端“点了没反应”。前端会显示“处理中…”并在稍后自动刷新。
        res = await _plugins()
        if background:
            # 登记到模块级集合：退出时可观测/排障；任务完成自动移除（add_done_callback）。
            _bg = asyncio.create_task(_bg_rebuild())
            _BG_TASKS.add(_bg)
            _bg.add_done_callback(_BG_TASKS.discard)
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

    def _setting_values(ov: dict) -> dict:
        """把一份覆盖（``{plugins.X: {...}}``）解析成键值（缺项回落默认）。"""
        out: dict = {}
        for k in _SETTING_SCHEMA:
            cur = (ov.get(_PLUGIN_OF_KEY.get(k, ""), {}) or {}).get(k, _SETTING_DEFAULTS[k])
            try:
                out[k] = _coerce_setting(k, cur)
            except (ValueError, TypeError):
                out[k] = _SETTING_DEFAULTS[k]
        return out

    def _session_raw(sk: str) -> dict:
        """本会话**显式**覆盖（仅用户确实写过的键）——「继承 vs 覆盖」据此区分。"""
        raw = _store().session(sk)
        out: dict = {}
        for k in _SETTING_SCHEMA:
            pid = _PLUGIN_OF_KEY.get(k, "")
            if k in (raw.get(pid) or {}):
                try:
                    out[k] = _coerce_setting(k, raw[pid][k])
                except (ValueError, TypeError):
                    pass
        return out

    @app.get("/api/settings")
    async def _settings(request: Request) -> dict:
        ident = _identity(request)
        sk = ident.session_key
        ctx_enabled = "plugins.context" in set(pm.enabled())
        # G2（S5-2）：三份视图——容器级生效值 / 本会话显式覆盖 / 二者合成的生效值。
        # ``values`` 保持旧名 = effective（既有调用点与前端零改动）；新增三个字段供
        # 设置页区分「容器级 / 本会话级」并渲染「重置为继承」。
        values = _setting_values(_store().effective(sk))
        ap_enabled = "plugins.approval" in set(pm.enabled())
        # G7：沙箱插件默认关（目录插件惯例）⇒ 设置页据此提示"未启用"，与 context/approval 同款
        sb_enabled = "plugins.sandbox" in set(pm.enabled())
        # ★ 沙箱**隔离档位与强度**（2026-10-03 B1 转型后）。前端必须拿到**自报的实际强度**
        # 才能渲染徽标 —— 缺这个字段时前端只能猜"正常"，那正是此前最大的误导源。
        # 经 ``container.plugin_statuses()``（公开 API，自带"单插件失败不拖垮"语义）取，
        # 插件未启用时如实报 ``none``（不谎报），前端据此显示「⚠ 无隔离」。
        sb_status: dict = {"mode": "none", "enforcement": "none", "gap": False,
                           "targets": ["mcp.*"], "reason": "沙箱插件未启用"}
        if sb_enabled:
            try:
                for row in await c.container.plugin_statuses():
                    if row.get("id") != "plugins.sandbox":
                        continue
                    st = row.get("status")
                    if isinstance(st, dict) and "error" not in st:
                        sb_status = {**sb_status, **st}
                    elif isinstance(st, dict):
                        sb_status["reason"] = f"读取沙箱状态失败: {st['error']}"
                    break
            except Exception as exc:  # noqa: BLE001 —— 自述失败不得拖垮设置页
                sb_status["reason"] = f"读取沙箱状态失败: {exc}"
        return {"context_plugin_enabled": ctx_enabled,
                "approval_enabled": ap_enabled, "sandbox_enabled": sb_enabled,
                "sandbox_mode": sb_status.get("mode", "none"),
                "sandbox_enforcement": sb_status.get("enforcement", "none"),
                "sandbox_gap": bool(sb_status.get("gap")),
                "sandbox_targets": sb_status.get("targets") or ["mcp.*"],
                "sandbox_reason": sb_status.get("reason", ""),
                # ★ 落点（2026-10-04）：声明档位时，外部工具子进程被挪到会话私有目录
                # （cwd + 缓存/临时目录重定向）。与 `enforcement` **分开**报——重定向
                # **不是**强制（绝对路径仍可写别处），报成 partial 才是谎报。
                # 这两个字段只回答一件事："东西落在哪"。
                "sandbox_workspace": sb_status.get("workspace", ""),
                "sandbox_env_redirected": bool(sb_status.get("env_redirected")),
                "values": values,
                "session_id": sk,
                "scopable": sorted(_SETTING_SCOPABLE),
                "container": _setting_values(_store().container()),
                "session": _session_raw(sk),
                "effective": values,
                # 域清单：设置页据此把 tool_domains 渲染为「可勾选」而非手填
                "domains": _domain_options(c.root.tasks.catalog(),
                                           values.get("tool_domains"))}

    @app.post("/api/settings")
    async def _settings_update(body: dict, request: Request):
        try:
            # scope：``container``（默认，保持旧行为）| ``session``（G2 本会话级）。
            scope = str((body or {}).get("scope") or "container").strip().lower()
            if scope not in ("container", "session"):
                return JSONResponse({"ok": False, "error": f"未知 scope: {scope}"},
                                    status_code=400)
            ident = _identity(request)
            sk = ident.session_key
            if bool(body.get("reset")):
                if scope == "session":
                    # 只清本会话覆盖 ⇒ 逐键回落到容器/默认；**不重建容器**（下个请求自然生效）
                    _store().clear_session(sk)
                    return JSONResponse({"ok": True, "settings": await _settings(request)})
                ov = _load_overrides()
                for pid in ("plugins.context", "plugins.short_term", "plugins.approval",
                            "plugins.budget", "plugins.react"):
                    ov.pop(pid, None)
                _save_overrides(ov)
                await _apply_rebuild()
                return JSONResponse({"ok": True, "settings": await _settings(request)})
            patch = body.get("patch") or {}
            if not isinstance(patch, dict) or not patch:
                return JSONResponse({"ok": False, "error": "patch 为空"}, status_code=400)
            if scope == "session":
                bad = sorted(k for k in patch if k not in _SETTING_SCOPABLE)
                if bad:
                    return JSONResponse(
                        {"ok": False,
                         "error": f"以下设置不支持会话级（仅容器级）: {', '.join(bad)}"},
                        status_code=400)
            buckets: dict[str, dict] = {}
            for k, v in patch.items():
                if k not in _SETTING_SCHEMA:
                    return JSONResponse({"ok": False, "error": f"未知设置项: {k}"}, status_code=400)
                coerced = _coerce_setting(k, v)
                buckets.setdefault(_PLUGIN_OF_KEY[k], {})[k] = coerced
            if scope == "session":
                # 会话级写入**不触发 _apply_rebuild**：门面/预算/压缩阈值都在请求时现取
                # 会话配置视图 ⇒ 下个请求自然生效（这也是「永不 rebuild」的收益）。
                _store().patch_session(sk, buckets)
            else:
                ov = _load_overrides()
                for pid, b in buckets.items():
                    cur = dict(ov.get(pid, {}))
                    cur.update(b)
                    ov[pid] = cur
                _save_overrides(ov)
                await _apply_rebuild()
            return JSONResponse({"ok": True, "settings": await _settings(request)})
        except (ValueError, TypeError) as exc:
            return JSONResponse({"ok": False, "error": f"参数错误: {exc}"}, status_code=400)


def _register_routes(app, c, *, active_runs, pm, _identity, _apply_rebuild, _sse,
                    _session_cfg, _approval_or_none, JSONResponse, Request,
                    StreamingResponse):
    """★ 注册全部 HTTP 路由（2026-10-04 从 ``_serve`` 机械搬移）。

    本函数只做**编排**：按固定顺序调用三个域函数。
    ① 探针/会话流 → ② approval → ③ 资源（``/api/settings`` 等）。
    ④ 静态托管 ``mount("/")`` 由 ``_serve`` 在**本函数之后**追加
       （Starlette 按注册顺序匹配，Mount 会捕获其后一切 ⇒ 必须最后）。

    等价性守卫：``.workbuddy/verify_serve_split_equiv.py``
    （真跑一次 ``_serve``，与基线**逐字段比对有序路由表** + 断言 Mount 最后）。
    ★ 参数只有 `app`/`c` 是位置参数，其余必须**关键字传**（签名里有 `*`）。
    """
    _kw = dict(active_runs=active_runs, pm=pm, _identity=_identity,
               _apply_rebuild=_apply_rebuild, _sse=_sse, _session_cfg=_session_cfg)
    # ★ 每个注册函数需要的**全部**来自 `_serve` 的闭包项都在此显式转发。
    #   少转发一个 ⇒ 该名字在函数体内未定义 ⇒ 调用即 NameError ⇒ 该端点 500
    #   （曾真实发生：`/api/approval/pending`）。守卫 verify_serve_split_equiv.py 的
    #   check_missing_names() 会静态抓出这类漏项。
    _register_probes(app, c, JSONResponse=JSONResponse, Request=Request,
                     StreamingResponse=StreamingResponse, **_kw)
    _register_approval(app, c, _approval_or_none=_approval_or_none,
                       JSONResponse=JSONResponse, Request=Request, **_kw)
    _register_resources(app, c, JSONResponse=JSONResponse, Request=Request, **_kw)

async def _serve(args: argparse.Namespace) -> int:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

    # 生产加固 fail-fast：`--hardened` 语义是"deny-by-default"，缺 token 会让**所有**工具调用
    #（含 chat）被 guard 拒绝 ⇒ 服务虽起但不可用，且"看起来在跑"极具误导性。**在装配之前**就
    # 拒绝（此前检查放在 _boot 之后 ⇒ 白白拉起 Redis/MCP 子进程再退出）。
    if getattr(args, "hardened", False) and not str(args.token or ""):
        raise SystemExit(
            "--hardened 已启用但未提供 token：deny-by-default 下所有工具调用（含 chat）都会被拒。"
            "请同时提供 --token 或环境变量 PRISM_TOKEN。")

    # ---- #5 可观测性：**最先**装日志（此前全仓无任何 handler，INFO 级静默丢弃）----
    # 放在这里（而非 uvicorn.Config 之前）是为了让 ``create_app`` 内部的所有日志
    # ——含前端产物提示、重建失败——也走同一套 handler 与字段。
    configure_logging(level=args.log_level, fmt=args.log_format)

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
    #
    # R1（2026-10-02）：本函数据此成为**请求作用域绑定的单一漏斗**——解析成功即把会话键
    # 绑到 ContextVar（按 asyncio task 隔离 ⇒ 每请求独立）。消费方多为**无 ctx 入参**的事件
    # 回调（approval/guard）或装配期捕获 ctx 的服务（budget/router），它们读不到请求参数，
    # 只能靠该通道；``ctx.state`` 仍照写（兼容既有测试写法），但不再是读取主通道。
    # ⇒ 新增端点若需要会话归属，**必须经本函数**解析身份（勿自己读头）。

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
            ident = make_identity(tenant=cfg.get("tenant"), user=cfg.get("user"),
                                  session=raw)
        except SessionIdError as exc:
            raise HTTPException(status_code=400, detail=f"非法会话标识：{exc}")
        set_current_session_key(ident.session_key)
        return ident

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
        _attach_session_cfg_view(c2.root)     # 新容器 ⇒ 重新挂接缝

    # ---- 审批（人在环中，第 2 期）：入站应答 + 在途快照 + 会话档位 ----
    # 解耦纪律：只经 Services.APPROVAL 门面（ctx.approval），不 import 插件内部模块。
    # ★ 定义在**这里**（_serve 内）而非 `_register_approval` 内 —— 因为 `c` 是 `_Box`，
    #   插件重建后 `c.root` 会变；放模块级会捕获旧容器。三个注册函数都靠参数拿到它。
    def _approval_or_none():
        if not c.root.has(Services.APPROVAL):
            return None
        return c.root.get(Services.APPROVAL)

    app = FastAPI(title="Agent Harness", docs_url=None, redoc_url=None)

    # ---- R3：全局异常兜底（此前完全缺失）-----------------------------------
    # 此前未注册任何 exception_handler ⇒ 非 HTTPException 的未捕获异常落到
    # Starlette 默认处理器，返回 **HTML** 500：前端 `res.json()` 直接解析失败，
    # 且不留统一日志。这里统一转成 JSON 错误体（形状沿用 /api/chat 的既有兜底
    # `{"error": ...}`，前端零改动），并记一条带路径的 error 日志。
    # 注意：HTTPException 不在此接管——Starlette 默认已返回 JSON `{"detail": ...}`，
    # 覆盖它会改变 400/404 的既有响应体形状，属于无收益的行为变更。
    # （`_err_log` 是**模块级**常量，此处直接复用，勿再定义局部同名——见其定义处说明。）

    @app.exception_handler(AgentError)
    async def _on_agent_error(request: Request, exc: AgentError) -> JSONResponse:
        # AgentError 是内核**受控**错误族（消息本就面向用户，如「能力未就绪/被策略拦截」），
        # 故仍回显其消息；但补 request_id 便于报障，并在服务端落完整日志（含路径）。
        rid = _req_rid(request)
        _err_log.error("AgentError @ %s rid=%s: %s", request.url.path, rid, exc)
        return JSONResponse({"error": str(exc), "kind": type(exc).__name__,
                             "request_id": rid}, status_code=500)

    @app.exception_handler(Exception)
    async def _on_unhandled(request: Request, exc: Exception) -> JSONResponse:
        # ★ 生产级：未捕获的**意外**异常绝不回显 str(exc)（可能含栈帧/路径/密钥/SQL），
        # 改为通用体 + request_id；完整异常仅留服务端 traceback（_err_log.exception）。
        rid = _req_rid(request)
        _err_log.exception("未捕获异常 @ %s rid=%s", request.url.path, rid)
        return JSONResponse({"error": "服务内部错误，请稍后重试或凭 request_id 报障",
                             "request_id": rid}, status_code=500)

    # ---- #5 可观测性：每请求绑定 request id ---------------------------------
    # 装在**所有路由之外**的中间件里 ⇒ 无论端点是否经 ``_identity`` 解析身份
    # （``/health``、``/metrics``、``/api/events``…）都能拿到本次请求的 id。
    # 账本（``agent_events`` 的三列）与日志（record factory 注入 rid/session/run）
    # 都从这一处绑定取值 ⇒ **一次请求的痕迹可按同一 id 串起来**。
    # 响应头回带 ``X-Request-Id``：用户报障时可直接给出该值定位到那一轮。
    # 实现见模块级 ``_obs_middleware``（抽成模块级函数才能被单测直接调用——
    # ``_serve`` 返回时容器已 teardown，无法在进程内对其发真实请求）。
    app.middleware("http")(_obs_middleware)
    # ---- #4 安全响应头（见模块级 ``_security_headers_middleware``）----
    app.middleware("http")(_security_headers_middleware)

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
    # （--hardened 缺 token 的 fail-fast 已在 `_serve` 入口、装配之前完成。）

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

    # ★ 路由注册已抽到模块级 `_register_routes`（2026-10-04，纯机械搬移）。
    #   等价性守卫：`.workbuddy/verify_serve_split_equiv.py`（比对有序路由表 + Mount 必须在最后）。
    _register_routes(
        app, c, active_runs=active_runs, pm=pm,
        _identity=_identity, _apply_rebuild=_apply_rebuild,
        _sse=_sse, _session_cfg=_session_cfg,
        _approval_or_none=_approval_or_none, JSONResponse=JSONResponse,
        Request=Request, StreamingResponse=StreamingResponse)
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
                    # #4 纵深防御：应用 HTML 此前**无 CSP**（CSP 只加在 /api/* 契约响应上），
                    # 而前端是 v-html / innerHTML 结构，安全性全押在手写 esc() 上。
                    # 这里补一层：script 只允许同源（挡住注入的外部脚本与内联脚本执行），
                    # style 放开 'unsafe-inline'（卡片 HTML 可能带内联样式，收紧会白屏）。
                    resp.headers.setdefault(
                        "Content-Security-Policy",
                        "default-src 'self'; script-src 'self'; img-src 'self' data:; "
                        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                        "frame-ancestors 'none'; base-uri 'none'")
                return resp

        app.mount("/", _SPAStaticFiles(directory=str(_DIST), html=True), name="web")
        logging.getLogger("agent.chat").info("托管前端产物：%s", _DIST)
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

        logging.getLogger("agent.chat").warning(
            "未找到前端产物 %s —— 首页将返回 503 与构建指引", _DIST)

    # ---- 生产改造 #4：安全姿态告警 ----
    # 只要「无 token 且权限开放」即打印醒目 WARNING（部署风险）。
    # （--hardened 缺 token 已在 `_token` 计算处 fail-fast，不在此处。）
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

    url = f"http://{args.host}:{args.port}"
    # #5：走 logger（级别由 --log-level 决定；设为 WARNING 及以上时不再打印此行）
    logging.getLogger("agent.chat").info("Agent Harness 就绪: %s   （Ctrl+C 退出）", url)
    # ---- #5 可观测性：uvicorn 日志级别跟随（此前硬编码 "warning" ⇒ 访问日志近乎关闭）----
    config = uvicorn.Config(app, host=args.host, port=args.port,
                            log_level=str(args.log_level).lower())
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
        # ---- 优雅退出：先排空在跑的会话任务，再 teardown（避免孤儿进程/半截写入）----
        # 关 SIGTERM 后 uvicorn 已停止 accept，但已建立的流式连接在 ``task`` 取消时才断；
        # 这里显式取消 active_runs 里未完的 run（chat/stream 的 cancel 在 finally 收尾），
        # 等它们干净退出再 teardown（其中含 MCP 子进程的回收）。
        _svc_log.warning("draining in-flight runs before teardown (%d active)",
                         len(active_runs))
        _pending = [t for (t, _owner) in active_runs.values()
                    if isinstance(t, asyncio.Task) and not t.done()]
        for t in _pending:
            t.cancel()
        for t in _pending:
            try:
                await asyncio.wait_for(t, timeout=5)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
        # 后台容器重建任务也一并等待（最多再给 5s），避免 teardown 时它还在动容器
        _bg = [t for t in list(_BG_TASKS) if not t.done()]
        if _bg:
            _svc_log.warning("waiting for %d background rebuild task(s)", len(_bg))
            for t in _bg:
                try:
                    await asyncio.wait_for(t, timeout=5)
                except (asyncio.CancelledError, asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass
        await c.teardown()
    return 0


async def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    if args.once:
        return await _once(args)
    return await _serve(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
