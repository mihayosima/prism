"""plugins.todo —— 待办存储。

按会话分桶：子 ctx 的 ``state``
不继承父级，且 ``ctx.state`` 是容器级 —— 直接写 ``ctx.state`` 会跨会话串号。``TodoStore``
只认会话键，绝不依赖 ``ctx.state``。

严格状态机：恰好一个 ``in_progress``；多则自动归一
，不在工具层拒绝调用以免打断规划流。
"""

from __future__ import annotations

import uuid

from dataclasses import dataclass

from agent.api import now_iso

_VALID = ("pending", "in_progress", "completed", "blocked")


@dataclass
class Todo:
    id: str
    content: str
    status: str = "pending"          # pending | in_progress | completed | blocked
    active_form: str = ""            # 进行时短语
    created_at: str = ""
    updated_at: str = ""


class TodoStore:
    """按会话分桶的待办存储：整表替换 + 严格状态机 + 完成度。

 会话键 → 该会话的待办列表；不持久化。
 """

    def __init__(self) -> None:
        self._buckets: dict[str, list[Todo]] = {}

    def write(self, session_key: str, todos: list[dict]) -> dict:
        """整表替换 + 严格状态机归一，返回完成度 ``summary``。

 ``todos`` 每项：``content``(必填) / ``status`` / ``activeForm`` / ``id``(可选)。
 """
        items: list[Todo] = []
        in_prog = 0
        for t in (todos or []):
            if not isinstance(t, dict):
                continue
            st = str(t.get("status", "pending"))
            if st not in _VALID:
                st = "pending"
            if st == "in_progress":
                in_prog += 1
            items.append(Todo(
                id=str(t.get("id") or uuid.uuid4().hex),
                content=str(t.get("content", "")),
                status=st,
                active_form=str(t.get("activeForm", "")),
                created_at=str(t.get("created_at") or now_iso()),
                updated_at=now_iso()))
        # 严格状态机：恰好一个 in_progress
        if in_prog > 1:
            seen = 0
            for t in reversed(items):
                if t.status == "in_progress":
                    seen += 1
                    if seen > 1:
                        t.status = "pending"
        self._buckets[session_key] = items
        return self.summary(session_key)

    def list(self, session_key: str) -> list[dict]:
        return [vars(t) for t in self._buckets.get(session_key, [])]

    def summary(self, session_key: str) -> dict:
        items = self._buckets.get(session_key, [])
        total = len(items)
        done = sum(1 for t in items if t.status == "completed")
        return {
            "total": total,
            "pending": sum(1 for t in items if t.status == "pending"),
            "in_progress": sum(1 for t in items if t.status == "in_progress"),
            "completed": done,
            "blocked": sum(1 for t in items if t.status == "blocked"),
            "percent": round(done / total * 100) if total else 0,
        }
