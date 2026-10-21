"""子 Agent 运行器。

与 react 主循环解耦：本模块绝不 import react.loop 的实现类，只经服务契约
``Services.REACT_FACTORY`` 取得工厂、构造受限子循环，并经 ``ctx.tasks`` / ``ctx.llm`` /
``ctx.events`` 协作。任何引擎替换
只改 react 插件内的工厂闭包，本模块零改动。

关键不变量：
- 深度护栏：用 ContextVar 跟踪当前委派深度，默认最多 1 层，
 子不可再委派。ContextVar 按 asyncio task 隔离，父/子各读各的，无需共享 state。
- 共享 state 隔离：子循环收尾会把容器级共享的 ``ctx.state[REACT_RUN_ID]`` 清空，
 故 handler 在派生前 save、await 结束后 restore 父的账单键。
- 子循环独立 task：必须 ``asyncio.create_task`` 运行子循环——否则子与主 handler 共
 同一 task，子 ``set_current_run_id`` 会污染父的请求作用域 ContextVar。
- 结构化返回：``{status, answer, evidence, request_id}``；子运行期异常被捕后转为结构化
 ``error`` 结果，交父循环作为观察自纠，不掀翻父 run。
"""

from __future__ import annotations

import asyncio
import contextvars
import copy
import hashlib
import logging
import pathlib
import uuid

from agent.api import (
    DEFAULT_DB_PATH, AgentError, DEFAULT_SESSION, DEFAULT_TENANT, DEFAULT_USER,
    Events, Services, StateKeys, current_run_id, current_session_key, durable,
    make_identity, now_iso,
)

from .roles import apply_leaf_rule, combine_tools, get_role, load_role_files, roles_dir

logger = logging.getLogger("agent.subagent")

# 子循环护栏，MVP 默认；阶段 3 起角色可覆盖，见 roles.MAX_ROLE_*
SUBAGENT_MAX_STEPS = 6           # 子循环步数上限
SUBAGENT_DEADLINE_S = 60.0       # 子循环整体时延上限；子循环自身的 deadline 闸据此收尾

# 阶段 2：并行委派护栏
MAX_SUBAGENT_THREADS = 4         # 每父 run 的在飞子 Agent 上限
MAX_SUBAGENT_BATCH = 16          # 单次 tools 调用最多委派的子任务数

# 阶段 3：防死循环
MAX_DELEGATIONS_PER_RUN = 24     # 同一父 run 内委派总次数上限，含同步/异步/并行展开
MAX_RESEND_PER_TASK = 3          # 同一任务文本的重派上限
MAX_PENDING_PER_RUN = 8          # 同一父 run 内未回收的异步句柄上限
MAX_TRACKED_RUNS = 256           # 记账字典上限

# 当前委派深度。主循环上下文为 0；每深入一层 +1。
_SUBAGENT_DEPTH = contextvars.ContextVar("subagent_depth", default=0)

# ---- 每父 run 的并发信号量 ----
# 审查意见 #4 明确：限制的是「同一个父 run 下同时在飞的子 Agent 数」。
# 键取父 run_id，无则回退会话键。
_PARENT_SEMS: dict[str, tuple[int, asyncio.Semaphore]] = {}
_PARENT_REFS: dict[str, int] = {}

# ---- 阶段 3：委派预算记账 ----
# 父 run 没有「结束」事件可挂清理 ⇒ 靠「上限 + 按插入序淘汰」兜住内存增长。
_RUN_DELEGATIONS: dict[str, int] = {}
_RUN_RESEND: dict[str, dict[str, int]] = {}

# ---- 阶段 3：异步委派句柄 ----
_PENDING: dict[str, dict[str, "_Pending"]] = {}
_BG_TASKS: set = set()


def _parent_run_key(ctx) -> str:
    """限流/预算/句柄的分桶键 = 父 run。

     只经 ContextVar 取父 run_id，不回落 ``ctx.state[REACT_RUN_ID]``：
     该共享键会被并发在跑的子循环改写，而异步委派
     的取回发生在另一次工具调用、子可能还在跑 ⇒ 若回落共享
     state，spawn 与 collect 会算出不同的键，句柄就"凭空消失"。ContextVar 按父 task 隔离且全程稳定，
     才是"父 run"的正确载体。
     """
    # 两者都不传 ctx ⇒ 只读 ContextVar，同上：不回落易变的共享 state，
    # 且 root ctx 与插件作用域 ctx 的 state 视图可能不同，必须取进程级稳定值。
    return (current_run_id() or current_session_key()
            or "__anonymous_parent__")


def _data_dir(ctx) -> str:
    """数据目录：取 db_path 的父目录，缺省回落 ``data``。"""
    # 缺省值经 SSOT 引用
    db_path = (ctx.config or {}).get("db_path") or DEFAULT_DB_PATH
    return str(pathlib.Path(db_path).parent)


def _catalog(ctx) -> list[dict]:
    """当前工具目录。"""
    try:
        return ctx.tasks.catalog() or []
    except Exception:                                  # 目录不可读 ⇒ 不做校验基准
        return []


class _Slot:
    """本父 run 的一个并发槽位。

     为什么不能用全局/每会话信号量：全局会把「另一个父 run 的子 Agent」算进来，
     无故压低吞吐；每会话则跨 run 串扰。每父 run 才是审查意见 #4 的语义。

     阶段 3：异步委派的槽位要跨工具调用持有，
     故除上下文管理器用法外，另提供 ``acquire()`` / ``release()`` 手工配对。
     """

    def __init__(self, ctx, limit: int):
        self._key = _parent_run_key(ctx) if limit > 0 else ""
        self._sem: asyncio.Semaphore | None = None
        if limit > 0:
            key = self._key
            if key not in _PARENT_SEMS:
                _PARENT_SEMS[key] = (limit, asyncio.Semaphore(limit))
                _PARENT_REFS[key] = 0
            elif _PARENT_SEMS[key][0] != limit:   # 限额变更 ⇒ 重建
                _PARENT_SEMS[key] = (limit, asyncio.Semaphore(limit))
            _PARENT_REFS[key] += 1
            self._sem = _PARENT_SEMS[key][1]

    async def acquire(self) -> None:
        if self._sem is not None:
            await self._sem.acquire()

    def release(self) -> None:
        """释放槽位并递减引用；幂等。"""
        if self._sem is None:
            return
        try:
            self._sem.release()
        finally:
            key = self._key
            _PARENT_REFS[key] -= 1
            if _PARENT_REFS.get(key, 0) <= 0:
                _PARENT_SEMS.pop(key, None)
                _PARENT_REFS.pop(key, None)
            self._sem = None             # 幂等：重复 release 不会二次递减

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *exc):
        self.release()
        return False


def _parent_slot(ctx, limit: int) -> _Slot:
    """占用本父 run 的一个并发槽位。"""
    return _Slot(ctx, limit)


# 阶段 3：委派预算与异步句柄

class _Pending:
    """一个在飞的异步委派。"""

    __slots__ = ("handle", "parent_key", "task", "role", "depth",
                 "child_session_key", "created_at", "task_obj")

    def __init__(self, *, handle: str, parent_key: str, task: str, role: str,
                 depth: int, child_session_key: str, task_obj):
        self.handle = handle
        self.parent_key = parent_key
        self.task = task
        self.role = role
        self.depth = depth
        self.child_session_key = child_session_key
        self.created_at = now_iso()
        self.task_obj = task_obj


def _task_fingerprint(task: str) -> str:
    """任务文本指纹，重派计数用；不落原文，避免长文本进内存键。"""
    return hashlib.sha1((task or "").strip().encode("utf-8")).hexdigest()[:16]


def _prune_runs() -> None:
    """记账字典超上限 ⇒ 按插入序淘汰最旧的父 run。"""
    for store in (_RUN_DELEGATIONS, _RUN_RESEND, _PENDING):
        while len(store) > MAX_TRACKED_RUNS:
            store.pop(next(iter(store)), None)


def reset_run_state(parent_key: str | None = None) -> None:
    """清空委派预算/句柄记账。"""
    if parent_key is None:
        _RUN_DELEGATIONS.clear()
        _RUN_RESEND.clear()
        _PENDING.clear()
        return
    _RUN_DELEGATIONS.pop(parent_key, None)
    _RUN_RESEND.pop(parent_key, None)
    _PENDING.pop(parent_key, None)


def _check_budget(ctx, task: str) -> dict | None:
    """委派预算校验；超限返回结构化 rejected。"""
    key = _parent_run_key(ctx)
    used = _RUN_DELEGATIONS.get(key, 0)
    if used >= MAX_DELEGATIONS_PER_RUN:
        return {"status": "rejected", "answer": None, "request_id": "",
                "evidence": {"reason": "delegation_budget_exhausted",
                             "used": used, "limit": MAX_DELEGATIONS_PER_RUN,
                             "detail": ("本轮已用完委派预算：请自行收敛给出结论，"
                                        "不要再委派子 Agent。")}}
    fp = _task_fingerprint(task)
    resent = _RUN_RESEND.get(key, {}).get(fp, 0)
    if resent >= MAX_RESEND_PER_TASK:
        return {"status": "rejected", "answer": None, "request_id": "",
                "evidence": {"reason": "resend_limit_exceeded",
                             "used": resent, "limit": MAX_RESEND_PER_TASK,
                             "detail": ("同一任务已被重派到上限次数（防「打回」死循环）："
                                        "请接受现有结果或换个思路，不要再重派同一任务。")}}
    pending_n = len(_PENDING.get(key, {}))
    if pending_n >= MAX_PENDING_PER_RUN:
        return {"status": "rejected", "answer": None, "request_id": "",
                "evidence": {"reason": "pending_handles_exhausted",
                             "used": pending_n, "limit": MAX_PENDING_PER_RUN,
                             "detail": ("未回收的异步委派句柄已达上限：请先收集既有结果"
                                        "（传 handle），再发起新的异步委派。")}}
    return None


def _charge_budget(ctx, task: str) -> None:
    """记账一次委派。"""
    key = _parent_run_key(ctx)
    _RUN_DELEGATIONS[key] = _RUN_DELEGATIONS.get(key, 0) + 1
    fp = _task_fingerprint(task)
    bucket = _RUN_RESEND.setdefault(key, {})
    bucket[fp] = bucket.get(fp, 0) + 1
    _prune_runs()


def pending_handles(ctx) -> list[str]:
    """本父 run 未回收的异步句柄。"""
    return sorted(_PENDING.get(_parent_run_key(ctx), {}))


async def cancel_all(reason: str = "teardown") -> int:
    """取消并回收全部在飞异步委派；返回取消数量。"""
    tasks = [t for t in _BG_TASKS if not t.done()]
    for t in tasks:
        t.cancel()
    _BG_TASKS.clear()
    for pend in [p for bucket in _PENDING.values() for p in bucket.values()]:
        if pend.task_obj is not None and not pend.task_obj.done():
            pend.task_obj.cancel()
    _PENDING.clear()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    logger.info("异步委派全部回收（reason=%s，取消 %d 个）", reason, len(tasks))
    return len(tasks)


async def _child_wrapper(loop, ctx, prompt: str, identity, depth: int):
    """在独立 task 中跑子循环，并把委派深度 +1。"""
    token = _SUBAGENT_DEPTH.set(depth)
    try:
        return await loop.run(ctx, prompt, identity=identity)
    finally:
        _SUBAGENT_DEPTH.reset(token)


async def _async_child(loop, ctx, prompt: str, identity, depth: int,
                       saved_rid, saved_skey, slot: _Slot | None):
    """异步委派的子任务体：在独立 task 中跑，收尾自行复原父共享键并释放槽位。

     与同步路径的差别：同步路径由 ``run_subagent`` 在 ``await`` 后复原；异步路径的
     ``await`` 发生在另一次工具调用里，故复原必须由子任务
     自己在 finally 完成——否则父的共享账单键会停在子的值上。
     """
    token = _SUBAGENT_DEPTH.set(depth)
    try:
        return await loop.run(ctx, prompt, identity=identity)
    finally:
        _SUBAGENT_DEPTH.reset(token)
        ctx.state[StateKeys.REACT_RUN_ID] = saved_rid
        ctx.state[StateKeys.SESSION_KEY] = saved_skey
        if slot is not None:
            slot.release()
        _BG_TASKS.discard(asyncio.current_task())


# 委派准备与结果结构化

class _Prepared:
    """一次委派的装配结果。"""

    __slots__ = ("role_name", "loop", "prompt", "identity", "depth", "tools_override")

    def __init__(self, *, role_name, loop, prompt, identity, depth, tools_override):
        self.role_name = role_name
        self.loop = loop
        self.prompt = prompt
        self.identity = identity
        self.depth = depth
        self.tools_override = tools_override


def _governance_for(ctx, role_obj):
    """Q4：按角色 ``context`` 派生一个治理器副本。

     解耦约束：本模块不得 import context 插件的实现类 ⇒ 只能对容器现取到的治理器
     实例做浅拷贝 + 同类型属性覆盖：
     - 治理器上没有该属性 ⇒ 诚实跳过并记账；
     - 类型不一致⇒ 跳过，防内部比较炸；
     - 无任何属性被覆盖 ⇒ 返回 ``None``。
     """
    spec = getattr(role_obj, "context", None) or {}
    if not spec:
        return None
    if not ctx.has(Services.CONTEXT):
        logger.warning("角色 %s 声明了 context 覆盖，但容器无 context 服务 ⇒ 忽略",
                       role_obj.name)
        return None
    gov = ctx.get(Services.CONTEXT)
    if gov is None:
        return None
    try:
        clone = copy.copy(gov)
    except Exception as exc:                      # 不可拷贝 ⇒ 不冒险改原对象
        logger.warning("治理器不可拷贝，跳过角色 context 覆盖：%s", exc)
        return None
    applied: dict = {}
    for k, v in spec.items():
        cur = getattr(clone, k, None)
        if cur is None and not hasattr(clone, k):
            logger.warning("角色 %s 的 context.%s 在治理器上不存在 ⇒ 跳过",
                           role_obj.name, k)
            continue
        if isinstance(cur, bool) or not isinstance(cur, (int, float, str)):
            continue
        if isinstance(v, int) and isinstance(cur, float):
            v = float(v)
        if not isinstance(v, type(cur)):
            logger.warning("角色 %s 的 context.%s 类型不匹配（%s vs %s）⇒ 跳过",
                           role_obj.name, k, type(v).__name__, type(cur).__name__)
            continue
        setattr(clone, k, v)
        applied[k] = v
    if not applied:
        return None
    logger.info("角色 %s 的上下文治理覆盖生效：%s", role_obj.name, applied)
    return clone


def _prepare(ctx, *, task: str, role: str, param_tools: list | None,
             max_depth: int) -> _Prepared:
    """装配一次委派。"""
    depth = _SUBAGENT_DEPTH.get()
    if depth >= max_depth:
        raise AgentError(
            f"已达子 Agent 最大递归深度（{max_depth}），禁止继续委派；"
            f"如需更深层次，请显式放宽 max_depth 并评估风险")

    extra_roles = load_role_files(roles_dir(_data_dir(ctx)), _catalog(ctx))
    role_obj = get_role(role, extra_roles)

    # ---- 工具三层交集的下两层 + 「子永远是叶子」 ----
    try:
        narrowed = combine_tools(role_obj.tools, param_tools)
    except ValueError as exc:
        raise AgentError(str(exc))
    tools_override = apply_leaf_rule(narrowed)

    factory = ctx.get(Services.REACT_FACTORY)
    if factory is None:
        raise AgentError("子 Agent 引擎（react.factory）未就绪：react 插件未启用或被停用")

    cfg_overrides = {
        "max_steps": int(role_obj.max_steps or SUBAGENT_MAX_STEPS),
        "deadline_s": float(role_obj.deadline_s or SUBAGENT_DEADLINE_S),
    }
    loop = factory(
        tools_override=tools_override,
        cfg_overrides=cfg_overrides,
        governance_override=_governance_for(ctx, role_obj),
        depth=depth + 1,
    )

    sk = current_session_key(ctx) or ""
    user, _, session = sk.partition(":")
    identity = make_identity(
        tenant=DEFAULT_TENANT,
        user=(user or None) or DEFAULT_USER,
        session=(session or None) or DEFAULT_SESSION,
    )
    prompt = (f"{role_obj.system_note}\n\n任务：{task}" if role_obj.system_note
              else f"任务：{task}")
    return _Prepared(role_name=role_obj.name, loop=loop, prompt=prompt,
                     identity=identity, depth=depth + 1,
                     tools_override=tools_override)


def _structured(status: str, result=None, exc: BaseException | None = None) -> dict:
    """把子循环结果/异常转成结构化返回。"""
    if exc is not None:
        return {"status": "error", "answer": None, "request_id": "",
                "evidence": {"error": type(exc).__name__,
                             "detail": str(exc)[:500]}}
    if result is None:
        return {"status": "error", "answer": None, "request_id": "",
                "evidence": {"error": "empty_result", "detail": "子循环未返回结果"}}
    if getattr(result, "ok", False) and not getattr(result, "exhausted", False):
        status = "ok"
    elif getattr(result, "exhausted", False):
        status = "exhausted"
    else:
        status = "error"
    return {"status": status,
            "answer": getattr(result, "text", None),
            "evidence": (getattr(result, "as_evidence", lambda: {})() or {}),
            "request_id": getattr(result, "run_id", "") or ""}


async def _emit_spawn(ctx, *, parent_rid, child_rid, child_session_key, role,
                      depth, task, status, error: str = "") -> None:
    """委派审计；父 run 上下文内调用 ⇒ ContextVar 正确。"""
    payload = {"parent_run_id": parent_rid,
               "parent_session_key": current_session_key(ctx),
               "child_run_id": child_rid, "child_session_key": child_session_key,
               "role": role, "depth": depth, "task": (task or "")[:500],
               "status": status, "at": now_iso()}
    if error:
        payload["error"] = error
    await durable(ctx, Events.SUBAGENT_SPAWN, payload)


async def run_subagent(ctx, *, task: str, role: str, param_tools: list | None,
                       max_depth: int, max_threads: int = MAX_SUBAGENT_THREADS) -> dict:
    """委派一个受限子循环并回收结构化结果。

     参数
     ----
     task 委派给子的独立任务描述。
     role 角色名。
     param_tools 调用方在角色白名单基础上进一步收窄的工具名列表。
     max_depth 最大委派深度；达到即拒绝继续委派。

     返回
     ----
     ``{"status", "answer", "evidence", "request_id"}``
     status ∈ {ok, exhausted, error}。
     """
    rejected = _check_budget(ctx, task)
    if rejected is not None:                       # 预算/重派超限：结构化，不抛
        return rejected
    prep = _prepare(ctx, task=task, role=role, param_tools=param_tools,
                    max_depth=max_depth)
    _charge_budget(ctx, task)

    parent_rid = current_run_id(ctx)                 # 父 run_id
    saved_rid = ctx.state.get(StateKeys.REACT_RUN_ID)
    # 读会话键必须经原语
    saved_skey = current_session_key(ctx)

    result = None
    exc_info: BaseException | None = None
    try:
        # 阶段 2：占用本父 run 的一个并发槽位
        async with _parent_slot(ctx, max_threads):
            child_task = asyncio.create_task(
                _child_wrapper(prep.loop, ctx, prep.prompt, prep.identity, prep.depth))
            try:
                result = await child_task
            except BaseException as exc:              # 子运行期异常：结构化，不裸抛
                exc_info = exc
    finally:
        # 还原父的共享账单键
        # 并行下多个子会互相改写该共享键，但每个子保存的都是同一个父值，
        # 故无论谁最后收尾，最终都回到父值；审计走 ContextVar，不受中间态影响。
        ctx.state[StateKeys.REACT_RUN_ID] = saved_rid
        ctx.state[StateKeys.SESSION_KEY] = saved_skey

    if exc_info is not None:
        await _emit_spawn(ctx, parent_rid=parent_rid, child_rid="",
                          child_session_key=prep.identity.session_key,
                          role=prep.role_name, depth=prep.depth, task=task,
                          status="error", error=type(exc_info).__name__)
        return _structured("error", exc=exc_info)

    out = _structured("ok", result=result)
    await _emit_spawn(ctx, parent_rid=parent_rid,
                      child_rid=getattr(result, "run_id", "") or "",
                      child_session_key=prep.identity.session_key,
                      role=prep.role_name, depth=prep.depth, task=task,
                      status=out["status"])
    return out


async def run_subagents_parallel(ctx, *, tasks: list[str], role: str,
                                 param_tools: list | None, max_depth: int,
                                 max_threads: int = MAX_SUBAGENT_THREADS) -> dict:
    """并行委派多个子任务：``asyncio.gather`` + 每父 run 信号量限流。

     不改共享 Actor：并行 fan-out 全部发生在本工具 handler 内，
     react 的 ``DefaultActor`` 仍是一次 ``await`` 拿回一个工具结果。

     隔离保证：
     - 每个子跑在独立 asyncio task ⇒ ``_SUBAGENT_DEPTH`` / run_id 等 ContextVar
     各自独立，互不污染；
     - 共享 ``ctx.state[REACT_RUN_ID]`` 会被并发改写，但每个子保存的都是同一个父值
     ⇒ 全部收尾后必然回到父值。

     部分失败语义：单个子失败不影响其他子，各自结构化上报；整体按
     `` 全部成功=ok / 部分成功=partial / 全部失败=error`` 汇总，父循环可据此自纠。

     返回 ``{"status", "count", "succeeded", "failed", "results", "request_ids"}``。
     """
    if not tasks:
        raise AgentError("并行委派：tasks 为空，至少要给一个子任务")
    if len(tasks) > MAX_SUBAGENT_BATCH:
        raise AgentError(f"并行委派子任务数 {len(tasks)} 超过上限 {MAX_SUBAGENT_BATCH}")

    async def _one(index: int, one_task: str) -> dict:
        try:
            out = await run_subagent(ctx, task=one_task, role=role,
                                     param_tools=param_tools, max_depth=max_depth,
                                     max_threads=max_threads)
        except BaseException as exc:                  # 单子失败不掀翻整批
            out = {"status": "error", "answer": None,
                   "evidence": {"error": type(exc).__name__,
                                "detail": str(exc)[:500]},
                   "request_id": ""}
        return {"index": index, "task": one_task, **out}

    raw = await asyncio.gather(*[_one(i, t) for i, t in enumerate(tasks)])
    results = list(raw)
    ok_n = sum(1 for r in results if r.get("status") in ("ok", "exhausted"))
    fail_n = len(results) - ok_n
    if fail_n == 0:
        status = "ok"
    elif ok_n == 0:
        status = "error"
    else:
        status = "partial"
    return {"status": status, "count": len(results), "succeeded": ok_n,
            "failed": fail_n, "results": results,
            "request_ids": [r.get("request_id") or "" for r in results]}


# 阶段 3：异步委派
# 设计取舍：「完成时注入下一轮 react 的 tool_result」需要
# react 侧提供「外部消息注入」接缝，而阶段 0 预留的五参数接缝里没有它；
# 与其现在改共享 Actor，不如把结果回收做成
# 一次 await：父再次调用 ``agent.delegate(handle=...)`` 即阻塞到该句柄完成，
# 语义上等价于「结果在下一轮被取回」，且没有轮询。

async def start_subagent(ctx, *, task: str, role: str, param_tools: list | None,
                         max_depth: int,
                         max_threads: int = MAX_SUBAGENT_THREADS) -> dict:
    """发起一个异步委派并立即返回句柄。

     返回 ``{"status": "pending", "handle", "role", "depth", "pending": [...]}``。
     句柄按父 run 分桶：别的 run 拿不到，父 run 记账随 teardown 一起清空。
     """
    rejected = _check_budget(ctx, task)
    if rejected is not None:
        return rejected
    prep = _prepare(ctx, task=task, role=role, param_tools=param_tools,
                    max_depth=max_depth)
    _charge_budget(ctx, task)

    parent_key = _parent_run_key(ctx)
    saved_rid = ctx.state.get(StateKeys.REACT_RUN_ID)
    saved_skey = current_session_key(ctx)             # 读点必须经原语
    slot = _Slot(ctx, max_threads)
    await slot.acquire()                              # 槽位跨调用持有，子跑完才释放
    handle = uuid.uuid4().hex[:12]
    child = asyncio.create_task(
        _async_child(prep.loop, ctx, prep.prompt, prep.identity, prep.depth,
                     saved_rid, saved_skey, slot),
        name=f"subagent-{handle}")
    _BG_TASKS.add(child)
    _PENDING.setdefault(parent_key, {})[handle] = _Pending(
        handle=handle, parent_key=parent_key, task=task, role=prep.role_name,
        depth=prep.depth, child_session_key=prep.identity.session_key,
        task_obj=child)
    await _emit_spawn(ctx, parent_rid=current_run_id(ctx), child_rid="",
                      child_session_key=prep.identity.session_key,
                      role=prep.role_name, depth=prep.depth, task=task,
                      status="pending")
    return {"status": "pending", "handle": handle, "role": prep.role_name,
            "depth": prep.depth, "task": (task or "")[:200],
            "pending": pending_handles(ctx)}


async def await_subagent(ctx, handle: str, timeout_s: float = 0.0) -> dict:
    """等待一个异步委派句柄完成，返回与同步委派同构的结果。

     - 超时⇒ 返回 ``status="pending"``
     且 ``timed_out=True``：子仍在跑；
     - 句柄不存在/已回收 ⇒ 结构化 ``error``，并回带当前可用句柄清单；
     - 子成功/失败 ⇒ 与同步路径同构的 ``{status, answer, evidence, request_id}``
     + ``handle``，同时落一条 ``SUBAGENT_SPAWN`` 完成审计。
     """
    key = _parent_run_key(ctx)
    pend = _PENDING.get(key, {}).get(str(handle or "").strip())
    if pend is None:
        return {"status": "error", "answer": None, "request_id": "",
                "evidence": {"error": "unknown_handle",
                             "detail": (f"句柄 {handle!r} 不在当前 run（已回收或从未创建）；"
                                        f"当前可用：{pending_handles(ctx)}")}}
    limit = float(timeout_s or SUBAGENT_DEADLINE_S)
    result = None
    exc_info: BaseException | None = None
    try:
        # shield：超时只取消「等待」，不取消子在跑的任务
        result = await asyncio.wait_for(asyncio.shield(pend.task_obj), limit)
    except asyncio.TimeoutError:
        return {"status": "pending", "handle": pend.handle, "timed_out": True,
                "waited_s": limit, "role": pend.role,
                "task": (pend.task or "")[:200]}
    except BaseException as exc:                    # 含 CancelledError：结构化，不裸抛
        exc_info = exc
    _PENDING.get(key, {}).pop(pend.handle, None)
    out = _structured("ok", result=result, exc=exc_info)
    out["handle"] = pend.handle
    await _emit_spawn(ctx, parent_rid=current_run_id(ctx),
                      child_rid=out.get("request_id") or "",
                      child_session_key=pend.child_session_key, role=pend.role,
                      depth=pend.depth, task=pend.task, status=out["status"],
                      error=(type(exc_info).__name__ if exc_info is not None else ""))
    return out


async def collect_pending(ctx, timeout_s: float = 0.0) -> dict:
    """回收本父 run 全部在飞的异步委派，按句柄创建序返回。

     用途：父一次 fan-out 多个 ``wait=false`` 后，用一次调用把它们全部 join 回来。
     """
    handles = pending_handles(ctx)
    if not handles:
        return {"status": "ok", "count": 0, "succeeded": 0, "failed": 0,
                "results": [], "detail": "当前 run 没有在飞的异步委派"}
    results = [await await_subagent(ctx, h, timeout_s) for h in handles]
    ok_n = sum(1 for r in results if r.get("status") in ("ok", "exhausted"))
    pend_n = sum(1 for r in results if r.get("status") == "pending")
    status = ("ok" if pend_n == 0 and ok_n == len(results)
              else ("pending" if pend_n else ("partial" if ok_n else "error")))
    return {"status": status, "count": len(results), "succeeded": ok_n,
            "failed": len(results) - ok_n - pend_n, "pending": pend_n,
            "results": results}
