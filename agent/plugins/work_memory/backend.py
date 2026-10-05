"""agent.plugins.work_memory.backend —— 工作记忆两种后端实现（Redis / 进程内）。

- ``RedisWorkMemory``：Redis 后端（duck-typed ``redis.asyncio`` 客户端）；
- ``InMemoryWorkMemory``：降级实现（进程内 dict，接口与 Redis 版一致）；
- ``WorkMemoryLike``：接缝契约（消费方依此调用，可注入替换）。

边界：只存任务进度态（step / tool_trace / checkpoints / status），**不存对话全文**
（对话历史属短期记忆 ``agent_messages``，跨会话知识属长期记忆 ``plugins.memory``）。

零特权：本模块仅依赖标准库；Redis 客户端由插件 ``__init__`` 延迟导入并注入，
不在本模块 import 第三方，也不反向依赖 sys/core（K0 守卫）。
"""

from __future__ import annotations

import json
import time
from typing import Any, Protocol


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


# ---- 跨进程 run 快照键空间（阶段 2 / D1：与 wm: 完全独立）------------------------
# 纪律：``finish`` 只删 ``wm:{task_id}``，**绝不**触及 ``run_snapshot:`` 前缀。三种键独立：
#   wm:{task_id}                             —— run 期间；finish 删；供运行内 wm_read 渐进披露。
#   run_snapshot:{tenant}:{task_id}          —— 每轮写；长 TTL；跨进程恢复依据；finish 不碰。
#   run_snapshot_finished:{tenant}:{task_id} —— mark_finished 失败兜底确认键（短 TTL，防误判崩溃）。
_SNAP_PREFIX = "run_snapshot:"
_SNAP_FLAG_PREFIX = "run_snapshot_finished:"
_SNAP_FLAG_TTL = 300


def _snap_key(tenant: str, task_id: str) -> str:
    return f"{_SNAP_PREFIX}{tenant or 'default'}:{task_id}"


def _snap_flag_key(tenant: str, task_id: str) -> str:
    return f"{_SNAP_FLAG_PREFIX}{tenant or 'default'}:{task_id}"


class WorkMemoryLike(Protocol):
    """工作记忆接缝契约（消费方依此调用，可注入替换）。"""

    async def begin(self, task_id: str, meta: dict) -> None: ...
    async def update(self, task_id: str, **fields) -> None: ...
    async def checkpoint(self, task_id: str, name: str, data: Any) -> None: ...
    async def get(self, task_id: str) -> dict | None: ...
    async def finish(self, task_id: str) -> None: ...
    async def list_active(self, session_id: str | None = None) -> list[str]: ...
    # ---- 跨进程 run 快照（阶段 2；键 run_snapshot:{tenant}:{task_id}，与 wm: 独立）----
    async def snapshot_save(self, tenant: str, task_id: str, data: dict,
                            ttl: int | None = None) -> None: ...
    async def snapshot_load(self, tenant: str, task_id: str) -> dict | None: ...
    # ``prefix``：会话级精确前缀（``run_snapshot:{tenant}:{user}:{session}:``）。
    # 缺省 = 租户级（``run_snapshot:{tenant}:``）——全租户扫描会**跨会话命中**（H1）。
    async def snapshot_list(self, tenant: str, prefix: str | None = None) -> list[dict]: ...
    async def snapshot_mark_finished(self, tenant: str, task_id: str) -> None: ...
    async def snapshot_flag_finished(self, tenant: str, task_id: str) -> None: ...
    #: 取消/异常路径专用（H1）：把 ``running`` 挪到 ``cancelled``。
    #: 老实现没有该方法时，调用方（react loop）会**跳过**并记计数——不劣化、不崩。
    async def snapshot_mark_abandoned(self, tenant: str, task_id: str) -> None: ...


class InMemoryWorkMemory:
    """降级实现：进程内 dict，接口与 Redis 版一致。

    仅在单进程、无 Redis 时生效；跨进程 / 重启后失效（与 Redis 语义一致，只是不跨进程）。
    """

    def __init__(self, ttl: int = 3600, prefix: str = "wm:",
                 snapshot_ttl: int = 86400):
        self._ttl = ttl
        self._prefix = prefix
        self._store: dict[str, tuple[float, dict]] = {}   # key -> (expire_at, data)
        self._snapshot_ttl = snapshot_ttl
        # 快照键 -> (expire_at, json_str)：JSON 字符串存储，语义与 Redis 后端严格一致
        self._snap: dict[str, tuple[float, str]] = {}

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

    # ---- 跨进程 run 快照（阶段 2）----（JSON 字符串存储，与 Redis 后端语义一致）

    async def snapshot_save(self, tenant: str, task_id: str, data: dict,
                            ttl: int | None = None) -> None:
        t = self._snapshot_ttl if ttl is None else ttl
        self._snap[_snap_key(tenant, task_id)] = (
            time.time() + t, json.dumps(data, ensure_ascii=False))

    async def snapshot_load(self, tenant: str, task_id: str) -> dict | None:
        key = _snap_key(tenant, task_id)
        entry = self._snap.get(key)
        if entry is None:
            return None
        exp, raw = entry
        if exp < time.time():
            self._snap.pop(key, None)
            return None
        try:
            return json.loads(raw)
        except Exception:                       # noqa: BLE001 —— 坏数据视同缺失
            return None

    async def snapshot_list(self, tenant: str, prefix: str | None = None) -> list[dict]:
        # 2.2（H1）：``prefix`` 给定时按**会话级**精确匹配；缺省退化为租户级（旧行为）。
        # ★ 关键纪律：``task_id`` **一律按租户前缀剥离**。扫描前缀可以窄（会话级），
        #   但回读走 ``snapshot_load(tenant, task_id)`` = 租户前缀 + **完整** task_id；
        #   若按扫描前缀剥离，返回会只剩 run_id，续接时按它回读必然落空。
        now = time.time()
        base = f"{_SNAP_PREFIX}{tenant or 'default'}:"
        pfx = prefix or base
        out: list[dict] = []
        for key, (exp, raw) in list(self._snap.items()):
            if exp < now or not key.startswith(pfx):
                continue
            try:
                data = json.loads(raw)
            except Exception:                   # noqa: BLE001
                continue
            out.append({"task_id": key[len(base):] if key.startswith(base) else key,
                        "status": data.get("status"), "ts": data.get("ts") or ""})
        out.sort(key=lambda x: x.get("ts") or "", reverse=True)
        return out

    async def snapshot_mark_finished(self, tenant: str, task_id: str) -> None:
        key = _snap_key(tenant, task_id)
        entry = self._snap.get(key)
        if entry is None:
            return
        exp, raw = entry
        try:
            data = json.loads(raw)
        except Exception:                       # noqa: BLE001
            return
        data["status"] = "finished"
        self._snap[key] = (exp, json.dumps(data, ensure_ascii=False))

    async def snapshot_mark_abandoned(self, tenant: str, task_id: str) -> None:
        """H1：取消/异常路径把 ``running`` → ``cancelled``。

        ★ 为什么非 ``finished`` 不可：把取消标成"已完成"是对状态的**谎报**——审计里
        查不到"这个 run 是被中断的"。而 ``_probe_resume`` 只认 ``status=="running"``，
        标成 ``cancelled`` 同样能阻止它被当崩溃残留自动续接，但**保留了真相**。
        """
        key = _snap_key(tenant, task_id)
        entry = self._snap.get(key)
        if entry is None:
            return
        exp, raw = entry
        try:
            data = json.loads(raw)
        except Exception:                       # noqa: BLE001
            return
        data["status"] = "cancelled"
        self._snap[key] = (exp, json.dumps(data, ensure_ascii=False))

    async def snapshot_flag_finished(self, tenant: str, task_id: str) -> None:
        self._snap[_snap_flag_key(tenant, task_id)] = (
            time.time() + _SNAP_FLAG_TTL, "1")


class RedisWorkMemory:
    """Redis 后端实现（duck-typed ``redis.asyncio`` 客户端）。

    字段：``wm:{task_id}`` hash；session 索引 ``wm:idx:{session_id}`` set（供 list_active）。
    所有可变字段以字符串存储；list/dict 经 json.dumps。
    """

    def __init__(self, client, ttl: int = 3600, prefix: str = "wm:",
                 snapshot_ttl: int = 86400):
        self._r = client
        self._ttl = ttl
        self._prefix = prefix
        self._snapshot_ttl = snapshot_ttl

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
        # D1 锁死：只删工作记忆键 wm:{task_id}，**绝不**触及 run_snapshot: 前缀
        # （跨进程快照的存活是断点续传前提；finish 只结束"运行内"工作记忆）。
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
            ks = k if isinstance(k, str) else k.decode("utf-8")  # decode_responses 开/关两种形态
            if ks.endswith("idx") or ":idx:" in ks:    # 跳过索引键
                continue
            out.append(ks[len(self._prefix):])
        return out

    # ---- 跨进程 run 快照（阶段 2）----

    async def snapshot_save(self, tenant: str, task_id: str, data: dict,
                            ttl: int | None = None) -> None:
        t = self._snapshot_ttl if ttl is None else ttl
        await self._r.set(_snap_key(tenant, task_id),
                          json.dumps(data, ensure_ascii=False), ex=t)

    async def snapshot_load(self, tenant: str, task_id: str) -> dict | None:
        raw = await self._r.get(_snap_key(tenant, task_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:                       # noqa: BLE001
            return None

    async def snapshot_list(self, tenant: str, prefix: str | None = None) -> list[dict]:
        # 2.2（H1）：``prefix`` 给定时按**会话级**精确匹配（SCAN 范围随之缩小几个数量级）；
        # 缺省退化为租户级（旧行为，仅管理面/兼容用）。
        # ★ 关键纪律（同 InMemory）：``task_id`` **一律按租户前缀剥离**——扫描前缀可窄，
        #   但回读 ``snapshot_load(tenant, task_id)`` 用的是完整 task_id。
        base = f"{_SNAP_PREFIX}{tenant or 'default'}:"
        pfx = prefix or base
        out: list[dict] = []
        async for k in self._r.scan_iter(match=pfx + "*"):
            ks = k if isinstance(k, str) else k.decode("utf-8")
            raw = await self._r.get(ks)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:                   # noqa: BLE001
                continue
            out.append({"task_id": ks[len(base):] if ks.startswith(base) else ks,
                        "status": data.get("status"), "ts": data.get("ts") or ""})
        out.sort(key=lambda x: x.get("ts") or "", reverse=True)
        return out

    async def snapshot_mark_finished(self, tenant: str, task_id: str) -> None:
        key = _snap_key(tenant, task_id)
        raw = await self._r.get(key)
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:                       # noqa: BLE001
            return
        data["status"] = "finished"
        # 重设 TTL（收尾后快照寿命回归 snapshot_ttl；不用 keepttl 以规避 fakeredis 兼容性）
        await self._r.set(key, json.dumps(data, ensure_ascii=False),
                          ex=self._snapshot_ttl)

    async def snapshot_mark_abandoned(self, tenant: str, task_id: str) -> None:
        """H1：取消/异常路径 → ``cancelled``（不标 finished：那是谎报，见 InMemory 版说明）。"""
        key = _snap_key(tenant, task_id)
        raw = await self._r.get(key)
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:                       # noqa: BLE001
            return
        data["status"] = "cancelled"
        await self._r.set(key, json.dumps(data, ensure_ascii=False),
                          ex=self._snapshot_ttl)

    async def snapshot_flag_finished(self, tenant: str, task_id: str) -> None:
        await self._r.set(_snap_flag_key(tenant, task_id), "1", ex=_SNAP_FLAG_TTL)


__all__ = ["WorkMemoryLike", "InMemoryWorkMemory", "RedisWorkMemory"]
