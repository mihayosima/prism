"""agent.profiles —— 装配（profile→patch 有序叠层，与 Cordis 同构）。

build_agent(config, patch)：
  1) BASE_AGENT_CONFIG ->> config ->> patch 深度合并（patch 最后覆盖）；
  2) SYS_PLUGINS + BUSINESS_PLUGINS 进同一 Container（builtin=config）。

两个 profile（A5 审查采纳）：
  - offline：默认矩阵全开 → 不挡 T0/T1/T2 默认路径；
  - tight：矩阵收窄（deny agent.ping）→ 给 guard 造真实「非默认全开」拒绝场景。

记忆四层**全部**不在系统层，均为 agent/plugins/ 下的目录插件（opt-in）：
  - plugins.system_memory（第1层·文件身份）/ plugins.memory（第2层·长期知识）
  - plugins.work_memory（第3层·任务进度）/ plugins.short_term（第4层·对话历史）
经「插件」页可停用 / 启用 / 卸载；停用后对应 ctx.has(...) 为 False，消费方软降级。
"""

from __future__ import annotations

from typing import Any

from agent.api import DEFAULT_DB_PATH, Container, coerce_config, validate_config
from agent.sys.budget import Budget
from agent.sys.chat import Chat
from agent.sys.events_plugin import EventsPlugin
from agent.sys.guard import Guard
from agent.sys.llm import LlmPlugin
from agent.sys.loop import Loop
from agent.sys.storage import Storage
from agent.sys.tasks import TasksPlugin

BASE_AGENT_CONFIG: dict = {
    # 真源在 core/config_schema.DEFAULT_DB_PATH（勿在此硬编码，避免两处漂移）
    "db_path": DEFAULT_DB_PATH,
    "agent": {
        "llm": {"provider": "fixture", "model": "fixture", "stream": True,
                "max_tokens": 8192},   # E：显式生成上限（不设则服务端默认 8K 腰斩长参数）
        "budget": {"max_tokens_per_run": 200000, "reset_on_new_session": True,
                   # 方案A：收尾预留额度——须 ≥ 收尾调用的窗口估算（治理器封顶 2 万）+ 余量
                   "finalize_reserve_tokens": 24000,
                   # F1：口径自校准（EWMA 因子作用于 budget 预借与治理器装配）
                   "cost_calibration": True},
        # P2/L：react 续接词表与懒加载默认（react 节 _any 免 schema，但默认值须有单一真相源）
        # P0-2：alignment_enabled＝续接前缀对齐（头部快照冻结）；默认开启，可就地回退
        "react": {"resume_keywords": ["继续", "接着", "续写", "continue"],
                  "lazy_tools": False, "lazy_tools_max": 12,
                  # G 域级裁剪（路线 2）：投影层按「域」(owner) 裁剪工具暴露面。
                  # enabled 默认 **关**（与 lazy_tools 同款灰度纪律，未启用逐字节等价现状）；
                  # 白名单为空视同未配置（**不做全隐藏**，避免一处笔误把工具全关掉）。
                  # 硬前提：仅 lazy_tools=True 时生效——非懒加载无检索通道，裁剪＝死锁。
                  # 详见 docs/reports/context/工具税域级裁剪实施方案-2026-09-30.md
                  "tool_domains_enabled": False,
                  "tool_domains": [],
                  "alignment_enabled": True},
        # G2.0（P1）：context 节默认值**唯一真相源**（governor.from_config / 设置页同源引用）。
        # 双窗口解耦：max_context_tokens 是**能力上限**（1M），working_window_tokens 是
        # **经济基数**（水位分母）——两者量纲不同，绝不可互相顶替（本次事故根因）。
        "context": {
            "max_context_tokens": 24000, "reserve_output_tokens": 4000,
            "memory_ratio": 0.10, "history_ratio": 0.60, "source_clip_tokens": 8192,
            "clip_max_tokens": 1000, "clip_head_tokens": 500, "clip_tail_tokens": 500,
            "react_keep_recent": 6, "react_min_chain_tokens": 2048,
            "working_window_tokens": 96000,
            "window_high_ratio": 0.70, "window_low_ratio": 0.40,
            "resident_cap_tokens": 8000, "on_demand_cap_tokens": 8000,
            "prompt_cap_tokens": 2000,
            # G2.0（P2）：治理动词——默认 ``shadow``（照常计算决策但不执行，只写遥测；
            # 这是让压缩阈值用真实数据校准的唯一安全方式）；``clear`` 最小释放量 4000。
            "compact_mode": "shadow", "clear_at_least_tokens": 4000,
        },
        "permissions": {"default": "open", "task_allow": ["*"], "task_deny": [], "llm": "open"},
        "approval": {"provider": "web", "allow": ["*"], "deny": [], "ask": [],
                      "timeout_s": 30, "default": "deny"},
        "loop": {"deadline_s": 60},
        # 阶段 2（2.0）：工作记忆持久化 + 跨进程 run 快照。agent.work_memory 为 _any（免 schema），
        # 但默认值须有单一真相源，故显式收口于此（原为插件内 hardcode 默认）。
        # - enabled/ttl/prefix：既有键（收口，不改语义）。
        # - required：True 时 Redis 不可用 → **启动失败**（required 语义）；False（默认，保 dev 兼容）
        #   → 降级 InMemory + 启动 WARNING。生产部署建议置 True。
        # - snapshot_ttl_s：跨进程 run 快照（run_snapshot:{tenant}:{task_id}）TTL，2.3 回滚窗口上界。
        # - snapshot_chain_keep：快照内 chain 保留「最近 N 轮 + 头部快照」，防单 key 膨胀（D19）。
        "work_memory": {"enabled": True, "ttl": 3600, "prefix": "wm:",
                        "required": False, "snapshot_ttl_s": 86400,
                        "snapshot_chain_keep": 20},
        # 阶段 2（2.2）：会话/租户身份的**服务端配置面**。
        # - tenant/user **只来自此处**（客户端可伪造 ⇒ 不下放）；
        # - session **不由此处决定**：默认 ``default``，实际值来自请求头 ``X-Session-Id``
        #   （无隔离语义，伪造只影响伪造者自己）。三者的键构造规则见 ``core/session.py``。
        "session": {"tenant": "default", "user": "default"},
    },
}

TIGHT_PATCH: dict = {"agent": {"permissions": {"task_deny": ["agent.ping"]}}}

LOW_BUDGET_PATCH: dict = {"agent": {"budget": {"max_tokens_per_run": 60}}}

# 记忆四层**全部**是目录插件（opt-in），不在系统层硬装配：
#   system_memory / work_memory / short_term / memory —— 经「插件」页启停，与 sys 层解耦。
SYS_PLUGINS = (EventsPlugin, TasksPlugin, Storage, LlmPlugin, Budget, Guard)
BUSINESS_PLUGINS = (Loop, Chat)


def deep_merge(base: dict, overlay: dict | None) -> dict:
    """深度合并 == 全量独立拷贝：结果不引用 base/overlay 的任何可变子值。

    防止测试/装配期间对配置的运行期修改（如矩阵收窄、deadline）污染模块级常量
    （BASE_AGENT_CONFIG / TIGHT_PATCH 等）——每个容器拿到一份自己的配置副本。
    """
    import copy

    out = copy.deepcopy(dict(base))
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def build_agent(*, config: dict | None = None, patch: dict | None = None,
                plugin_dir: str | None = None,
                state_file: str | None = None,
                plugin_mgr: Any = None,
                extra_plugins: tuple = (), builtin_data: dict | None = None) -> Container:
    """装配一个 agent 容器（profile 叠层）。

    - 系统层（SYS_PLUGINS）+ 内置业务（BUSINESS_PLUGINS：loop/chat）恒定装配；
    - 目录插件**显式 opt-in**：仅当传入 ``plugin_mgr`` / ``plugin_dir`` 才装配
      `agent/plugins/` 下【启用】的目录插件（业务插件，可像引擎一样启停/新增/卸载）。
      默认不扫目录 → 测试/无头入口封闭、不误拉真实子进程。
    """
    cfg = coerce_config(config or {}, defaults=BASE_AGENT_CONFIG)
    cfg = deep_merge(cfg, patch or {})
    validate_config(cfg)                      # P2：配置键/类型 fail-fast
    builtin = {"config": cfg}
    if builtin_data:
        builtin.update(builtin_data)
    container = Container(builtin=builtin, verify_wiring=True)   # P3：显式开接线校验（默认即 True，写清便于审计）
    container.add(*SYS_PLUGINS, *BUSINESS_PLUGINS, *extra_plugins)
    pm = plugin_mgr
    if pm is None and plugin_dir:
        from agent.sys.plugin_manager import PluginManager
        pm = PluginManager(plugin_dir=plugin_dir, state_file=state_file)
    if pm is not None:
        container.add(*pm.load())
    return container


__all__ = [
    "BASE_AGENT_CONFIG", "LOW_BUDGET_PATCH", "TIGHT_PATCH",
    "build_agent", "deep_merge",
]
