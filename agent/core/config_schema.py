"""agent.core.config_schema —— 配置 schema 校验（让拼错键 / 类型不符在装配期即报错）。

设计原则（与 P1 / P3 一致：单一真相源 + fail-fast）：
- 默认值唯一真相源是 ``BASE_AGENT_CONFIG``（profiles.py）；本模块**不重复定义**默认值，
  仅通过 ``coerce_config(cfg, defaults=...)`` 引用它做结构补齐。
- ``coerce_config`` 仅做**无损归一**（补缺失节 + 独立深拷贝），**不做字符串猜测式类型转换**；
- ``validate_config`` 显式递归校验：未知键 / 类型不符 → 抛 ``ConfigError``（fail-fast）。
- 插件自管节（``windows_mcp``、``agent.memory``、``agent.react``）标 ``"_any"`` 宽松放行，
  避免过约束插件扩展点；内核托管节（llm/budget/permissions/loop）严格校验，
  正是拼错键（如 ``budet``、``max_token_per_run``）最高危处。
"""

from __future__ import annotations

import copy
from typing import Any

from .errors import ConfigError


# SQLite 库路径的**唯一默认值真源**（2026-10-04 收口）。
#
# 此前 ``data/agent.db`` 在 ``profiles.py::BASE_AGENT_CONFIG`` 与 ``sys/storage.py``
# 的兜底取值处**各硬编码一份**，两处漂移时表现为「配置改了但库没换」，排查需翻两处。
# 现两处统一引用本常量。
#
# ⚠️ 与 ``run/chat.py`` 的 ``--db`` 默认（``data/agent_chat.db``）**刻意不同**，不是 bug：
#   - 本常量服务于 ``build_agent()`` 的离线/嵌入式/测试用法；
#   - 服务器入口（``python -m agent.run.chat``）用独立库，避免长驻进程与离线用法
#     抢同一个文件（WAL 争用 + 测试写入污染服务端数据）。
# 两个库 schema 完全相同（同一套 ``MIGRATIONS``），切换入口即可，不需要迁移。
DEFAULT_DB_PATH = "data/agent.db"


# 声明式 schema：节 -> {键: 期望类型}。``"_any"`` 表示该节/键由插件自管，宽松放行。
_SCHEMA: dict[str, Any] = {
    "db_path": str,
    "agent": {
        "llm": {"provider": str, "model": str, "stream": bool, "max_tokens": int},
        "budget": {"max_tokens_per_run": int, "reset_on_new_session": bool,
                   "finalize_reserve_tokens": int, "warning_ratio": (int, float),
                   "cost_calibration": bool},   # 方案A：收尾预留额度
        # 准入矩阵的唯一持有者 = guard（2026-10-03 收口：模式匹配与域限定从 sandbox 迁入）。
        # ``targets`` 默认 ``["*"]`` = 全部（等价于收窄前的行为），支持尾部 ``*`` 通配。
        # ⚠️ ``llm`` 声明为 ``str`` 而**不是**枚举——``core`` 层的 ``_matches`` 只支持类型规约
        # （枚举校验的既有先例在 ``run/chat.py::_coerce_setting`` 的 choice 分支，属设置层）。
        # 取值域由 ``sys/guard.py::_LLM_MODES``（``open``/``deny``）在**代码层**收：
        # 未知值回落 open 并计数，不再"任何非 open 值都锁死全部模型调用"。
        "permissions": {"default": str, "task_allow": list, "task_deny": list, "llm": str,
                        "risk": dict,          # S5：按风险级裁决 {"read":"allow","danger":"deny",...}
                        "targets": list,       # 域限定；不在此列表的工具完全不过问
                        # ★ 2026-10-10 补登：`allow`/`deny` 是**设置页已注册的可写键**
                        #   （`run/chat.py::_SETTING_META`，归属 `plugins.guard`，经
                        #   `_SECTION_ALIAS` 映射到本节），但本节漏登记 ⇒ 用户在设置页
                        #   保存准入名单时 `validate_config` fail-fast 报
                        #   「未知配置键 'allow'（不在 schema 中）」，保存整体失败。
                        #   责任在 schema 漏登记（G7 那笔加了设置键却没同步这里），
                        #   与本仓「同一事实 N 处同步」的纪律相符：补齐即可。
                        "allow": list, "deny": list},
        "loop": {"deadline_s": (int, float)},
        # 阶段 2（2.2）：会话/租户身份的服务端配置面（session 另从请求头取，不在此配置）。
        # 严格校验（非 _any）：拼错键（如 ``tennat``）在装配期即 fail-fast。
        "session": {"tenant": str, "user": str},
        # G2.0（P1-2）：context 节由 ``_any`` **补成具体键类型**（与 budget 节同纪律）——
        # 拼错键（如 ``working_windows_tokens``）在装配期即 fail-fast，不再静默落默认值。
        "context": {
            # 既有键（G0/G1/S3）
            "max_context_tokens": int, "reserve_output_tokens": int,
            "memory_ratio": (int, float), "history_ratio": (int, float),
            "source_clip_tokens": int,
            "clip_max_tokens": int, "clip_head_tokens": int, "clip_tail_tokens": int,
            "react_keep_recent": int, "react_min_chain_tokens": int,
            # G2.0（P1）：双窗口解耦 + 水位 + 层 cap
            "working_window_tokens": int,
            "window_high_ratio": (int, float), "window_low_ratio": (int, float),
            "resident_cap_tokens": int, "on_demand_cap_tokens": int,
            "prompt_cap_tokens": int,
            # G2.0（P2）：治理动词（模式枚举 + clear 最小释放量）
            "compact_mode": str, "clear_at_least_tokens": int,
        },
        # 插件自管节
        "memory": "_any",
        "react": "_any",
        "approval": "_any",
        "system_memory": "_any",
        "work_memory": "_any",
        "short_term": "_any",
        # 技能子系统（plugins.skills）与只读技能 MCP 桥（plugins.skills_mcp）。
        # ★ 必须登记：``_merge_cfg`` 把容器级覆盖按 ``plugins.<名>`` → ``agent.<名>`` 映射，
        #   而 ``agent`` 节是**严格校验**节（非 ``_any``）⇒ 漏登记时用户一写配置就在装配期
        #   ``ConfigError`` ⇒ 服务起不来（与 sandbox 节同一教训）。顶层另有一份同名
        #   ``skills_mcp`` 登记（沿用 windows_mcp / playwright_mcp 的既有先例）。
        "skills": "_any",
        "skills_mcp": "_any",
        # 语音识别（plugins.asr）：``_any`` 宽松节，理由与 skills/skills_mcp 同款——
        # ``provider`` / ``sample_rate`` / ``num_threads`` / ``streaming`` / ``file`` / ``vad``
        # 等键全部由插件自管（嵌套节还在各子层自行校验），标严格节只会让漏登记变成
        # 装配期 ``ConfigError``（服务起不来）。**必须登记**：``_merge_cfg`` 把容器级
        # 覆盖按 ``plugins.<名>`` → ``agent.<名>`` 映射进来，而 ``agent`` 节是严格校验节。
        "asr": "_any",
        # G7 外部工具隔离（plugins.sandbox）：**逐键严格**而非 "_any"（沿用 context/budget 的纪律：
        # 拼错键在装配期即 fail-fast）。登记这一节是**硬前提**——``_merge_cfg`` 把容器级
        # 覆盖按 ``plugins.<名>`` → ``agent.<名>`` 映射进来，而 ``validate_config`` 在每次
        # 装配都跑；不登记 ⇒ 用户一写 sandbox 配置就是 ``ConfigError`` ⇒ 服务起不来。
        # ``mode`` = 声明的隔离档位（2026-10-03 新增，收口时登记）。
        # ``default``/``allow``/``deny``/``risk`` 四键**已废弃**（准入判定归 guard，
        # 问人归 approval）⇒ 保留仅为兼容既有配置，**不再有裁决效果**。
        # ⚠️ 删键会让写了这些键的用户配置在装配期抛 ``ConfigError`` ⇒ 服务起不来，故不删。
        # ★ P0（2026-10-13，分级沙箱骨架）新增三键——**必须登记**：
        #   ``backend`` = state 轴（auto/off/win-token/wsl2/container/cloud）
        #   ``network`` = 网络限制（dict，内含 ``allow_domains`` 等；登记成 dict 而非点号键，
        #                 因为严格节的键是扁平的）
        #   ``strict``  = 显式要求后端不可得时是否硬失败（默认 false = 告警+回落）
        "sandbox": {"mode": str, "targets": list, "default": str, "allow": list,
                    "deny": list, "risk": dict, "backend": str, "network": dict,
                    "strict": bool},
    },
    # 插件自管顶层节
    "windows_mcp": "_any",
    "playwright_mcp": "_any",
    # 只读技能 MCP 桥（plugins/skills_mcp）：与上面两个 MCP 桥同款 `_any` 宽松节。
    # ★ 用 `_any` 而非严格键集：`skills_mcp.enabled` / `data_dir` / `timeout_s` 等由插件自管，
    #   标严格节会让设置页保存（validate_config fail-fast）因漏登记而失败。
    "skills_mcp": "_any",
}


def _deep_merge(base: dict, overlay: dict | None) -> dict:
    """全量独立拷贝合并（overlay 优先）；不引用 base/overlay 任何可变子值。"""
    out = copy.deepcopy(dict(base))
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def coerce_config(cfg: dict | None, defaults: dict | None = None) -> dict:
    """无损归一：返回配置独立副本，缺失节以 ``defaults`` 补齐（``cfg`` 已给值优先）。

    仅做结构补齐 + 深拷贝，**不做字符串猜测式类型转换**（类型不符由 ``validate_config`` 抛错）。
    例：``coerce_config(user_cfg, defaults=BASE_AGENT_CONFIG)`` 即把用户配置补到完整默认骨架。
    """
    base = copy.deepcopy(dict(defaults)) if defaults else {}
    return _deep_merge(base, cfg or {})


def _matches(value: Any, spec: Any) -> bool:
    """value 是否满足类型规约 spec（类型或类型元组）。显式排除 bool：True/False 不应被当成 int/float。"""
    specs = spec if isinstance(spec, tuple) else (spec,)
    for t in specs:
        if t is bool:
            if isinstance(value, bool):
                return True
            continue
        if isinstance(value, bool):   # 任何非 bool 期望都不接受 bool
            continue
        if isinstance(value, t):
            return True
    return False


def _type_name(spec: Any) -> str:
    if isinstance(spec, tuple):
        return " / ".join(t.__name__ for t in spec)
    return spec.__name__


def validate_config(cfg: dict | None, schema: dict = _SCHEMA, path: str = "") -> None:
    """递归校验配置；未知键 / 类型不符 → 抛 ``ConfigError``（fail-fast）。

    不修改 cfg；仅检查。可选节缺失（节本身不存在）不报错，仅校验"存在时的键与类型"。
    """
    if not isinstance(cfg, dict):
        raise ConfigError(f"配置根应为 dict，得到 {type(cfg).__name__}")
    for key, value in cfg.items():
        if key not in schema:
            raise ConfigError(
                f"未知配置键 {path}{key!r}（不在 schema 中）；可能拼错或该节需标 _any"
            )
        spec = schema[key]
        if spec == "_any":
            continue
        if isinstance(spec, dict):
            if not isinstance(value, dict):
                raise ConfigError(f"配置节 {path}{key!r} 应为 dict，得到 {type(value).__name__}")
            validate_config(value, spec, path + key + ".")
        elif not _matches(value, spec):
            raise ConfigError(
                f"配置键 {path}{key!r} 类型不符：期望 {_type_name(spec)}，得到 {type(value).__name__}"
            )


__all__ = ["ConfigError", "coerce_config", "validate_config"]
