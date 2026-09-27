"""plugins.approval.providers —— 通道接缝（怎么把人叫过来）。

``ApprovalProvider`` 是可换接缝（设计稿 §3）：只在判定为 ``ask`` 时被调用，
输入请求描述、返回 ``{"approved": bool, "by": str}``。**provider 无权放行
guard 已否决的任务**（它排在 guard 之后，根本看不到那些调用）。

本期内置两个**非交互** provider（第 1 期即可完整验收机制）：
- ``auto-deny`` —— fail-safe 默认：ask 一律拒绝（不挂起、不超时）；
- ``auto-allow`` —— 本地开发/灰度：ask 一律放行，但**审计照记**
  （asked/answered 两事件都落 agent_events，可事后追责）。

``cli`` / ``web``（交互式）：``web`` 经待答表 + SSE 弹窗 + 入站应答接口（第 2 期）；
``cli`` 在终端直接问答（第 3 期，无头/本地演练用）。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol


class ApprovalProvider(Protocol):
    """通道接缝：``ask`` 返回 ``{"approved": bool, "by": str}``（纯数据）。

    ``interactive``：是否需要向人发「待审批」信号（插件层据此决定是否 emit
    ``agent/approval/pending``——非交互 provider 问不等人，发信号只会产生
    一闪而过的假弹窗）。
    """

    name: str
    interactive: bool

    async def ask(self, ctx: Any, req: dict) -> dict: ...


class AutoDenyProvider:
    """fail-safe：ask → 拒绝（OPEN-2 推荐默认）。"""

    name = "auto-deny"
    interactive = False

    async def ask(self, ctx: Any, req: dict) -> dict:
        return {"approved": False, "by": self.name}


class AutoAllowProvider:
    """开发/灰度：ask → 放行（审计照记，由插件层落 asked/answered）。"""

    name = "auto-allow"
    interactive = False

    async def ask(self, ctx: Any, req: dict) -> dict:
        return {"approved": True, "by": self.name}


class WebProvider:
    """第 2 期交互式：挂起在待答表等人的应答（超时/关闭 → 表内 fail-safe 拒绝）。

    人的应答经 ``/api/approval/answer`` → ``PendingTable.resolve`` 投喂。
    ``req["call_id"]`` 透传进待答表，保证「弹窗信号」与「应答」用的是同一标识。
    去重 key = ``tool + kwargs 摘要``（并发同参调用合并为一次人答）。
    """

    name = "web"
    interactive = True

    def __init__(self, table: Any):
        self._table = table

    async def ask(self, ctx: Any, req: dict) -> dict:
        kwargs = req.get("kwargs") or {}
        key = str(req.get("tool", "")) + "|" + json.dumps(
            kwargs, sort_keys=True, ensure_ascii=False, default=str)
        ans, _ = await self._table.wait(key, req, call_id=req.get("call_id"))
        return ans


class CliProvider:
    """第 3 期：终端问答（无头/本地演练）。``y``=批准、``a``=批准且本会话总是允许、
    其余/EOF/Ctrl-C=拒绝。``interactive=False``——它自己就是通道，不发弹窗信号。
    阻塞读入放线程池（``asyncio.to_thread``），不卡事件循环。"""

    name = "cli"
    interactive = False

    async def ask(self, ctx: Any, req: dict) -> dict:
        kwargs = req.get("kwargs") or {}
        print(f"\n[审批] 工具 {req.get('tool')}（risk={req.get('risk')}, "
              f"mode={req.get('mode')}）请求执行")
        if kwargs:
            print("  参数:", json.dumps(kwargs, ensure_ascii=False, default=str))
        try:
            raw = str(await asyncio.to_thread(
                input, "  批准？[y=允许 / a=本会话总是允许 / 其余=拒绝] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):        # noqa: BLE001 —— 无输入即拒绝
            return {"approved": False, "by": "cli:eof"}
        if raw in ("y", "yes"):
            return {"approved": True, "by": "cli"}
        if raw == "a":
            return {"approved": True, "by": "cli", "always": True}
        return {"approved": False, "by": "cli"}


REGISTRY: dict[str, type] = {
    AutoDenyProvider.name: AutoDenyProvider,
    AutoAllowProvider.name: AutoAllowProvider,
    WebProvider.name: WebProvider,
    CliProvider.name: CliProvider,
}


def build_provider(name: str, table: Any = None) -> ApprovalProvider:
    """按配置名构造 provider；未知名字 / ``web`` 无待答表 → fail-safe 落 auto-deny。"""
    if str(name or "") == WebProvider.name:
        if table is None:
            return AutoDenyProvider()
        return WebProvider(table)
    cls = REGISTRY.get(str(name or ""))
    if cls is None:
        cls = AutoDenyProvider
    return cls()


__all__ = ["ApprovalProvider", "AutoDenyProvider", "AutoAllowProvider",
           "WebProvider", "CliProvider", "REGISTRY", "build_provider"]
