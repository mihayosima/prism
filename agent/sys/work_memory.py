"""agent.sys.work_memory —— 工作记忆（第 3 层：任务级进度缓存）。

设计（用户敲定）：
- 存储后端：**Redis**（外部服务）；Redis 不可用 / 未配置 / 导入失败 → **降级为进程内 dict**
  （``InMemoryWorkMemory``，接口一致），功能不崩、不破解耦。
- 寿命：任务级——``run()`` 开始 ``begin``、结束 ``finish``（DEL）；兜底 TTL 防泄漏。
- 形态：系统层插件 ``agent.sys.work_memory``，``provides=[Services.WORK_MEMORY]``，
  ``inject=[]``（纯缓存服务，零业务依赖）。
- 边界：只存任务进度态（step / tool_trace / checkpoints / status），**不存对话全文**
  （对话历史属短期记忆 ``agent_messages``，跨会话知识属长期记忆 ``plugins.memory``）。

零特权：本模块仅依赖 ``agent.api``；Redis 客户端经 ``redis.asyncio`` 延迟导入（第三方，
在 try 内），不反向依赖 sys/plugins（K0 守卫）。消费方（react）经 ``ctx.get(Services.WORK_MEMORY)``
接缝获取，不 import 具体实现。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Protocol

from agent.api import Services, plugin

logger = logging.getLogger("agent.sys.work_memory")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _initial_data(meta: dict, ttl: int) -> dict:
    """hash 初始字段（两种后端共用）。"""
    return {
        "session_id": meta.get("session_id") or "",
        "run_id": meta.get("run_id") or "",
        "status": "running",
        "step": 0,
        "max_steps": meta.get("max_steps") or 0,
        "tools": "[]",
        "last_action": "",
        "checkpoints": "[]",
        "started_at": _now_iso(),
        "updated_at": _now_iso(),
    }


class WorkMemoryLike(Protocol):
    """工作记忆接缝契约（留系统层；消费方依此调用，可注入替换）。"""

    async def begin(self, task_id: str, meta: dict) -> None: ...
    async def update(self, task_id: str, **fields) -> None: ...
    async def checkpoint(self, task_id: str, name: str, data: Any) -> None: ...
    async def get(self, task_id: str) -> dict | None: ...
    async def finish(self, task_id: str) -> None: ...
    async def list_active(self, session_id: str | None = None) -> list[str]: ...


class InMemoryWorkMemory:
    """降级实现：进程内 dict，接口与 Redis 版一致。

    仅在单进程、无 Redis 时生效；跨进程 / 重启后失效（与 Redis 语义一致，只是不跨进程）。
    """

    def __init__(self, ttl: int = 3600, prefix: str = "wm:"):
        self._ttl = ttl
        self._prefix = prefix
        self._store: dict[str, tuple[float, dict]] = {}   # key -> (expire_at, data)

    def _k(self, task_id: str) -> str:
        return f"{self._prefix}{task_id}"

    def _alive(self, key: str) -> dict | None:
        if key not in self._store:
            return None
        exp, data = self._store[key]
        if exp < time.time():
            del self._store[key]
            return None
        return data

    async def begin(self, task_id: str, meta: dict) -> None:
        data = _initial_data(meta, self._ttl)
        self._store[self._k(task_id)] = (time.time() + self._ttl, data)

    async def update(self, task_id: str, **fields) -> None:
        key = self._k(task_id)
        data = self._alive(key)
        if data is None:
            return
        for k, v in fields.items():
            data[k] = v if isinstance(v, (str, int, float, bool)) else json.dumps(v, ensure_ascii=False)
        data["updated_at"] = _now_iso()
        self._store[key] = (time.time() + self._ttl, data)

    async def checkpoint(self, task_id: str, name: str, data: Any) -> None:
        key = self._k(task_id)
        cur = self._alive(key)
        if cur is None:
            return
        cps = json.loads(cur.get("checkpoints") or "[]")
        entry = {
            "name": name,
            "step": cur.get("step", 0),
            "ts": _now_iso(),
            "data": data if isinstance(data, (str, int, float, bool, list, dict)) else str(data),
        }
        cps.append(entry)
        if len(cps) > 10:                       # 列表上限 10，超丢弃最旧（防 hash 膨胀）
            cps = cps[-10:]
        cur["checkpoints"] = json.dumps(cps, ensure_ascii=False)
        cur["updated_at"] = _now_iso()
        self._store[key] = (time.time() + self._ttl, cur)

    async def get(self, task_id: str) -> dict | None:
        data = self._alive(self._k(task_id))
        return dict(data) if data else None

    async def finish(self, task_id: str) -> None:
        self._store.pop(self._k(task_id), None)

    async def list_active(self, session_id: str | None = None) -> list[str]:
        now = time.time()
        out: list[str] = []
        for key, (exp, data) in list(self._store.items()):
            if exp < now:
                continue
            if session_id is None or data.get("session_id") == session_id:
                out.append(key[len(self._prefix):])
        return out


class RedisWorkMemory:
    """Redis 后端实现（duck-typed ``redis.asyncio`` 客户端）。

    字段：``wm:{task_id}`` hash；session 索引 ``wm:idx:{session_id}`` set（供 list_active）。
    所有可变字段以字符串存储；list/dict 经 json.dumps。
    """

    def __init__(self, client, ttl: int = 3600, prefix: str = "wm:"):
        self._r = client
        self._ttl = ttl
        self._prefix = prefix

    def _k(self, task_id: str) -> str:
        return f"{self._prefix}{task_id}"

    def _idx(self, session_id: str) -> str:
        return f"{self._prefix}idx:{session_id}"

    @staticmethod
    def _enc(v: Any) -> str:
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    async def begin(self, task_id: str, meta: dict) -> None:
        key = self._k(task_id)
        mapping = {k: self._enc(v) for k, v in _initial_data(meta, self._ttl).items()}
        await self._r.hset(key, mapping=mapping)
        await self._r.expire(key, self._ttl)
        sid = meta.get("session_id")
        if sid:
            await self._r.sadd(self._idx(sid), task_id)

    async def update(self, task_id: str, **fields) -> None:
        if not fields:
            return
        mapping = {k: self._enc(v) for k, v in fields.items()}
        mapping["updated_at"] = _now_iso()
        await self._r.hset(self._k(task_id), mapping=mapping)
        await self._r.expire(self._k(task_id), self._ttl)

    async def checkpoint(self, task_id: str, name: str, data: Any) -> None:
        key = self._k(task_id)
        raw = await self._r.hget(key, "checkpoints")
        cps = json.loads(raw) if raw else []
        entry = {
            "name": name,
            "step": int(await self._r.hget(key, "step") or 0),
            "ts": _now_iso(),
            "data": data if isinstance(data, (str, int, float, bool, list, dict)) else str(data),
        }
        cps.append(entry)
        if len(cps) > 10:
            cps = cps[-10:]
        await self._r.hset(key, mapping={
            "checkpoints": json.dumps(cps, ensure_ascii=False), "updated_at": _now_iso()})
        await self._r.expire(key, self._ttl)

    async def get(self, task_id: str) -> dict | None:
        raw = await self._r.hgetall(self._k(task_id))
        return dict(raw) if raw else None

    async def finish(self, task_id: str) -> None:
        key = self._k(task_id)
        await self._r.delete(key)
        # 索引清理：遍历所有 idx 集合代价高，仅当能从 data 取 session_id 时精确 SREM
        # （finish 时 hash 已删，session_id 不可得 → 由 list_active 的 exists 校验兜漏）

    async def list_active(self, session_id: str | None = None) -> list[str]:
        if session_id:
            ids = await self._r.smembers(self._idx(session_id))
            return [i for i in ids if await self._r.exists(self._prefix + i)]
        out: list[str] = []
        async for k in self._r.scan_iter(match=self._prefix + "*"):
            if k.endswith(b"idx") or ":idx:" in k:    # 跳过索引键
                continue
            out.append(k[len(self._prefix):])
        return out


@plugin
class WorkMemory:
    """ctx 服务 ``work_memory``：任务级进度缓存；Redis 不可用自动降级内存版。

    委托给 ``self._impl``；委托调用内部吞掉异常（仅告警），保证工作记忆异常绝不拖累主链路
    （react 即便不包 try/except 也安全）。
    """

    name = "agent.sys.work_memory"
    version = "0.1.0"
    layer = "system"
    provides = [Services.WORK_MEMORY]
    inject = []
    description = "工作记忆：任务级进度缓存（Redis 后端，不可用降级进程内）"

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
