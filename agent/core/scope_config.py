"""agent.core.scope_config —— 会话作用域配置的**纯规则**（零 IO、零业务词）。

只回答两件事（IO / 文件读写属适配层，见 ``agent.run.config_store``）：

 1. **合成**：容器覆盖（G1）与会话覆盖（G2）如何并成一份生效覆盖——
    **继承而非替换**：以容器为底，会话逐键覆盖；会话未提的键一律继承。
    「scope 只收窄不猜测」——会话表里没有的键，绝不从别处推断。
 2. **淘汰**：会话桶上限固定（缺省 200），超限时按 LRU 淘汰最久未用者。
    ★ 淘汰**只删覆盖**，不触碰任何会话数据（转写 / 记忆 / 快照都不受影响）。

纯度（继承 core 纪律）：零依赖、零 IO；不得 import sys/plugins/run 树。
"""

from __future__ import annotations

from typing import Any

DEFAULT_SESSION_LIMIT = 200


def merge_overrides(container: dict | None, session: dict | None) -> dict[str, dict]:
    """容器覆盖 + 会话覆盖 → 生效覆盖（形状恒为 ``{插件节: {键: 值}}``）。

    浅合并到**插件节层**：值是标量（int/float/bool/str/list），不存在「嵌套再合并」需求。
    会话覆盖中的非 dict 项一律跳过（脏数据不污染整份配置）。
    """
    out: dict[str, dict] = {}
    for pid, node in (container or {}).items():
        if isinstance(pid, str) and isinstance(node, dict):
            out[pid] = dict(node)
    for pid, patch in (session or {}).items():
        if not isinstance(pid, str) or not isinstance(patch, dict):
            continue
        out.setdefault(pid, {}).update(patch)
    return out


def section_of(merged: dict | None, section: str) -> dict:
    """取生效覆盖里某个插件节（``context`` / ``budget`` / ``short_term`` …）的键值。

    映射即 ``plugins.<section>``——与 ``run/chat.py::_PLUGIN_OF_KEY`` 的既有口径一致。
    """
    return dict((merged or {}).get(f"plugins.{section}") or {})


def touch(sessions: dict, key: str) -> None:
    """标记会话桶最近使用（dict 保序：删了再插即移到末尾）。"""
    if key in sessions:
        sessions[key] = sessions.pop(key)


def evict_lru(sessions: dict, keep: str | None, limit: int = DEFAULT_SESSION_LIMIT
              ) -> list[str]:
    """淘汰最久未用的会话桶；返回被淘汰的键（供调用方同步清理其状态分桶）。

    ``keep``（刚用过的会话）永不淘汰；``limit <= 0`` 视为不限。
    """
    if limit <= 0 or len(sessions) <= limit:
        return []
    over = len(sessions) - limit
    keys = [k for k in list(sessions.keys()) if k != keep]
    drop = keys[:max(0, over)]
    for k in drop:
        sessions.pop(k, None)
    return drop


def deep_defaults(defaults: dict, override: dict | None) -> dict[str, Any]:
    """默认值 ∪ 覆盖（覆盖优先，缺省键回落默认）。与 scope 无关的通用小工具。"""
    out = dict(defaults or {})
    for k, v in (override or {}).items():
        out[k] = v
    return out


__all__ = ["DEFAULT_SESSION_LIMIT", "merge_overrides", "section_of",
           "touch", "evict_lru", "deep_defaults"]
