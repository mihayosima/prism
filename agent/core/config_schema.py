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


# 声明式 schema：节 -> {键: 期望类型}。``"_any"`` 表示该节/键由插件自管，宽松放行。
_SCHEMA: dict[str, Any] = {
    "db_path": str,
    "agent": {
        "llm": {"provider": str, "model": str},
        "budget": {"max_tokens_per_run": int, "reset_on_new_session": bool},
        "permissions": {"default": str, "task_allow": list, "task_deny": list, "llm": str},
        "loop": {"deadline_s": (int, float)},
        # 插件自管节
        "memory": "_any",
        "react": "_any",
    },
    # 插件自管顶层节
    "windows_mcp": "_any",
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
