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
        snapshot_ttl = int(cfg.get("snapshot_ttl_s", 86400))
        # 阶段 2（D10）：required=True → Redis 是**硬依赖**，不可用即**启动失败**（装配期 raise，
        # 容器 fail-fast，见 core/container.py:280-287 apply 异常向上传播）；
        # False（默认，保 dev 兼容）→ 不可用则降级 InMemory + 启动 WARNING。
        required = bool(cfg.get("required", False))
        if not cfg.get("enabled", True):
            if required:
                raise RuntimeError(
                    "work_memory: required=True 但 enabled=False —— 配置矛盾：required 须启用 Redis 后端")
            logger.info("work_memory: disabled by config, in-memory fallback")
            self._impl = InMemoryWorkMemory(ttl=ttl, prefix=prefix,
                                            snapshot_ttl=snapshot_ttl)
            ctx.provide(Services.WORK_MEMORY, self)
            return
        try:
            import redis.asyncio as aioredis                 # 延迟导入（第三方）
            url = cfg.get("url", "redis://localhost:6379/0")
            client = aioredis.from_url(url, decode_responses=True, socket_connect_timeout=2)
            await client.ping()
            self._impl = RedisWorkMemory(client, ttl=ttl, prefix=prefix,
                                         snapshot_ttl=snapshot_ttl)
            logger.info("work_memory: redis backend active (%s)", url)
        except Exception as exc:                             # 任何失败 → 视 required 而定
            if required:
                logger.error("work_memory: required=True 但 redis 不可用（%s）—— 启动失败", exc)
                raise RuntimeError(
                    f"work_memory: required=True but redis unavailable: {exc}") from exc
            logger.warning("work_memory: redis unavailable (%s), degrade to in-memory"
                           " —— 自动续接在降级下不保证，生产请置 required=True", exc)
            self._impl = InMemoryWorkMemory(ttl=ttl, prefix=prefix,
                                            snapshot_ttl=snapshot_ttl)
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

    # ---- 跨进程 run 快照（阶段 2）----
    # 纪律：save/load/list 走"吞异常仅告警"（同工作记忆主链路——快照失败绝不拖累 run）；
    #      mark_finished **例外**（D16）——"收尾置位失败会被误判为崩溃"，故单独走重试+确认键路径。

    async def snapshot_save(self, tenant: str, task_id: str, data: dict,
                            ttl: int | None = None) -> None:
        try:
            if self._impl:
                await self._impl.snapshot_save(tenant, task_id, data, ttl)
        except Exception as exc:                             # noqa: BLE001
            logger.warning("work_memory.snapshot_save failed: %s", exc)

    async def snapshot_load(self, tenant: str, task_id: str) -> dict | None:
        try:
            return await self._impl.snapshot_load(tenant, task_id) if self._impl else None
        except Exception as exc:                             # noqa: BLE001
            logger.warning("work_memory.snapshot_load failed: %s", exc)
            return None

    async def snapshot_list(self, tenant: str, prefix: str | None = None) -> list[dict]:
        """列出未完成/已完成快照。**务必传 ``prefix``**（会话级），否则退化为全租户扫描（H1）。"""
        try:
            return await self._impl.snapshot_list(tenant, prefix) if self._impl else []
        except Exception as exc:                             # noqa: BLE001
            logger.warning("work_memory.snapshot_list failed: %s", exc)
            return []

    async def snapshot_mark_finished(self, tenant: str, task_id: str) -> None:
        """D16：**不走**吞异常路径。

        收尾置位失败会导致下次启动误判为"崩溃残留"→ 错误自动续接，故：置位 → 失败重试一次
        → 仍失败则 ERROR + 写独立确认键 ``run_snapshot_finished:*``（短 TTL）兜底防误判。
        """
        if not self._impl:
            return
        try:
            await self._impl.snapshot_mark_finished(tenant, task_id)
            return
        except Exception as exc:                             # noqa: BLE001
            logger.warning("work_memory.snapshot_mark_finished failed, retrying: %s", exc)
        try:
            await self._impl.snapshot_mark_finished(tenant, task_id)
        except Exception as exc:                             # noqa: BLE001
            logger.error("work_memory.snapshot_mark_finished failed twice: %s", exc)
            try:
                await self._impl.snapshot_flag_finished(tenant, task_id)
            except Exception as exc2:                        # noqa: BLE001 —— 确认键亦失败：仅记录
                logger.error("work_memory.snapshot_flag_finished failed: %s", exc2)


__all__ = ["WorkMemory", "InMemoryWorkMemory", "RedisWorkMemory", "WorkMemoryLike"]
