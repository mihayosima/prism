"""agent.profiles —— 装配（profile→patch 有序叠层，与 Cordis 同构）。

build_agent(config, patch)：
  1) BASE_AGENT_CONFIG ->> config ->> patch 深度合并（patch 最后覆盖）；
  2) SYS_PLUGINS + BUSINESS_PLUGINS 进同一 Container（builtin=config）。

两个 profile（A5 审查采纳）：
  - offline：默认矩阵全开 → 不挡 T0/T1/T2 默认路径；
  - tight：矩阵收窄（deny agent.ping）→ 给 guard 造真实「非默认全开」拒绝场景。

记忆（plugins.memory）不在系统层：它是 agent/plugins/memory 下的目录插件，
默认启用，经「插件」页可停用 / 启用 / 卸载；停用后 ctx.has("memory") 为 False。
"""

from __future__ import annotations

from typing import Any

from agent.api import Container
from agent.core.config_schema import validate_config, coerce_config
from agent.sys.budget import Budget
from agent.sys.chat import Chat
from agent.sys.events_plugin import EventsPlugin
from agent.sys.guard import Guard
from agent.sys.llm import LlmPlugin
from agent.sys.loop import Loop
from agent.sys.storage import Storage
from agent.sys.tasks import TasksPlugin

BASE_AGENT_CONFIG: dict = {
    "db_path": "data/agent.db",
    "agent": {
        "llm": {"provider": "fixture", "model": "fixture"},
        "budget": {"max_tokens_per_run": 200000, "reset_on_new_session": True},
        "permissions": {"default": "open", "task_allow": ["*"], "task_deny": [], "llm": "open"},
        "loop": {"deadline_s": 60},
    },
}

TIGHT_PATCH: dict = {"agent": {"permissions": {"task_deny": ["agent.ping"]}}}

LOW_BUDGET_PATCH: dict = {"agent": {"budget": {"max_tokens_per_run": 60}}}

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