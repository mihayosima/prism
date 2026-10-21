"""配置结构补齐与递归校验：coerce_config 做无损归一，validate_config 对未知键/类型不符抛 ConfigError。"""

from __future__ import annotations

import copy
from typing import Any

from .errors import ConfigError


# 默认 SQLite 库路径。
DEFAULT_DB_PATH = "data/agent.db"


# 声明式 schema：节 -> {键: 期望类型}。``"_any"`` 表示该节/键由插件自管，宽松放行。
_SCHEMA: dict[str, Any] = {
    "db_path": str,
    "agent": {
        "llm": {"provider": str, "model": str, "stream": bool, "max_tokens": int},
        "budget": {"max_tokens_per_run": int, "reset_on_new_session": bool,
                   "finalize_reserve_tokens": int, "warning_ratio": (int, float),
                   "cost_calibration": bool},
        "permissions": {"default": str, "task_allow": list, "task_deny": list, "llm": str,
                        "risk": dict, "targets": list, "allow": list, "deny": list},
        "loop": {"deadline_s": (int, float)},
        "session": {"tenant": str, "user": str},
        "context": {
            "max_context_tokens": int, "reserve_output_tokens": int,
            "memory_ratio": (int, float), "history_ratio": (int, float),
            "source_clip_tokens": int,
            "clip_max_tokens": int, "clip_head_tokens": int, "clip_tail_tokens": int,
            "react_keep_recent": int, "react_min_chain_tokens": int,
            "working_window_tokens": int,
            "window_high_ratio": (int, float), "window_low_ratio": (int, float),
            "resident_cap_tokens": int, "on_demand_cap_tokens": int,
            "prompt_cap_tokens": int,
            "compact_mode": str, "clear_at_least_tokens": int,
        },
        "memory": "_any",
        "react": "_any",
        "approval": "_any",
        "system_memory": "_any",
        "work_memory": "_any",
        "short_term": "_any",
        "skills": "_any",
        "skills_mcp": "_any",
        "asr": "_any",
        "sandbox": {"mode": str, "targets": list, "default": str, "allow": list,
                    "deny": list, "risk": dict, "backend": str, "network": dict,
                    "strict": bool},
    },
    "windows_mcp": "_any",
    "playwright_mcp": "_any",
    "skills_mcp": "_any",
}


def _deep_merge(base: dict, overlay: dict | None) -> dict:
    """全量独立拷贝合并；不引用 base/overlay 任何可变子值。"""
    out = copy.deepcopy(dict(base))
    for key, value in (overlay or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def coerce_config(cfg: dict | None, defaults: dict | None = None) -> dict:
    """无损归一：返回配置独立副本，缺失节以 defaults 补齐。"""
    base = copy.deepcopy(dict(defaults)) if defaults else {}
    return _deep_merge(base, cfg or {})


def _matches(value: Any, spec: Any) -> bool:
    """value 是否满足类型规约 spec。显式排除 bool：True/False 不应被当成 int/float。"""
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
    """递归校验配置；未知键 / 类型不符 → 抛 ``ConfigError``。不修改 cfg。"""
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
