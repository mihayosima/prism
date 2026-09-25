"""插件包：work_memory —— 工作记忆（第 3 层：任务级进度缓存）。

目录插件（agent/plugins/work_memory），与短期记忆 ``plugins.short_term``、
系统记忆 ``plugins.system_memory``、长期记忆 ``plugins.memory`` 平级；
经「插件」页可启用 / 停用 / 卸载。

设计（用户敲定）：
- 存储后端：**Redis**（外部服务）；Redis 不可用 / 未配置 / 导入失败 → **降级为进程内 dict**
  （``InMemoryWorkMemory``，接口一致），功能不崩、不破解耦。
- 寿命：任务级——``run()`` 开始 ``begin``、结束 ``finish``（DEL）；兜底 TTL 防泄漏。
- 边界：只存任务进度态（step / tool_trace / checkpoints / status），**不存对话全文**
  （对话历史属短期记忆 ``agent_messages``，跨会话知识属长期记忆 ``plugins.memory``）。

解耦纪律：
- 消费面（react）经 ``ctx.get(Services.WORK_MEMORY)`` 接缝获取，不 import 具体实现；
- 委托调用内部吞异常（仅告警），工作记忆异常绝不拖累主链路；
- 第三方 Redis 客户端在此**延迟导入**（try 内），插件未启用时零开销。
"""

from __future__ import annotations

import logging
from typing import Any

from agent.api import Services, plugin

from .backend import InMemoryWorkMemory, RedisWorkMemory, WorkMemoryLike

logger = logging.getLogger("agent.plugins.work_memory")


@plugin
class WorkMemory:
    """ctx.work_memory —— 任务级进度缓存；Redis 不可用自动降级内存版。

    委托给 ``self._impl``；委托调用内部吞掉异常（仅告警），保证工作记忆异常绝不拖累主链路
    （react 即便不包 try/except 也安全）。
    """

    name = "plugins.work_memory"
    version = "0.1.0"
    layer = "system"
    provides = [Services.WORK_MEMORY]
    inject = []
    description = "工作记忆（第3层）：任务级进度缓存（Redis 后端，不可用降级进程内）"

    def __init__(self):
        self._impl: WorkMemoryLike | None = None

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("work_memory") or {}
        ttl = int(cfg.get("ttl", 3600))
        prefix = cfg.get("prefix", "wm:")
        if not cfg.get("enabled", True):
            logger.info("work_memory: disabled by config, in-memory fallback")
            self._impl = InMemoryWorkMemory(ttl=ttl, prefix=prefix)
            ctx.provide(Services.WORK_MEMORY, self)
            return
        try:
            import redis.asyncio as aioredis                 # 延迟导入（第三方）
            url = cfg.get("url", "redis://localhost:6379/0")
            client = aioredis.from_url(url, decode_responses=True, socket_connect_timeout=2)
            await client.ping()
            self._impl = RedisWorkMemory(client, ttl=ttl, prefix=prefix)
            logger.info("work_memory: redis backend active (%s)", url)
        except Exception as exc:                             # 任何失败 → 降级内存版
            logger.warning("work_memory: redis unavailable (%s), degrade to in-memory", exc)
            self._impl = InMemoryWorkMemory(ttl=ttl, prefix=prefix)
        ctx.provide(Services.WORK_MEMORY, self)

    # ---- 委托（吞异常，主链路不崩） ----
    async def begin(self, task_id: str, meta: dict) -> None:
        try:
            if self._impl:
                await self._impl.begin(task_id, meta)
        except Exception as exc:
            logger.warning("work_memory.begin failed: %s", exc)

    async def update(self, task_id: str, **fields) -> None:
        try:
            if self._impl:
                await self._impl.update(task_id, **fields)
        except Exception as exc:
            logger.warning("work_memory.update failed: %s", exc)

    async def checkpoint(self, task_id: str, name: str, data: Any) -> None:
        try:
            if self._impl:
                await self._impl.checkpoint(task_id, name, data)
        except Exception as exc:
            logger.warning("work_memory.checkpoint failed: %s", exc)

    async def get(self, task_id: str) -> dict | None:
        try:
            return await self._impl.get(task_id) if self._impl else None
        except Exception as exc:
            logger.warning("work_memory.get failed: %s", exc)
            return None

    async def finish(self, task_id: str) -> None:
        try:
            if self._impl:
                await self._impl.finish(task_id)
        except Exception as exc:
            logger.warning("work_memory.finish failed: %s", exc)

    async def list_active(self, session_id: str | None = None) -> list[str]:
        try:
            return await self._impl.list_active(session_id) if self._impl else []
        except Exception as exc:
            logger.warning("work_memory.list_active failed: %s", exc)
            return []


__all__ = ["WorkMemory", "InMemoryWorkMemory", "RedisWorkMemory", "WorkMemoryLike"]
