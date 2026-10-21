"""agent.profiles —— 装配。

build_agent(config, patch)：
 1) BASE_AGENT_CONFIG ->> config ->> patch 深度合并；
 2) SYS_PLUGINS + BUSINESS_PLUGINS 进同一 Container。

两个 profile：
 - offline：默认矩阵全开 → 不挡 T0/T1/T2 默认路径；
 - tight：矩阵收窄→ 给 guard 造真实「非默认全开」拒绝场景。

记忆四层全部不在系统层，均为 agent/plugins/ 下的目录插件：
 - plugins.system_memory/ plugins.memory
 - plugins.work_memory/ plugins.short_term
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
    # 真源在 core/config_schema.DEFAULT_DB_PATH，勿在此硬编码，避免两处漂移
    "db_path": DEFAULT_DB_PATH,
    "agent": {
        "llm": {"provider": "fixture", "model": "fixture", "stream": True,
                "max_tokens": 8192},   # 显式生成上限
        "budget": {"max_tokens_per_run": 200000, "reset_on_new_session": True,
                   # 方案 A：收尾预留额度——须 ≥ 收尾调用的窗口估算+ 余量
                   "finalize_reserve_tokens": 24000,
                   # 口径自校准
                   "cost_calibration": True},
        # P2/L：react 续接词表与懒加载默认
        # P0-2：alignment_enabled＝续接前缀对齐；默认开启，可就地回退
        "react": {"resume_keywords": ["继续", "接着", "续写", "continue"],
                  "lazy_tools": False, "lazy_tools_max": 12,
                  # G 域级裁剪：投影层按「域」(owner) 裁剪工具暴露面。
                  # enabled 默认 关；
                  # 白名单为空视同未配置，不做全隐藏，避免一处笔误把工具全关掉。
                  # 硬前提：仅 lazy_tools=True 时生效——非懒加载无检索通道，裁剪＝死锁。
                  "tool_domains_enabled": False,
                  "tool_domains": [],
                  "alignment_enabled": True},
        # context 节默认值唯一真相源。
        # 双窗口解耦：max_context_tokens 是能力上限，working_window_tokens 是
        # 经济基数——两者量纲不同，绝不可互相顶替。
        "context": {
            "max_context_tokens": 24000, "reserve_output_tokens": 4000,
            "memory_ratio": 0.10, "history_ratio": 0.60, "source_clip_tokens": 8192,
            "clip_max_tokens": 1000, "clip_head_tokens": 500, "clip_tail_tokens": 500,
            "react_keep_recent": 6, "react_min_chain_tokens": 2048,
            "working_window_tokens": 96000,
            "window_high_ratio": 0.70, "window_low_ratio": 0.40,
            "resident_cap_tokens": 8000, "on_demand_cap_tokens": 8000,
            "prompt_cap_tokens": 2000,
            # 治理动词——默认 ``shadow``；``clear`` 最小释放量 4000。
            "compact_mode": "shadow", "clear_at_least_tokens": 4000,
        },
        "permissions": {"default": "open", "task_allow": ["*"], "task_deny": [], "llm": "open"},
        "approval": {"provider": "web", "allow": ["*"], "deny": [], "ask": [],
                      "timeout_s": 30, "default": "deny"},
        "loop": {"deadline_s": 60},
        # 阶段 2：工作记忆持久化 + 跨进程 run 快照。agent.work_memory 为 _any，
        # 但默认值须有单一真相源，故显式收口于此。
        # - enabled/ttl/prefix：既有键。
        # - required：True 时 Redis 不可用 → 启动失败；False
        # → 降级 InMemory + 启动 WARNING。生产部署建议置 True。
        # - snapshot_ttl_s：跨进程 run 快照 TTL，2.3 回滚窗口上界。
        # - snapshot_chain_keep：快照内 chain 保留「最近 N 轮 + 头部快照」，防单 key 膨胀。
        "work_memory": {"enabled": True, "ttl": 3600, "prefix": "wm:",
                        "required": False, "snapshot_ttl_s": 86400,
                        "snapshot_chain_keep": 20},
        # 阶段 2：会话/租户身份的服务端配置面。
        # - tenant/user 只来自此处；
        # - session 不由此处决定：默认 ``default``，实际值来自请求头 ``X-Session-Id``
        # 三者的键构造规则见 ``core/session.py``。
        "session": {"tenant": "default", "user": "default"},
    },
}

TIGHT_PATCH: dict = {"agent": {"permissions": {"task_deny": ["agent.ping"]}}}

LOW_BUDGET_PATCH: dict = {"agent": {"budget": {"max_tokens_per_run": 60}}}

# 记忆四层全部是目录插件，不在系统层硬装配：
# system_memory / work_memory / short_term / memory —— 经「插件」页启停，与 sys 层解耦。
SYS_PLUGINS = (EventsPlugin, TasksPlugin, Storage, LlmPlugin, Budget, Guard)
BUSINESS_PLUGINS = (Loop, Chat)


def deep_merge(base: dict, overlay: dict | None) -> dict:
    """深度合并 == 全量独立拷贝：结果不引用 base/overlay 的任何可变子值。

 防止测试/装配期间对配置的运行期修改污染模块级常量
 ——每个容器拿到一份自己的配置副本。
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
    """装配一个 agent 容器。

 - 系统层+ 内置业务恒定装配；
 - 目录插件显式 opt-in：仅当传入 ``plugin_mgr`` / ``plugin_dir`` 才装配
 `agent/plugins/` 下【启用】的目录插件。
 默认不扫目录 → 测试/无头入口封闭、不误拉真实子进程。
 """
    cfg = coerce_config(config or {}, defaults=BASE_AGENT_CONFIG)
    cfg = deep_merge(cfg, patch or {})
    validate_config(cfg)                      # 配置键/类型 fail-fast
    builtin = {"config": cfg}
    if builtin_data:
        builtin.update(builtin_data)
    container = Container(builtin=builtin, verify_wiring=True)   # 显式开接线校验
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
