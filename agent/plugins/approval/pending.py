"""待答请求表：``call_id`` → Future + 超时 + 去重。``wait`` 挂起等答案，``resolve`` 投喂答案；超时/关闭一律失败安全拒绝，按会话分桶去重。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

DENY_TIMEOUT = {"approved": False, "by": "timeout"}
DENY_SHUTDOWN = {"approved": False, "by": "shutdown"}


@dataclass(slots=True)
class _Entry:
    call_id: str
    key: str                    # 原始去重 key
    req: dict
    future: "asyncio.Future[dict]"
    session: str = ""           # 归属会话键
    dedupe: str = ""            # 分桶键 ``{session}|{key}``


class PendingTable:
    """待答请求表。``wait`` 挂起等答案；``resolve`` 由通道侧投喂答案。"""

    def __init__(self, timeout_s: float = 30.0):
        self._timeout_s = float(timeout_s)
        self._by_call: dict[str, _Entry] = {}
        self._by_key: dict[str, _Entry] = {}

    # ---------- 查询 ----------

    def size(self) -> int:
        """在途请求数。"""
        return len(self._by_call)

    # ---------- 等待 / 应答 ----------

    async def wait(self, key: str, req: dict, *,
                   timeout_s: float | None = None,
                   call_id: str | None = None,
                   session: str = "") -> tuple[dict, str]:
        """登记并等待应答，返回 ``(answer, call_id)``；超时/关闭 → 失败安全拒绝。同会话同 key 合并等待，复用同一 future。"""
        sess = str(session or "")
        dedupe = f"{sess}|{key}" if sess else key
        existing = self._by_key.get(dedupe)
        if existing is not None:                       # 同会话内同 key 合并等待
            return await existing.future, existing.call_id

        cid = call_id or uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = _Entry(call_id=cid, key=key, req=req, future=fut,
                       session=sess, dedupe=dedupe)
        self._by_call[cid] = entry
        self._by_key[dedupe] = entry
        try:
            # shield：超时不取消 fut，其他同 key 等待者仍能拿到统一结果
            answer = await asyncio.wait_for(asyncio.shield(fut),
                                            timeout=self._timeout_s if timeout_s is None
                                            else float(timeout_s))
            return dict(answer), cid
        except asyncio.TimeoutError:
            if not fut.done():
                fut.set_result(dict(DENY_TIMEOUT))
            return dict(DENY_TIMEOUT), cid
        finally:
            self._pop(entry)

    def resolve(self, call_id: str, answer: dict, session: str | None = None) -> bool:
        """投喂答案；请求不存在/已了结 → False。``session`` 给定时校验归属，非本会话不投喂。"""
        entry = self._by_call.get(call_id)
        if entry is None or entry.future.done():
            return False
        if session is not None and entry.session != str(session):
            return False
        entry.future.set_result(dict(answer))
        return True

    def owner_of(self, call_id: str) -> str | None:
        """该请求的归属会话键。"""
        entry = self._by_call.get(call_id)
        return entry.session or None if entry else None

    def snapshot(self, session: str | None = None) -> list[dict]:
        """在途请求快照 ``[{"call_id", "req"}]``；给定 session 只返回本会话项。"""
        return [{"call_id": e.call_id, "req": dict(e.req)}
                for e in self._by_call.values()
                if session is None or e.session == session]

    # ---------- 生命周期 ----------

    async def aclose(self) -> None:
        """关闭：全部在途请求以拒绝了结。"""
        for entry in list(self._by_call.values()):
            if not entry.future.done():
                entry.future.set_result(dict(DENY_SHUTDOWN))
        self._by_call.clear()
        self._by_key.clear()

    # ---------- 内部 ----------

    def _pop(self, entry: _Entry) -> None:
        if self._by_call.get(entry.call_id) is entry:
            del self._by_call[entry.call_id]
        dedupe = entry.dedupe or entry.key
        if self._by_key.get(dedupe) is entry:
            del self._by_key[dedupe]


__all__ = ["PendingTable"]
