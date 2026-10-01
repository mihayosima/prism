"""agent.run.config_store —— 插件配置覆盖的持久化（v1 平铺 → v2 分 scope）。

为什么独立成模块：覆盖文件是**适配层资产**（``data/agent_plugin_config.json``），
而合并/淘汰规则是**内核纯函数**（``core.scope_config``）——IO 与规则分离，便于单测。

v1（现状）：``{ "plugins.context": {...}, "plugins.budget": {...} }``
v2（本次）：``{version: 2, container: {<v1 形状>}, sessions: {<sk>: {<v1 形状>}}}``

迁移七条纪律（与方案 §S0 一致，代码逐条对应）：
 1. 读 v1 **兼容且不写回**（只有真正发生写操作时才升级）；
 2. 首次写 v2 前备份 ``*.v1.bak``（已备份则不覆盖）；
 3. 版本守卫：未知 version（既非 1 也非 2）→ 按 v1 **只读**解析，避免把脏数据当 v2 落盘；
 4. 禁止双写平铺副本（v2 下不再出现顶层 ``plugins.*``）；
 5. scope 只收窄不猜测（会话缺键 = 继承容器，见 ``core.scope_config.merge_overrides``）；
 6. sessions 上限 + LRU（缺省 200）；淘汰只删覆盖，不动会话数据；
 7. 损坏文件按空处理（不裸崩，与既有 ``_load_overrides`` 同纪律）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from agent.api import (DEFAULT_SESSION_LIMIT, evict_lru, merge_overrides,
                       section_of, touch)

V1 = 1
V2 = 2
DEFAULT_PATH = Path("data/agent_plugin_config.json")


class ConfigStore:
    """覆盖文件的读写门面（内存态 + 落盘；进程内唯一真源）。"""

    def __init__(self, path: Path | None = None,
                 session_limit: int = DEFAULT_SESSION_LIMIT) -> None:
        self.path = Path(path) if path is not None else DEFAULT_PATH
        self.session_limit = int(session_limit or DEFAULT_SESSION_LIMIT)
        self._data = self._load()

    # ---- 加载 / 兼容 ----

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": V2, "container": {}, "sessions": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8")) or {}
        except Exception:                       # noqa: BLE001 —— 损坏按空处理
            return {"version": V2, "container": {}, "sessions": {}}
        if not isinstance(raw, dict):
            return {"version": V2, "container": {}, "sessions": {}}
        ver = raw.get("version")
        if ver == V2:
            return {"version": V2,
                    "container": dict(raw.get("container") or {}),
                    "sessions": dict(raw.get("sessions") or {})}
        # v1 平铺（或未知结构）：只认 plugins.* 顶层节，按容器覆盖读，**不写回**
        container = {k: dict(v) for k, v in raw.items()
                     if isinstance(k, str) and k.startswith("plugins.")
                     and isinstance(v, dict)}
        return {"version": V1, "container": container, "sessions": {}}

    @property
    def is_v1(self) -> bool:
        return self._data.get("version") != V2

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.is_v1 and self.path.exists():
            bak = self.path.with_suffix(".v1.bak")
            if not bak.exists():
                try:
                    shutil.copyfile(self.path, bak)
                except Exception:               # noqa: BLE001 —— 备份失败不阻断写入
                    pass
        self._data["version"] = V2
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    # ---- 读 ----

    def container(self) -> dict:
        """容器级覆盖（v1 形状 ``{plugins.X: {...}}``）。"""
        return dict(self._data.get("container") or {})

    def session(self, sk: str) -> dict:
        """某个会话的覆盖（v1 形状；无则空 dict）。"""
        return dict((self._data.get("sessions") or {}).get(sk) or {})

    def sessions(self) -> dict:
        return dict(self._data.get("sessions") or {})

    def view(self, section: str, sk: str | None = None) -> dict:
        """**会话配置视图**：容器 ∪ 本会话 → 该插件节的生效键值（返回副本）。

        这就是挂到 ctx 上的 ``session.cfg_view`` 接缝所调用的形态。
        ``sk`` 为空 ⇒ 等价于纯容器视图（无会话覆盖）。
        """
        sess = self.session(sk) if sk else {}
        merged = merge_overrides(self.container(), sess)
        return section_of(merged, section)

    def effective(self, sk: str | None = None) -> dict:
        """容器 ∪ 会话 的全量生效覆盖（v1 形状，给设置页回显用）。"""
        return merge_overrides(self.container(), self.session(sk) if sk else {})

    # ---- 写 ----

    def _touch_and_evict(self, sk: str | None) -> list[str]:
        sessions = self._data.setdefault("sessions", {})
        if sk:
            touch(sessions, sk)
        return evict_lru(sessions, keep=sk, limit=self.session_limit)

    def patch_container(self, buckets: dict[str, dict]) -> None:
        """按插件节写入容器级覆盖（``buckets`` = ``{plugins.X: {k: v}}``）。"""
        node = self._data.setdefault("container", {})
        for pid, patch in (buckets or {}).items():
            if not isinstance(patch, dict):
                continue
            cur = dict(node.get(pid) or {})
            cur.update(patch)
            node[pid] = cur
        self._save()

    def replace_container(self, data: dict) -> None:
        """整份替换容器覆盖（既有 ``_save_overrides`` 语义：整体覆盖，非合并）。"""
        self._data["container"] = {k: dict(v) for k, v in (data or {}).items()
                                   if isinstance(k, str) and isinstance(v, dict)}
        self._save()

    def patch_session(self, sk: str, buckets: dict[str, dict]) -> None:
        """按插件节写入会话级覆盖（同时刷新 LRU 并淘汰）。"""
        if not sk:
            return
        node = self._data.setdefault("sessions", {}).setdefault(sk, {})
        for pid, patch in (buckets or {}).items():
            if not isinstance(patch, dict):
                continue
            cur = dict(node.get(pid) or {})
            cur.update(patch)
            node[pid] = cur
        self._touch_and_evict(sk)
        self._save()

    def clear_session(self, sk: str) -> bool:
        """清空某会话的全部覆盖（「重置为继承容器」）；返回是否确有删除。"""
        sessions = self._data.get("sessions") or {}
        existed = sk in sessions
        if existed:
            sessions.pop(sk, None)
            self._save()
        return existed

    def reset_container(self, pids: list[str] | tuple[str, ...]) -> None:
        """恢复默认：删除这些插件节的**容器级**覆盖（既有 reset 语义，不变）。"""
        node = self._data.setdefault("container", {})
        for pid in pids or ():
            node.pop(pid, None)
        self._save()


__all__ = ["ConfigStore", "V1", "V2", "DEFAULT_PATH"]
