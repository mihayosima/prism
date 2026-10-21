"""agent.run.chat —— Agent 通用对话外设。

前端参考 agent 对话形态，不特化为任何领域：
通用能力为 ping / tool_call / chat；领域能力经「已启用的工具插件」挂入工具目录。

用法：
 python -m agent.run.chat # 网页
 python -m agent.run.chat --fixture # 离线夹具
 python -m agent.run.chat --once "你好" # 无头跑一次

前端：页面已独立为 ``web/`` 工程，本服务在
``_serve`` 末尾以 ``StaticFiles`` 单端口托管其构建产物 ``web/dist/``。
开发期仍可另起 Vite dev server 经 proxy 转发 ``/api``。
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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent.api import (AgentError, SESSION_CFG_VIEW, Services, SessionIdError,
                       StateKeys, bind_request_id, configure_logging,
                       counters_snapshot, current_request_id, inc_counter,
                       make_identity, set_current_session_key)
from agent.profiles import BASE_AGENT_CONFIG, build_agent
from agent.run.config_store import ConfigStore
from agent.sys.progress import tap_progress

# : 服务器入口的 SQLite 库路径。
# : 刻意不同于 ``core/config_schema.DEFAULT_DB_PATH``，不是笔误：
# : - 后者服务 ``build_agent()`` 的离线/嵌入式用法——它们会真实连上项目根的库文件；
# : - 长驻服务器若共用同一个文件，会与上述用法争 WAL、且被离线写入污染服务端账本。
# : 两库 schema 完全相同，换入口即可，无需迁移。
DEFAULT_CHAT_DB_PATH = "data/agent_chat.db"

# : SSE 帧队列容量。与 ``sys/progress.py::tap_progress`` 的自建默认值
# : 保持一致：token 高频流 + 慢消费者不再无限占内存。
# : 满了走既有丢帧策略，不阻断业务主链路。
_STREAM_QUEUE_MAXSIZE = 1024

# : 会话 run 登记锁：``check-busy → register`` 必须原子，
# : 否则同会话并发两条请求可双双通过忙检、各自登记 ⇒ 两个 run 抢同一会话链互相覆盖。
# : 放在模块级：本服务进程内单实例，无需按容器分桶。
_RUN_LOCK = asyncio.Lock()

# : 后台容器重建任务登记：仅用于退出时观测/排障，
# : 不阻塞主链路；任务完成自动从集合移除。
_BG_TASKS: set[asyncio.Task] = set()

# : 路由处理器日志器。必须模块级：`_register_probes`/`_register_approval` 等是
# : 模块级函数，其内部 handler 引用本名——此前只在 `_serve`
# : 内定义局部 `_err_log`，机械搬移路由到模块级函数时漏了这条依赖 ⇒ 那些错误分支调用时
# : `NameError`。提到模块级后两处同源。
_err_log = logging.getLogger("agent.chat")

# 本模块开了 ``from __future__ import annotations`` ⇒ 路由签名里的注解是字符串，
# FastAPI 用模块全局解析它们。故 ``Request``/``HTTPException`` 必须在模块级可见，
# 只在 create_app 内局部 import 会被当成普通查询参数。fastapi 仍是软依赖：缺失时置 None。
# 同理，ASR 端点用到的 ``UploadFile``/``File``/``WebSocket`` 也必须在此登记：
# 它们若只在 ``_register_asr`` 函数体内 import，签名注解会变成无法解析的 ForwardRef
# ⇒ pydantic ``TypeAdapter is not fully defined`` ⇒ 路由根本注册不上。
try:
    from fastapi import HTTPException, Request          # noqa: F401
    from fastapi import File, UploadFile, WebSocket, WebSocketDisconnect  # noqa: F401
except Exception:                                       # pragma: no cover
    Request = None                                      # type: ignore[assignment]
    HTTPException = None                                # type: ignore[assignment]
    UploadFile = File = WebSocket = WebSocketDisconnect = None  # type: ignore[assignment]


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
    # ---- B3 前后端分离：跨域与鉴权 ----
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
    # ---- #5 可观测性：默认开启，不改功能行为，只新增日志输出——此前全仓无 handler，
    #      INFO 级日志被静默丢弃，运维等于瞎的。 --------------------------------
    p.add_argument("--log-level", default="INFO",
                   help="日志级别（DEBUG/INFO/WARNING/ERROR；默认 INFO）")
    p.add_argument("--log-format", default="text", choices=("text", "json"),
                   help="text=人读（行内含 rid/session/run）；json=单行 JSON 供采集侧解析")
    args = p.parse_args(argv)
    # 生产加固：token 与 hardened 支持环境变量注入，镜像/compose 默认形态不必把密钥写进 CMD。
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


# ---- 插件配置覆盖 ----
# 会话级化：覆盖文件升级为 v2 分 scope 结构，
# 读写一律经 ``ConfigStore``。
# 两个旧函数名保留为薄封装 —— 语义不变，避免全仓调用点改动。
_OVERRIDE_FILE = Path("data/agent_plugin_config.json")

# 进程内唯一真源：``_merge_cfg`` / 设置接口 / ``session.cfg_view`` 接缝共用同一份。
# 惰性访问：``_OVERRIDE_FILE`` 可能被重定向，
# 直接固化路径会让重定向失效——故按「路径变了就重建」保持单一真源。
_STORE = ConfigStore(path=_OVERRIDE_FILE)


def _store() -> ConfigStore:
    """当前覆盖存储。"""
    global _STORE
    if Path(_STORE.path) != Path(_OVERRIDE_FILE):
        _STORE = ConfigStore(path=_OVERRIDE_FILE)
    return _STORE


def _load_overrides() -> dict:
    """读取用户对插件运行时配置的容器级覆盖。"""
    return _store().container()


def _save_overrides(data: dict) -> None:
    """整份写回容器级覆盖。"""
    _store().replace_container(data)


# 覆盖节名 → ``config["agent"]`` 下的实际配置节的特例表。
# 通用规则是同名，但 ``plugins.guard`` 是例外：
# guard 是系统层硬装配插件，其矩阵的 SSOT 节叫 ``permissions``
# 策略收口后设置页的 ``targets``/``default``/``allow``/``deny`` 改挂 ``plugins.guard``，
# 若无此特例它们会落到不存在的 ``agent.guard`` ⇒ 静默失效。
# 这条特例是显式的：通用规则保持干净，不让任何插件去猜自己的节名。
_SECTION_ALIAS: dict[str, str] = {"plugins.guard": "permissions"}


def _merge_cfg(args: argparse.Namespace) -> dict:
    """基础 config+ 用户覆盖，注入 build_agent。

     覆盖按 ``plugins.<名>`` → ``agent.<名>`` 映射并入对应插件配置节；``_SECTION_ALIAS`` 里的节名走特例。
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
        # 生产改造 #4：加固画像 —— deny-by-default，显式白名单才放行。
        # build_agent 内的 deep_merge 递归合并，仅覆盖 permissions 的 default / task_allow，
        # 保留 task_deny / llm 等键。
        perms = agent.setdefault("permissions", {})
        perms["default"] = "deny"
        perms["task_allow"] = []
    return cfg


# ---- 设置项元数据 ----
# G2：校验/归属/默认/作用域四件事实合并为一张表。
# 此前散在 _SETTING_SCHEMA / _PLUGIN_OF_KEY / _SETTING_DEFAULTS 三处，加 scope 维度后三表 + scope 必然漂移——故先合并再扩展。
# 每键四项：
# spec : 校验规则
# plugin : 归属插件节——决定落 ``plugins.<名>``
# default : 默认值，与 profiles.BASE_AGENT_CONFIG 同源，避免硬编码漂移
# scope : 允许写入的作用域层。``"session"`` = 可写到「本会话」；
# ``"container"`` = 仅容器级。
# context 节默认值单一真相源，与 profiles.BASE_AGENT_CONFIG 同源，避免漂移
_CTX_DEFAULTS = ((BASE_AGENT_CONFIG.get("agent") or {}).get("context") or {})
_BUDGET_DEFAULTS = ((BASE_AGENT_CONFIG.get("agent") or {}).get("budget") or {})
_REACT_DEFAULTS = ((BASE_AGENT_CONFIG.get("agent") or {}).get("react") or {})

_SETTING_META: dict[str, dict] = {
    # ---- 短期记忆----
    "compact_trigger_tokens": {
        "spec": (int, 256, 1_000_000), "plugin": "plugins.short_term",
        "default": 12000, "scope": "session"},
    # ---- 上下文窗口与配比----
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
    # ---- S3：循环内上下文治理 ----
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
    # ---- G2.0：双窗口解耦 + 水位 + 层 cap ----
    "working_window_tokens": {
        "spec": (int, 1, 1_000_000), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("working_window_tokens", 96000),
        "scope": "session"},                           # 经济窗口
    "window_high_ratio": {
        "spec": (float, 0.01, 0.99), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("window_high_ratio", 0.70),
        "scope": "session"},                           # 触发水位
    "window_low_ratio": {
        "spec": (float, 0.01, 0.99), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("window_low_ratio", 0.40),
        "scope": "session"},                           # 回落目标
    "resident_cap_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("resident_cap_tokens", 8000),
        "scope": "session"},                           # 常驻层 cap
    "on_demand_cap_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("on_demand_cap_tokens", 8000),
        "scope": "session"},                           # 按需层 cap
    "prompt_cap_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("prompt_cap_tokens", 2000),
        "scope": "session"},                           # 输入层 cap
    # ---- G2.0：治理动词----
    "compact_mode": {
        "spec": ("choice", ["off", "shadow", "on"]), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("compact_mode", "shadow"),
        "scope": "session"},                           # 默认 shadow
    "clear_at_least_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.context",
        "default": _CTX_DEFAULTS.get("clear_at_least_tokens", 4000),
        "scope": "session"},                           # 单次清除的最小释放量
    # ---- 审批插件：通道 / 超时 / 必问名单 ----
    # 容器级：provider/timeout 属「装配与运行通道」语义，
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
    # ---- 成本护栏----
    "max_tokens_per_run": {
        "spec": (int, 1000, 10_000_000), "plugin": "plugins.budget",
        "default": _BUDGET_DEFAULTS.get("max_tokens_per_run", 200000),
        "scope": "session"},                           # 本会话累计 LLM token 上限
    "finalize_reserve_tokens": {
        "spec": (int, 0, 1_000_000), "plugin": "plugins.budget",
        "default": _BUDGET_DEFAULTS.get("finalize_reserve_tokens", 0),
        "scope": "session"},                           # 方案A：收尾预留额度
    "cost_calibration": {
        "spec": ("bool",), "plugin": "plugins.budget",
        "default": _BUDGET_DEFAULTS.get("cost_calibration", True),
        "scope": "container"},                         # F1：口径校准是模型属性，不随会话拆
    # ---- 模型生成上限----
    # 容器级：属「模型属性」，会话化无意义；改后重建容器生效。
    # 调大可为长正文预留空间、降低 reasoning 占满触顶导致的空响应
    "max_tokens": {
        "spec": (int, 1024, 1_000_000), "plugin": "plugins.llm",
        "default": 8192, "scope": "container"},
    # ---- 工具懒加载----
    # 容器级：属「暴露层装配」开关，会话化会让同一模型的工具面在不同会话里漂移。
    "lazy_tools": {
        "spec": ("bool",), "plugin": "plugins.react",
        "default": _REACT_DEFAULTS.get("lazy_tools", False), "scope": "container"},
    # ---- 域级裁剪----
    # 硬前提：仅 lazy_tools=True 时生效。空白名单视同未配置。
    "tool_domains_enabled": {
        "spec": ("bool",), "plugin": "plugins.react",
        "default": _REACT_DEFAULTS.get("tool_domains_enabled", False),
        "scope": "container"},
    # 陷阱：tool_domains 的默认值必须是 []，不能是 None —— _coerce_setting 的
    # "list" 分支对非 str/list 走 ``parts=[raw]``，None 会被收成字符串 "None"。
    "tool_domains": {
        "spec": ("list",), "plugin": "plugins.react",
        "default": _REACT_DEFAULTS.get("tool_domains", []),
        "scope": "container"},                         # 放行域白名单，owner，如 plugins.windows_mcp
    # ---- P0-2：续接前缀对齐----
    "alignment_enabled": {
        "spec": ("bool",), "plugin": "plugins.react",
        "default": _REACT_DEFAULTS.get("alignment_enabled", True),
        "scope": "container"},
    # ---- 执行闸门两旋钮----
    # 背景：P0-1 实测 298 run → 触顶 64 次中 ``step_limit`` 占 24，
    # 触顶任务平均已用 5.2 步。而此前 max_steps 只存在于配置文件、设置页不可见，
    # 用户想拧只能手改 json。
    # 默认值不能从 _REACT_DEFAULTS 取：max_steps 的真实默认在
    # ``plugins/react/__init__.py`` 的 ``cfg.get("max_steps", 8)``，
    # ``profiles.BASE_AGENT_CONFIG`` 的 react 节没有这个键。
    # 口径纪律：此处与那处必须一致；改此处须同步改 react/__init__.py。
    "max_steps": {
        "spec": (int, 1, 200), "plugin": "plugins.react",
        "default": 8, "scope": "container"},
    # 同批放行 deadline_s：它与 max_steps 是成对的执行闸，
    # 只暴露一个会让用户"拧完步数又撞时限"，反而更难归因。
    "deadline_s": {
        "spec": (float, 5.0, 3600.0), "plugin": "plugins.react",
        "default": 120.0, "scope": "container"},
    # ---- 准入名单四键----
    # 键名取裸名：设置层是扁平单值命名空间，
    # 由 _PLUGIN_OF_KEY 保证不歧义。``risk`` 刻意不进设置页——已核实 MCP 工具
    # 全部是 write 档，放上去是个无效旋钮，只走配置文件。
    # 归属改为 ``plugins.guard``：准入策略的唯一持有者是 guard。此前挂 ``plugins.sandbox`` 造成两份名单
    # 并存、互相打洞。
    # 落到 ``agent.permissions`` 靠 ``_SECTION_ALIAS`` 特例。
    # ``targets`` 默认从 ``["mcp.*"]`` 改为 ``["*"]``：与 ``sys.guard.DEFAULT_POLICY``
    # 同源。
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
    # ---- 隔离档位----
    # 归属 ``plugins.sandbox``⇒ 经 ``_SECTION_ALIAS`` 同名映射到
    # ``agent.sandbox``，正是 ``sys`` 侧 ``_config()`` 读的那一节。
    # 取值域与 ``agent/plugins/sandbox/__init__.py::MODES`` 逐字一致。
    "mode": {
        "spec": ("choice", ["none", "read-only", "workspace-write"]),
        "plugin": "plugins.sandbox", "default": "none", "scope": "session"},
}

# 以下三张表为派生视图——单一真相源是 _SETTING_META。
_SETTING_SCHEMA = {k: m["spec"] for k, m in _SETTING_META.items()}
_PLUGIN_OF_KEY = {k: m["plugin"] for k, m in _SETTING_META.items()}
_SETTING_DEFAULTS = {k: m["default"] for k, m in _SETTING_META.items()}
# 允许写到「本会话」层的键：设置接口据此拒绝把容器级键写进会话覆盖。
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
    if kind == "list":                                     # 列表：ask 名单
        # ``None`` 必须视为「空」而不是 ``["None"]``：
        # 前端把未填写的设置项以 ``null`` 提交时，原实现会落一个字面量 ``"None"`` 幽灵条目
        # ——对 ``approval.ask``/ ``tool_domains`` 这类键，
        # 表现为"多出一个永远匹配不到的域"，且用户看不见来源。
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
    """可用工具域清单。

     = 当前已注册域 ∪ 配置里已选但当前未注册的域。

     后半段是必要的而非补充：域随插件启用/停用而增减，若只渲染已注册域，
     「配置里已勾选、但该插件当前停用」的域会因控件不存在而在保存时被静默丢弃
     —— 用户会看到自己的选择无声消失。

     域 ≡ 工具注册来源 ``owner``；
     ``owner`` 为空串的工具不计入清单。
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


# ---- G2：接缝注入 -------------------------------------------------
# 解耦纪律：插件不得 import 适配层。会话配置经 ctx 接缝传递——
# ``view(section, session_key) -> dict``。插件侧 ctx.get 软取，
# 缺失即退化为容器级单实例 ⇒ 未启用会话覆盖时行为与改造前逐值等价。
# 接缝名取自 ``agent.api.SESSION_CFG_VIEW``。


def _budget_view(root, ident, container_cfg: dict) -> dict:
    """状态栏预算视图：本会话已用 + 本会话上限。

     G2 前 ``BUDGET_SPENT`` 是容器单值 ⇒ 状态栏看到的其实是「所有会话合计」；
     现改为按会话取，上限亦走同一份会话配置视图。
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
    """把会话配置视图挂到根容器。"""
    if root is None:
        return
    try:
        # 传 lambda 而非 ``_store().view`` 绑定：存储被重定向后
        # 接缝仍指向当前真源，不会读到旧实例。
        root.provide(SESSION_CFG_VIEW, lambda section, sk=None: _store().view(section, sk))
    except Exception:                        # noqa: BLE001 —— 接缝不可用时降级为容器级
        pass


# 无头模式

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


# Web 形态
# 前端已独立为 web/ 工程，
# 服务端不再内联页面；改由 _serve() 末尾挂载 StaticFiles
# 托管 web/dist/。
# 开发期仍走 Vite dev server + proxy，托管不参与。



async def _context_view_payload(root, *, limit: int = 60) -> dict:
    """Inspector ``/context`` 视图。

     纯只读旁路：从审计脊柱 ``agent_events`` 读 ``agent/context/governed`` 载荷 → 经治理器
     门面软调用 ``gov.ledger`` 重建 ``RoundLedger`` → 渲染分类视图 + 逐端点 I-4 对账。
     消费方不 import 插件内部。轮次归集：每个 ``stage`` 取
     最近一次装配，即「这一轮钱花在哪」。无治理器 / 渲染失败 → ``available=False``。
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
    for p in payloads:                              # 每个 stage 取最近一次
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
    """#5 可观测性中间件：给每个 HTTP 请求绑定 request id、回带响应头、打访问日志。

         刻意做成模块级函数：``_serve`` 返回时容器已
         teardown ⇒ 单测无法在进程内对真实 app 发请求；抽出来才能被测试直接调用，
         测的是生产实现本身而不是复刻品。

         - 绑定发生在 ``call_next`` 之前 ⇒ 下游都能通过 ContextVar 读到同一个 id；
         - 计数：请求数 + 5xx/异常数；
         - 访问日志：每请求一行，字段由 record factory 自动
         带上 rid/session/run ⇒ 排查时先 grep rid，再看这一行。
         嫌吵可把 ``--log-level`` 提到 WARNING。

         访问日志里的 ``session`` 恒为空：会话键由下游路由任务绑定，而 ``BaseHTTPMiddleware`` 会把下游跑在另一个 task ⇒ 绑定
         不回流到本层。``rid`` 在本层绑定、对下游可见 ⇒ 关联键是 rid；
         要看该请求的会话，查 ``/api/events?request_id=<rid>`` 的 ``session`` 列。
         """
    rid = bind_request_id()
    # 同时把 rid 暂存到 ``request.state``：ContextVar 在 ``BaseHTTPMiddleware`` 把下游
    # 跑在另一个 task 时不会回流到异常处理器上下文，直接读 ``current_request_id()``
    # 会拿到空串。异常处理器/端点改从 ``request.state.rid`` 取。
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
    """从 ``request.state.rid`` 取请求标识，兜底 ``current_request_id()``。

     为什么不用裸 ``current_request_id()``：``BaseHTTPMiddleware`` 把路由处理跑在独立 task，
     异常处理器上下文里 ContextVar 可能已回退为空串；``request.state.rid`` 在同一次请求内
     始终可读。
     """
    rid = getattr(getattr(request, "state", None), "rid", "") or ""
    return rid or current_request_id()


async def _security_headers_middleware(request, call_next):
    """#4 生产级安全响应头。

     刻意做成模块级函数，便于单测直接调用。

     - ``X-Content-Type-Options: nosniff``：阻止 MIME 嗅探。
     - ``X-Frame-Options: DENY``：禁止被嵌入 iframe。
     - 对 ``/api/`` 数据面额外加：
     * ``Cache-Control: no-store``：响应含会话/事件等敏感数据，禁止任何缓存落盘；
     * ``Content-Security-Policy: default-src 'none'``：API 只返回 JSON，不需要任何
     子资源加载，收紧到最小。

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
    """该会话是否已有 run 在跑；是则返回那个 run_id，否则 None。

     H2：同会话并发是真实可达的，
     后果有二，两条都是静默错误：
     1. 两个 run 抢同一个 ``react_chain:{session_key}`` ⇒ 链互相覆盖；
     2. 后一个请求的 ``_probe_resume`` 看到前一个的 ``running`` 快照 ⇒ 把它标
     ``orphaned``，而那个 run 还在跑。
     故入口处查一次、忙则 409 诚实拒绝。

     实现上刻意反查现有 ``active_runs`` 而不另建一张表：
     少一份状态、且天然与取消端点共用同一真相。

     顺带修掉一个能力缺口：``/api/chat`` 此前不登记 ``active_runs``
     ⇒ 它既查不到别人、也无法被 ``/api/chat/cancel`` 取消。现已登记。
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
    """H2-TOCTOU 修复：原子的「查忙 → 登记」。

     此前 ``/api/chat`` 与 ``/api/chat/stream`` 先在入口 ``_session_busy`` 查忙，
     再在若干 await 之后才 ``active_runs[run_id] = ...`` 登记占位/真实任务。
     两条同会话请求可双双通过忙检、各自登记 ⇒ 两个 run 抢同一 ``react_chain``、
     后者还会把前者标 orphaned。

     本函数用 ``_RUN_LOCK`` 把「查忙 + 登记占位 marker」包成原子段：
     任一时刻只有一个协程能进入，登记完成前别的请求查到的仍是「未登记」，
     故第二个请求必然命中 busy 分支拿 409。

     返回：
     ``("ok", run_id, marker)`` —— 登记成功，marker 是占位任务
     ``("busy", busy_run_id, None)`` —— 该会话已有 run 在跑，调用方应回 409 并带上 busy_run_id
     """
    async with _RUN_LOCK:
        busy = _session_busy(active_runs, session_key)
        if busy is not None:
            return ("busy", busy, None)
        run_id = uuid.uuid4().hex[:8]
        marker = asyncio.ensure_future(asyncio.sleep(0))   # 占位任务，立即被替换
        active_runs[run_id] = (marker, session_key)
        return ("ok", run_id, marker)

@dataclass
class HandlerDeps:
    """路由处理器的依赖包。

         此前四个 ``_register_*`` 各带 7~11 个参数，且共用同一组闭包项。
         这带来两个真问题：

         1. 加一个依赖要改 4 处签名 + 2 处调用：漏一处 ⇒ 该函数体内名字未定义
         ⇒ 调用即 ``NameError`` ⇒ 该端点 500。
         2. 依赖只能由真跑 ``_serve`` 才能构造：它们全部是 ``_serve`` 的局部闭包，
         要凑齐就得真起 uvicorn + 真实装配容器 ⇒ handler 脱离真实进程就测不了，
         HTTP 层覆盖率因此长期钉在低位。

         收成单一对象后：依赖集合一处声明、一处构造；且测试可用替身构造它，把路由注册到一个全新的
         ``FastAPI()`` 上直接打端点——不需要 uvicorn，也不需要真实容器。

         刻意不含 ``_session_cfg``：它只被 ``_serve`` 内部的 ``_identity`` 使用；
         经 AST 逐个函数核实，三个域函数的函数体均未引用它 ⇒ 继续转发是死参数。
         """

    c: object                 # ``_Box`` 容器句柄；handlers 一律经 ``c.root`` 取服务
    active_runs: dict         # run_id -> (Task, 会话键)；取消/断连清理用
    pm: object                # PluginManager
    identity: Callable        # 请求身份解析
    apply_rebuild: Callable   # 按当前配置重建容器，管理操作即时生效
    sse: Callable             # SSE 帧序列化 (event, payload) -> str
    approval_or_none: Callable  # 取审批门面
    JSONResponse: type
    Request: type
    StreamingResponse: type
    # : 访问令牌。全局 http 中间件不拦 WS 握手
    # : ⇒ WS 侧必须自行校验，否则开了
    # : ``--token`` 的服务上 ``/api/asr/ws`` 会裸奔。空串 = 未启用鉴权。
    token: str = ""


def _register_probes(app, deps):
    """探针 + 会话流路由。

     从 ``_register_routes`` 拆出。调用顺序由
     ``_register_routes`` 固定：探针 → 会话流 → approval → 资源。
     """
    c = deps.c
    active_runs = deps.active_runs
    _identity = deps.identity
    _sse = deps.sse
    JSONResponse = deps.JSONResponse
    StreamingResponse = deps.StreamingResponse
    # ---- 生产改造 #2：健康/就绪/指标探针----
    # 必须在 SPA mount("/") 之前注册，否则被 catch-all 吞成 index.html。
    # 顶层路径即便启用 --token 也由中间件豁免。
    from datetime import datetime, timezone
    from fastapi.responses import Response

    @app.get("/health")
    async def _health() -> dict:
        """liveness 探针：恒 200，不依赖任何内部状态。"""
        return {"status": "ok", "ts": datetime.now(timezone.utc).isoformat()}

    @app.get("/ready")
    async def _ready():
        """readiness 探针：只查硬依赖，不查可选插件层。
         用 container.has 查注册态，不触发懒加载实例化；任一硬依赖缺失即 503。"""
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
        """per-session 指标开关。"""
        if os.environ.get("PRISM_METRICS_SESSION_LABELS", "").strip().lower() in (
                "1", "true", "on", "yes"):
            return True
        cfg = ((c.root.config or {}).get("agent") or {}).get("metrics") or {}
        return bool(cfg.get("session_labels"))

    def _prom_label(value: object) -> str:
        """Prometheus 标签值转义。"""
        return (str(value).replace("\\", "\\\\").replace('"', '\\"')
                .replace("\n", "\\n"))

    @app.get("/metrics")
    async def _metrics() -> Response:
        """极简 Prometheus 文本指标。"""
        budget_cfg = ((c.root.config or {}).get("agent") or {}).get("budget") or {}
        # BUDGET_SPENT 已按会话分桶。metrics 保持「容器总计」语义，
        # 需要 per-session 明细时打开 PRISM_METRICS_SESSION_LABELS=1。
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
        # ---- #5 可观测性：请求/事件计数----
        for name, value in sorted(counters_snapshot().items()):
            lines += [f"# HELP {name} 进程内累计计数",
                      f"# TYPE {name} counter",
                      f"{name} {value:g}"]
        for owner, n in sorted(owners.items()):
            if owner:
                lines.append(f'agent_tools_by_owner{{owner="{owner}"}} {n}')
        # per-session 预算明细——默认关闭。开启方式二选一：环境变量 ``PRISM_METRICS_SESSION_LABELS=1``
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

         H2：本端点此前既不登记 active_runs、也不查会话是否在跑
         ⇒ 同一会话连发两条 ⇒ 两个 run 抢 ``react_chain:{session_key}`` 互相覆盖，
         且第二个请求的 ``_probe_resume`` 会把正在跑的第一个 run 标 orphaned。
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
        """B5 流式：SSE 逐帧推送。

         与 B6 共用同一 run 生命周期：本端点持有任务并登记 run_id，
         取消端点据此 cancel 并校验归属。``/api/chat`` 保留不动。

         2.2：会话标识经 ``X-Session-Id`` 请求头。

         H2：同会话已有 run 在跑时诚实拒绝 409。
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
        # H4：此处曾传无界队列，绕过了 ``sys/progress.py`` 自建队列的
        # 1024 丢帧保护
        # ⇒ 慢客户端/断连时 token 高频帧无限堆积，吃满内存。
        # 现在有界；``_push`` 满时丢最旧并计数，worker 的终止帧另走
        # ``_put_terminal``。
        q: asyncio.Queue = asyncio.Queue(maxsize=_STREAM_QUEUE_MAXSIZE)

        async def _put_terminal(frame: dict) -> None:
            """放终止帧：满了丢最旧再放，绝不 await 阻塞。

             为什么不能直接 ``await q.put``：队列满时它会挂住 ⇒ worker 永不返回 ⇒
             run 永远停在 active_runs 里。
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
                # 先订阅，再起任务
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
        """B6 取消：按 run_id 取消在跑的那轮。

                 2.2：校验归属——run 只可被它所属的会话取消；跨会话取消返回 403，否则任一会话凭 run_id 就能掐掉别人的任务。
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


def _register_approval(app, deps):
    """审批 + 只读状态路由。

     含 ``/api/approval/*`` 与只读状态端点
     ``/api/status`` ``events`` ``context`` ``perms`` ``session``
     ``session/reset`` ``tools`` ``memory``。

     划分依据：这三个域都是"读状态 / 走审批"，与 ``_register_probes``
     和 ``_register_resources`` 区分。

     从 ``_register_routes`` 拆出。调用顺序由
     ``_register_routes`` 固定：probes → approval(本函数) → resources。
     本函数必须在 ``mount("/")`` 之前调用。
     """
    c = deps.c
    _identity = deps.identity
    _approval_or_none = deps.approval_or_none
    JSONResponse = deps.JSONResponse
    @app.post("/api/approval/answer")
    async def _approval_answer(body: dict, request: Request):
        """人对在途请求的应答：审批与澄清问询共用。

                 经待答表 resolve 唤醒在途等待者；附加键原样透传，由等待方按自身语义消费。
                 未知/已了结的 call_id → 404。
                 fail-safe 在插件侧：无人应答/超时一律默认拒绝，本端点只投喂不裁决。

                 2.2：校验归属——只能应答本会话的在途请求，跨会话应答返回 403。
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
        if body.get("always"):                       # 第 3 期：批量确认，本会话同类不再问
            answer["always"] = True
        note = body.get("note")                      # 第 3 期：计划拒绝意见
        if isinstance(note, str) and note.strip():
            answer["note"] = note.strip()[:500]
        rw = body.get("kwargs")                      # 第 3 期：updated_input
        if isinstance(rw, dict):
            answer["kwargs"] = rw
        if "answered" in body:                       # 澄清问询：answered/choice/text 透传
            answer["answered"] = bool(body.get("answered"))
        if "choice" in body:
            answer["choice"] = body.get("choice")
        cs = body.get("choices")                     # 澄清问询：取值列表透传
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
        """在途审批快照。

         2.2：只返回本会话的在途请求。
         """
        ident = _identity(request)
        svc = _approval_or_none()
        if svc is None:
            return {"items": [], "enabled": False}
        return {"items": svc.pending.snapshot(ident.session_key), "enabled": True,
                "mode": svc.mode()}

    @app.post("/api/approval/mode")
    async def _approval_mode(body: dict, request: Request):
        """切换本会话档位。

         OPEN-5：切 bypass 属高危，需 ``confirm=true`` 二次确认。

         R1 补：档位已按会话分桶，故本端点必须解析身份——此前不解析，`set_mode` 写的是容器级单值 ⇒ 任何会话改档位
         都会改到全局。
         """
        _identity(request, body)                  # 解析身份
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
                # 2.2：回显身份
                "tenant": ident.tenant, "user": ident.user, "session": ident.session,
                # 预算按会话统计
                "budget": _budget_view(c.root, ident, budget_cfg)}

    @app.get("/api/events")
    async def _events(request: Request, request_id: str = "", limit: int = 120) -> dict:
        """durable 事件账本。

                 #5：``request_id`` 非空时只回该请求的痕迹——这就是"可排查、可串联"的落点：
                 拿到响应头 ``X-Request-Id`` 即可拉出那一轮的全部事件。

                 生产级多租户隔离：``request_id`` 为空时不再回吐全局账本，改为只回本会话最近事件。要跨请求串联想用 ``request_id`` 精确查。
                 ``limit`` 上限 500。
                 """
        ident = _identity(request)                    # 解析身份
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

    # ---- G2.0：Inspector ``/context`` 视图----

    @app.get("/api/context")
    async def _context_view(request: Request, limit: int = 60) -> dict:
        """把 governed 遥测合并为轮次全景账本并渲染 ``/context`` 分类视图。

                 ：补 ``_identity(request)`` 解析身份——与 ``/api/perms`` 同理由，
                 未绑定请求作用域会让 ContextVar 落到上一次聊天的会话残留态，视图口径不确定。
                 """
        _identity(request)                        # 解析身份
        return await _context_view_payload(c.root, limit=limit)

    @app.get("/api/perms")
    async def _perms(request: Request) -> dict:
        """权限视图：策略 + 最近判定 + 本会话档位。

         R1 补：``mode`` 已按会话分桶，故本端点必须解析身份——否则
         ContextVar 未绑定 ⇒ 走到回退分支读到上一次聊天请求留下的会话，
         前端显示的档位与实际生效的档位可能不是同一个会话的。
         """
        _identity(request)                        # 解析身份
        recent = await c.root.db.query(
            c.root, "SELECT seq, payload, at FROM agent_events"
                    " WHERE event='agent/permission/decision' ORDER BY seq DESC LIMIT 30")
        approval = c.root.get(Services.APPROVAL) if c.root.has(Services.APPROVAL) else None
        return {"policy": c.root.guard.policy(c.root), "recent": recent,
                "mode": approval.mode() if approval else None}

    @app.get("/api/session")
    async def _session(request: Request) -> dict:
        """当前会话，2.2：按请求身份取，不再是进程级全局会话。"""
        ident = _identity(request)
        return {"session_id": ident.session_key, "tenant": ident.tenant,
                "user": ident.user,
                "history": await c.root.chat.history(c.root, ident.session_key)}

    @app.post("/api/session/reset")
    async def _session_reset(request: Request):
        """清空当前会话的转写。"""
        ident = _identity(request)
        cleared = await c.root.chat.reset(c.root, ident.session_key)
        return JSONResponse({"ok": True, "cleared": cleared})

    @app.get("/api/tools")
    async def _tools(request: Request) -> dict:
        # ：补 ``_identity(request)`` —— 与全量 /api 路由一致地解析请求作用域，
        # 避免 ContextVar 落到上一次聊天的会话残留态。
        _identity(request)
        return {"tools": c.root.tasks.catalog()}

    # ---- 记忆 ----
    # 解耦纪律：消费面只用 ctx.has(Services.X) 守卫 + 门面方法，不 import 任何插件内部模块。

    @app.get("/api/memory")
    async def _memory(request: Request) -> dict:
        ident = _identity(request)
        sid = ident.session_key                    # 2.2：各层按当前会话统计
        layers: list[dict] = []

        # 第 1 层 系统记忆
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

        # 第 2 层 短期记忆
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

        # 第 3 层 工作记忆
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

        # 长期记忆：独立子块，不影响三层展示
        long_term = {"enabled": c.root.has(Services.MEMORY), "stats": {}, "rows": []}
        if c.root.has(Services.MEMORY):
            try:
                long_term["stats"] = await c.root.memory.stats(c.root)
                long_term["rows"] = await c.root.memory.list_recent(c.root, limit=40)
            except Exception:  # noqa: BLE001
                long_term["stats"], long_term["rows"] = {"error": "stats failed"}, []

        return {"layers": layers, "long_term": long_term}

    # ---- 插件管理 ----


def _register_resources(app, deps):
    """插件与设置路由。

     ``/api/plugins``、``/api/plugins/{enable,scaffold,remove}``、
     ``/api/settings``。

     与 ``_register_approval`` 的分界：那边是审批 + 只读状态，这边是
     会触发插件重建的配置写入。

     从 ``_register_routes`` 拆出。调用顺序由
     ``_register_routes`` 固定：probes → approval → resources(本函数)。
     本函数必须在 ``mount("/")`` 之前调用。
     """
    c = deps.c
    pm = deps.pm
    _identity = deps.identity
    _apply_rebuild = deps.apply_rebuild
    JSONResponse = deps.JSONResponse
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
            # 行结构统一：目录插件与内置 manifest 两种来源都同时给 id + name
            rows.append({**d, "id": d["name"], "desired": e, "actual": a, "delta": delta,
                         "status": status_map.get(d["name"])})
        for m in manifest:
            # 含 system 层插件一同展示。
            # 仅按 discovered 去重，避免目录插件与其 manifest 项重复出现。
            if m["id"] in discovered_ids:
                continue
            rows.append({**m, "name": m["id"], "desired": True, "actual": True,
                         "delta": "system/builtin",
                         "status": status_map.get(m["id"])})
        return {"plugins": rows, "state_exists": pm.state_file.exists()}

    async def _bg_rebuild() -> None:
        """后台重建容器。失败仅记录，不回滚期望态。"""
        try:
            await _apply_rebuild()
        except Exception as exc:  # noqa: BLE001
            # #5：改走 logger
            logging.getLogger("agent.chat").error(
                "后台重建失败（期望态已保存，实际装配可能需重启）：%s", exc)

    async def _mutate_then_fetch(background: bool = False) -> dict:
        # 先返回期望态已更新的当前快照。
        # 容器重建代价高，含 MCP 子进程拉起等，常需数十秒，故改为后台执行，
        # 避免管理请求长时间阻塞、前端“点了没反应”。前端会显示“处理中…”并在稍后自动刷新。
        res = await _plugins()
        if background:
            # 登记到模块级集合：退出时可观测/排障；任务完成自动移除。
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
        # 无论重建成功与否都返回最新插件状态，保证前端即时刷新表格
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

    # ---- 设置：上下文管理 ----

    def _setting_values(ov: dict) -> dict:
        """把一份覆盖解析成键值。"""
        out: dict = {}
        for k in _SETTING_SCHEMA:
            cur = (ov.get(_PLUGIN_OF_KEY.get(k, ""), {}) or {}).get(k, _SETTING_DEFAULTS[k])
            try:
                out[k] = _coerce_setting(k, cur)
            except (ValueError, TypeError):
                out[k] = _SETTING_DEFAULTS[k]
        return out

    def _session_raw(sk: str) -> dict:
        """本会话显式覆盖——「继承 vs 覆盖」据此区分。"""
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
        # 三份视图——容器级生效值 / 本会话显式覆盖 / 二者合成的生效值。
        # ``values`` 保持旧名 = effective；新增三个字段供
        # 设置页区分「容器级 / 本会话级」并渲染「重置为继承」。
        values = _setting_values(_store().effective(sk))
        ap_enabled = "plugins.approval" in set(pm.enabled())
        # 沙箱插件默认关⇒ 设置页据此提示"未启用"，与 context/approval 同款
        sb_enabled = "plugins.sandbox" in set(pm.enabled())
        # 沙箱隔离档位与强度。前端必须拿到自报的实际强度
        # 才能渲染徽标 —— 缺这个字段时前端只能猜"正常"，那正是此前最大的误导源。
        # 经 ``container.plugin_statuses()`` 取，
        # 插件未启用时如实报 ``none``，前端据此显示「⚠ 无隔离」。
        sb_status: dict = {"mode": "none", "enforcement": "none", "gap": False,
                           "targets": ["mcp.*"], "reason": "沙箱插件未启用",
                           "backend": "null", "enforced_by": "none",
                           "backend_error": False, "unportable_targets": []}
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
                # 落点：声明档位时，外部工具子进程被挪到会话私有目录
                # 与 `enforcement` 分开报——重定向
                # 不是强制，报成 partial 才是谎报。
                # 这两个字段只回答一件事："东西落在哪"。
                "sandbox_workspace": sb_status.get("workspace", ""),
                "sandbox_env_redirected": bool(sb_status.get("env_redirected")),
                # P4/P5/P8：实际生效的后端与强制者。
                # 没有这四个字段，前端只能显示"无隔离"——但后端已经能兑现
                # partial/full 了，不显示等于把"有隔离"藏起来。
                "sandbox_backend": sb_status.get("backend", "null"),
                "sandbox_enforced_by": sb_status.get("enforced_by", "none"),
                "sandbox_backend_error": bool(sb_status.get("backend_error")),
                # 在该后端下跑不了、已被单独降级为无隔离的桥
                # 必须可见，否则运维会以为全都隔离了。
                "sandbox_unportable_targets": sb_status.get("unportable_targets") or [],
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
            # scope：``container``| ``session``。
            scope = str((body or {}).get("scope") or "container").strip().lower()
            if scope not in ("container", "session"):
                return JSONResponse({"ok": False, "error": f"未知 scope: {scope}"},
                                    status_code=400)
            ident = _identity(request)
            sk = ident.session_key
            if bool(body.get("reset")):
                if scope == "session":
                    # 只清本会话覆盖 ⇒ 逐键回落到容器/默认；不重建容器
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
                # 会话级写入不触发 _apply_rebuild：门面/预算/压缩阈值都在请求时现取
                # 会话配置视图 ⇒ 下个请求自然生效。
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


# : 上传音频大小上限：长录音常见 1–2 小时 wav
#:，200MB 留够余量。
ASR_UPLOAD_MAX_BYTES = 200 * 1024 * 1024


def _register_asr(app, deps):
    """语音识别路由。

         路由归属：``/api/asr/*`` 走软取 ``Services.ASR``——插件
         未启用时 ``_asr_or_none()`` 返回 ``None`` ⇒ 端点回 503 明示"插件未启用"，
         不崩服务。

         两个端点都不含任何编排/下游：ASR 只做"音频→文本"。

         WS 协议
         -----------------
         客户端 → 服务端：
         - 二进制帧：int16 PCM
         - 文本帧 JSON：``{"type":"start"}`` / ``{"type":"stop"}``
         服务端 → 客户端：
         ``{"type":"ready"|"loading"|"partial"|"final"|"error", …}``

         WS 内的鉴权：全局 http 中间件不拦 WS 握手⇒ 这里按 ``/api/`` 同款规则自行校验 ``--token``，否则开了鉴权的服务上 WS 会裸奔。
         """
    c = deps.c
    JSONResponse = deps.JSONResponse

    def _asr_or_none():
        if not c.root.has(Services.ASR):
            return None
        return c.root.get(Services.ASR)

    def _token_ok(ws) -> bool:
        """WS 侧 token 校验。"""
        expected = str(getattr(deps, "token", "") or "")
        if not expected:
            return True                      # 未启用鉴权 ⇒ 放行
        import hmac

        got = str(ws.query_params.get("token") or "")
        if not got:
            auth = str(ws.headers.get("authorization") or "")
            if auth[:7].lower() == "bearer ":
                got = auth[7:].strip()
        return hmac.compare_digest(got, expected)

    @app.get("/api/asr/status")
    async def _asr_status():
        """只读：引擎是否就绪。"""
        svc = _asr_or_none()
        if svc is None:
            return JSONResponse({"ok": False, "available": False,
                                 "error": "ASR 插件未启用"}, status_code=503)
        return JSONResponse({"ok": True, "available": True, "status": svc.status()})

    @app.post("/api/asr/transcribe-file")
    async def _asr_transcribe_file(file: UploadFile = File(...)):
        """模式 F：长录音文件 → ``Segment[]``。

         - 格式支持以 libsndfile 为准；m4a/aac 不支持，
         读取失败如实回 400；
         - 超过 ``ASR_UPLOAD_MAX_BYTES``⇒ 413；
         - 落盘到 ``data/asr_uploads/`` 临时目录，转写结束即删。
         """
        svc = _asr_or_none()
        if svc is None:
            return JSONResponse({"ok": False, "error": "ASR 插件未启用"}, status_code=503)

        from agent.api import EngineUnavailable

        name = str(getattr(file, "filename", "") or "audio.wav")
        suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ".wav"
        upload_dir = Path("data/asr_uploads")
        upload_dir.mkdir(parents=True, exist_ok=True)
        target = upload_dir / f"{uuid.uuid4().hex}{suffix}"
        max_bytes = ASR_UPLOAD_MAX_BYTES
        written = 0
        try:
            with open(target, "wb") as fh:
                while True:
                    chunk = await file.read(1 << 20)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > max_bytes:
                        return JSONResponse(
                            {"ok": False, "error": "文件超过 200MB 上限"},
                            status_code=413)
                    fh.write(chunk)
            loop = asyncio.get_running_loop()
            try:
                result = await svc.transcribe_path(str(target), {})
            except FileNotFoundError as exc:
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
            except EngineUnavailable as exc:
                inc_counter("agent_asr_unavailable_total")
                return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
            except (ValueError, RuntimeError) as exc:
                # 解不动/损坏的音频：libsndfile 会抛这些 ⇒ 明确报错，不谎报"空结果"
                return JSONResponse(
                    {"ok": False, "error": f"无法解析音频（{suffix}）：{exc}"},
                    status_code=400)
            inc_counter("agent_asr_transcribe_file_total")
            payload = result.to_dict()
            return JSONResponse({"ok": True, **payload})
        finally:
            # 用户音频不留存
            try:
                target.unlink(missing_ok=True)
            except Exception:                   # noqa: BLE001
                pass

    @app.websocket("/api/asr/ws")
    async def _asr_ws(ws: WebSocket):
        """模式 R：流式识别。见函数 docstring 的协议表。"""
        if not _token_ok(ws):
            await ws.close(code=4401)
            return
        await ws.accept()
        svc = _asr_or_none()
        if svc is None:
            await ws.send_json({"type": "error", "error": "ASR 插件未启用"})
            await ws.close(code=1011)
            return
        from agent.api import EngineUnavailable

        await ws.send_json({"type": "loading"})
        try:
            await svc.ensure_ready()
        except EngineUnavailable as exc:
            # 前端据此降级到 Web Speech ⇒ 必须给可读原因，不是 500
            await ws.send_json({"type": "error", "error": str(exc)})
            await ws.close(code=1011)
            return

        session = None
        try:
            await ws.send_json({"type": "ready"})
            while True:
                try:
                    msg = await ws.receive()
                except WebSocketDisconnect:
                    break
                mtype = msg.get("type")
                if mtype == "websocket.disconnect":
                    break
                # 二进制帧 = PCM
                if msg.get("bytes") is not None:
                    if session is None:
                        session = svc.open_stream()
                    frames = await svc.feed_stream(session, msg["bytes"])
                    for f in frames:
                        await ws.send_json(f)
                    continue
                text = msg.get("text")
                if text is None:
                    continue
                try:
                    ctrl = json.loads(text)
                except (ValueError, TypeError):
                    continue
                ctype = str(ctrl.get("type") or "")
                if ctype == "stop":
                    if session is not None:
                        for f in await svc.finish_stream(session):
                            await ws.send_json(f)
                        session = None
                    break
                # start 帧：仅重置会话
                if ctype == "start" and session is not None:
                    session.reset()
        except EngineUnavailable as exc:
            try:
                await ws.send_json({"type": "error", "error": str(exc)})
            except Exception:                   # noqa: BLE001
                pass
        except WebSocketDisconnect:
            pass
        finally:
            if session is not None:
                try:
                    session.reset()
                except Exception:               # noqa: BLE001
                    pass
            inc_counter("agent_asr_ws_connections_total")


def _register_routes(app, deps):
    """注册全部 HTTP 路由。

         本函数只做编排：按固定顺序把同一个 ``HandlerDeps`` 交给各域函数。
         ① 探针/会话流 → ② approval → ③ 资源→ ④ asr。
         ⑤ 静态托管 ``mount("/")`` 由 ``_serve`` 在本函数之后追加。

         依赖经 ``HandlerDeps`` 整体传递：不再在此逐个
         转发闭包项 ⇒ 新增依赖只改 ``HandlerDeps`` 一处，不会再出现"某个域函数漏收
         一个参数 ⇒ 函数体 NameError ⇒ 该端点 500"。

         等价性守卫：``.workbuddy/verify_serve_split_equiv.py``。
         新增 asr 后该守卫的基线需同步——它是显式基线文件，
         不是自动发现的断言。
         """
    _register_probes(app, deps)
    _register_approval(app, deps)
    _register_resources(app, deps)
    _register_asr(app, deps)
    from agent.run.skills_routes import register_skills_routes
    register_skills_routes(app, deps)

async def _serve(args: argparse.Namespace) -> int:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

    # 生产加固 fail-fast：`--hardened` 语义是"deny-by-default"，缺 token 会让所有工具调用
    # 被 guard 拒绝 ⇒ 服务虽起但不可用，且"看起来在跑"极具误导性。在装配之前就
    # 拒绝。
    if getattr(args, "hardened", False) and not str(args.token or ""):
        raise SystemExit(
            "--hardened 已启用但未提供 token：deny-by-default 下所有工具调用（含 chat）都会被拒。"
            "请同时提供 --token 或环境变量 PRISM_TOKEN。")

    # ---- #5 可观测性：最先装日志----
    # 放在这里是为了让 ``create_app`` 内部的所有日志
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
    # 2.2：不再有「进程级全局会话」——原 ``session_id = uuid4().hex[:8]`` 是启动时生成
    # 一次的闭包常量，所有请求共用。现改为每请求经 ``_identity()`` 解析。
    # B6/B5 共用：run_id -> (在跑的任务, 归属会话键)。取消端点据此 cancel 并校验归属
    # H4：否则任一会话都能取消别人的 run；断连时清理，防孤儿任务。
    active_runs: dict[str, tuple[asyncio.Task, str]] = {}

    # ---- 阶段 2：请求级身份解析 -----------------------------------------
    # 信任边界：tenant/user 只来自服务端配置；
    # session 来自请求头 ``X-Session-Id``。
    # 键构造/净化的唯一真源是 ``core/session.py``——任何非法标识在此 fail-fast，
    # 不静默回退，否则用户以为在私密会话，实则落进共享桶 / 前缀扫描越权命中他人。
    # R1：本函数据此成为请求作用域绑定的单一漏斗——解析成功即把会话键
    # 绑到 ContextVar。消费方多为无 ctx 入参的事件
    # 回调或装配期捕获 ctx 的服务，它们读不到请求参数，
    # 只能靠该通道；``ctx.state`` 仍照写，但不再是读取主通道。
    # ⇒ 新增端点若需要会话归属，必须经本函数解析身份。

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
        """一帧 SSE。帧类型见设计方案。"""
        return "data: " + json.dumps({"event": event, "payload": payload or {}},
                                     ensure_ascii=False) + "\n\n"

    async def _apply_rebuild() -> None:
        """按当前 config+ 当前插件期望态重建容器。"""
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

    # ---- 审批：入站应答 + 在途快照 + 会话档位 ----
    # 解耦纪律：只经 Services.APPROVAL 门面，不 import 插件内部模块。
    # 定义在这里而非 `_register_approval` 内 —— 因为 `c`是`_Box`，
    # 插件重建后 `c.root` 会变；放模块级会捕获旧容器。三个注册函数都靠参数拿到它。
    def _approval_or_none():
        if not c.root.has(Services.APPROVAL):
            return None
        return c.root.get(Services.APPROVAL)

    app = FastAPI(title="Agent Harness", docs_url=None, redoc_url=None)

    # ---- R3：全局异常兜底-----------------------------------
    # 此前未注册任何 exception_handler ⇒ 非 HTTPException 的未捕获异常落到
    # Starlette 默认处理器，返回 HTML 500：前端 `res.json()` 直接解析失败，
    # 且不留统一日志。这里统一转成 JSON 错误体，并记一条带路径的 error 日志。
    # 注意：HTTPException 不在此接管——Starlette 默认已返回 JSON `{"detail": ...}`，
    # 覆盖它会改变 400/404 的既有响应体形状，属于无收益的行为变更。

    @app.exception_handler(AgentError)
    async def _on_agent_error(request: Request, exc: AgentError) -> JSONResponse:
        # AgentError 是内核受控错误族，
        # 故仍回显其消息；但补 request_id 便于报障，并在服务端落完整日志。
        rid = _req_rid(request)
        _err_log.error("AgentError @ %s rid=%s: %s", request.url.path, rid, exc)
        return JSONResponse({"error": str(exc), "kind": type(exc).__name__,
                             "request_id": rid}, status_code=500)

    @app.exception_handler(Exception)
    async def _on_unhandled(request: Request, exc: Exception) -> JSONResponse:
        # 生产级：未捕获的意外异常绝不回显 str(exc)，
        # 改为通用体 + request_id；完整异常仅留服务端 traceback。
        rid = _req_rid(request)
        _err_log.exception("未捕获异常 @ %s rid=%s", request.url.path, rid)
        return JSONResponse({"error": "服务内部错误，请稍后重试或凭 request_id 报障",
                             "request_id": rid}, status_code=500)

    # ---- #5 可观测性：每请求绑定 request id ---------------------------------
    # 装在所有路由之外的中间件里 ⇒ 无论端点是否经 ``_identity`` 解析身份
    # 都能拿到本次请求的 id。
    # 账本与日志
    # 都从这一处绑定取值 ⇒ 一次请求的痕迹可按同一 id 串起来。
    # 响应头回带 ``X-Request-Id``：用户报障时可直接给出该值定位到那一轮。
    # 实现见模块级 ``_obs_middleware``。
    app.middleware("http")(_obs_middleware)
    # ---- #4 安全响应头----
    app.middleware("http")(_security_headers_middleware)

    # ---- 跨域------------------------------------------------
    # 默认「不启用」：沿用 dev 下的 Vite proxy，零行为变化。
    # 前后端直连部署时显式传 --cors-origin 打开。
    # 注意：allow_credentials=True 时 不可 用 "*"，
    # 故此处按实际来源逐一回显——这也是「默认空 = 不启用」的另一层理由。
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

    # ---- 访问令牌--------------------------------------------
    # 默认「不鉴权」：本地 dev 与既有测试零影响。
    # 启用后支持两种携带方式。
    _AUTH_EXEMPT = {"/", "/api/status"}       # 健康探针：首页与状态接口始终放行
    _token = str(args.token or "")

    def _extract_token(request) -> str:
        """按 `Authorization: Bearer x` → `?token=x` 顺序取令牌。"""
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
            # 1) OPTIONS 预检 —— CORS 预检不带 Authorization，拦了 CORS 全废；
            # 2) 显式豁免的探针 —— `/`首页与`/api/status`；
            # 3) 非 `/api/` 路径 —— B4 起这些是前端静态产物。
            # 浏览器用 <script>/<link> 加载它们时无法携带 token，若拦则整页白屏；
            # 且静态产物本就是发给浏览器的公开资源，不构成额外信息泄露。
            # 真正的数据面一律照旧校验，安全强度不变。
            if (request.method == "OPTIONS"
                    or path in _AUTH_EXEMPT
                    or not path.startswith("/api/")):
                return await call_next(request)
            # 常量时间比较：避免按字符计时侧信道
            if not hmac.compare_digest(_extract_token(request), _token):
                return _JSONResponse({"error": "未授权：缺少或无效的 token"},
                                     status_code=401)
            return await call_next(request)

    # 路由注册已抽到模块级 `_register_routes`。
    # 等价性守卫：`.workbuddy/verify_serve_split_equiv.py`。
    # 依赖包一处构造：此后新增依赖只需改 HandlerDeps，不再逐个函数改签名。
    deps = HandlerDeps(
        c=c, active_runs=active_runs, pm=pm,
        identity=_identity, apply_rebuild=_apply_rebuild, sse=_sse,
        approval_or_none=_approval_or_none,
        JSONResponse=JSONResponse, Request=Request,
        StreamingResponse=StreamingResponse, token=_token)
    _register_routes(app, deps)
    # ---- B4：单端口托管前端产物 ------------------------------------
    # 放在全部 /api/* 路由注册之后：Starlette 按注册顺序匹配，先注册者优先。
    # 故 `/api/*` 照旧命中上面的路由，只有剩余路径落到这里。
    # 开发期不受影响：日常开发仍走 Vite dev server+ proxy，本挂载只在
    # 「单端口部署」形态下生效；产物缺失时给出明确指引而非静默 404。
    _DIST = Path(__file__).resolve().parents[2] / "web" / "dist"

    if _DIST.is_dir():
        from fastapi.staticfiles import StaticFiles

        class _SPAStaticFiles(StaticFiles):
            """StaticFiles + 对 HTML 响应加 ``Cache-Control: no-store``。

             为什么必须 no-store：产物文件名带内容哈希。
             浏览器若启发式缓存了 ``index.html``，改版后会继续引用已不存在的旧 hash
             文件 → 整页白屏。B2 期实测过同类问题。
             带哈希的静态资源本身可长缓存，故只干预 text/html。
             """

            async def get_response(self, path, scope):  # type: ignore[override]
                resp = await super().get_response(path, scope)
                ctype = resp.headers.get("content-type", "")
                if ctype.startswith("text/html"):
                    resp.headers["Cache-Control"] = "no-store"
                    # #4 纵深防御：应用 HTML 此前无 CSP，
                    # 而前端是 v-html / innerHTML 结构，安全性全押在手写 esc() 上。
                    # 这里补一层：script 只允许同源，
                    # style 放开 'unsafe-inline'。
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
            # 不静默 404：告诉使用者缺什么、怎么补，避免"页面打不开但不知为啥"
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
    # 只要「无 token 且权限开放」即打印醒目 WARNING。
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
    # #5：走 logger，级别由 --log-level 决定；设为 WARNING 及以上时不再打印此行
    logging.getLogger("agent.chat").info("Agent Harness 就绪: %s   （Ctrl+C 退出）", url)
    # ---- #5 可观测性：uvicorn 日志级别跟随----
    config = uvicorn.Config(app, host=args.host, port=args.port,
                            log_level=str(args.log_level).lower())
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # 禁用 uvicorn 自带信号处理器，下面自己接管
    task = asyncio.create_task(server.serve())

    # ---- #1 SIGTERM/SIGINT 优雅退出 ----
    # uvicorn 自带处理器已被禁用，这里注册双信号；收到后设置 should_exit 并取消 serve 任务，
    # 使 finally 中的 c.teardown() 一定执行，避免孤儿进程累积。
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
        # Windows：asyncio 不支持 add_signal_handler。
        # 回退 signal.signal：handler 在主线程同步执行，必须经 call_soon_threadsafe
        # 把置位动作扔回事件循环线程，否则正 await server.serve() 的循环不会被唤醒。
        signal.signal(signal.SIGTERM, lambda *_: _graceful())
        signal.signal(signal.SIGINT, lambda *_: _graceful())
        # 控制台下 CTRL_BREAK / taskkill 不带 /F 走 SIGBREAK，一并接管
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
        # ---- 优雅退出：先排空在跑的会话任务，再 teardown，避免孤儿进程/半截写入----
        # 关 SIGTERM 后 uvicorn 已停止 accept，但已建立的流式连接在 ``task`` 取消时才断；
        # 这里显式取消 active_runs 里未完的 run，
        # 等它们干净退出再 teardown。
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
        # 后台容器重建任务也一并等待，避免 teardown 时它还在动容器
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
