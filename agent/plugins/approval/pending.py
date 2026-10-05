"""plugins.approval.pending —— 待答请求表（call_id → Future + 超时 + 去重）。

交互式 provider（``cli``/``web``，第 3 期）的基座：把「问人」变成一次可 await
的 ``wait``，人的应答经 ``resolve(call_id, answer)`` 唤醒。fail-safe 三保证：

1. **超时默认拒绝**——``wait`` 超时返回 ``{"approved": False, "by": "timeout"}``，
   绝不挂起不决；
2. **同 key 去重**——同一 ``(tool, args)`` 在途时复用同一请求（并发多 ask 合并
   为一次人答；所有等待者拿到同一结果）；
3. **关闭即拒绝**——``aclose()``（teardown / 插件停用）唤醒全部在途等待者为
   拒绝，不留悬挂协程。

去重 key 由调用方拼（建议 ``tool + args 摘要``）；本表只认 key 字符串。

2.2（H2 会话隔离）：本表此前是**全局单表**，多会话下会出两类事故——
   (a) B 能在 ``snapshot()`` 里看见 A 的待审批项；
   (b) 更隐蔽：去重 key 跨会话共享 ⇒ B 的同名同参调用会**合并到 A 的在途请求上**，
       并拿到 A 的审批结果（静默串数据）。故入表一律带 ``session``，去重按会话分桶。
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

DENY_TIMEOUT = {"approved": False, "by": "timeout"}
DENY_SHUTDOWN = {"approved": False, "by": "shutdown"}


@dataclass(slots=True)
class _Entry:
    call_id: str
    key: str                    # 原始去重 key（回显用）
    req: dict
    future: "asyncio.Future[dict]"
    session: str = ""           # 2.2：归属会话键（``{user}:{session}``）
    dedupe: str = ""            # 2.2：实际分桶键 ``{session}|{key}``


class PendingTable:
    """待答请求表。``wait`` 挂起等答案；``resolve`` 由通道侧投喂答案。"""

    def __init__(self, timeout_s: float = 30.0):
        self._timeout_s = float(timeout_s)
        self._by_call: dict[str, _Entry] = {}
        self._by_key: dict[str, _Entry] = {}

    # ---------- 查询 ----------

    def size(self) -> int:
        """在途请求数（status() 展示用）。"""
        return len(self._by_call)

    # ---------- 等待 / 应答 ----------

    async def wait(self, key: str, req: dict, *,
                   timeout_s: float | None = None,
                   call_id: str | None = None,
                   session: str = "") -> tuple[dict, str]:
        """登记并等待应答；返回 ``(answer, call_id)``。超时/关闭 → fail-safe 拒绝。

        ``call_id``：调用方（插件层）已生成的对外标识（与 pending 弹窗信号一致），
        传入则透传复用——通道侧凭**同一个** call_id 应答；缺省自生成。

        ``session``（2.2）：归属会话键。去重按 ``{session}|{key}`` 分桶——跨会话
        **不合并**（否则 B 会复用 A 的在途请求并拿到 A 的审批结果）。
        """
        sess = str(session or "")
        dedupe = f"{sess}|{key}" if sess else key
        existing = self._by_key.get(dedupe)
        if existing is not None:                       # 去重：**同会话**内同 key 合并等待
            return await existing.future, existing.call_id

        cid = call_id or uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = _Entry(call_id=cid, key=key, req=req, future=fut,
                       session=sess, dedupe=dedupe)
        self._by_call[cid] = entry
        self._by_key[dedupe] = entry
        try:
            # shield：wait_for 超时不取消 fut——同 key 的其他等待者仍能拿到统一结果
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
        """投喂答案；请求不存在/已了结 → False（幂等，不抛）。

        ``session``（2.2）：给定则**二次校验归属**——不属于该会话的请求一律不投喂
        （纵深防御：路由层已判 403，此处再兜一道，避免将来新调用点漏判）。
        """
        entry = self._by_call.get(call_id)
        if entry is None or entry.future.done():
            return False
        if session is not None and entry.session != str(session):
            return False
        entry.future.set_result(dict(answer))
        return True

    def owner_of(self, call_id: str) -> str | None:
        """2.2：该请求的归属会话键（不存在 → ``None``）。路由层据此判 403。"""
        entry = self._by_call.get(call_id)
        return entry.session or None if entry else None

    def snapshot(self, session: str | None = None) -> list[dict]:
        """在途请求快照（``[{"call_id", "req"}]``；前端刷新后恢复弹窗用）。

        ``session``（2.2）：给定则**只返回本会话**的待审批项（否则跨会话可见）。
        """
        return [{"call_id": e.call_id, "req": dict(e.req)}
                for e in self._by_call.values()
                if session is None or e.session == session]

    # ---------- 生命周期 ----------

    async def aclose(self) -> None:
        """关闭：全部在途请求以拒绝了结（fail-safe；teardown 对称回收）。"""
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
