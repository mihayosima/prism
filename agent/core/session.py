"""agent.core.session —— 会话/租户身份原语。

与 ``state_keys.py`` / ``services_catalog.py`` 同层：纯值对象 + 纯函数，
无 IO、无插件依赖、不注册服务。身份的解析在适配层，
身份的消费在各插件——本模块只负责「身份长什么样、键怎么拼」。

为什么这一层必须存在：

1. 键构造单一真相源：``task_id`` / 快照键 / 会话键的形状若有第二处实现，
 迟早出现形状漂移。
2. 输入净化的唯一闸门：``session`` 可来自客户端请求头。
 若不净化，``X-Session-Id: *`` 会让 ``snapshot_list`` 的前缀扫描匹配到
 别的会话甚至全租户——这不是样式问题，是越权读取。
 净化只在此处做一次，下游拿到的必是安全片段。
3. 宽容兼容既有键：2.1 已写入的快照 task_id 是二元组 ``{session}:{run}``，
 ``parse_task_id`` 需能无损读回，否则存量快照全部失效。

键设计：

 会话键 session_key = {user}:{session}
 run 键 task_id = {user}:{session}:{run_id}
 快照键 run_snapshot:{tenant}:{user}:{session}:{run_id}

⇒ 快照前缀可精确到会话级，
既消除「续接别的会话」，又把每请求的 SCAN 从全租户缩小几个数量级。

R1：本模块另承载请求作用域绑定通道。它是"身份如何抵达无 ctx 入参的消费点"
这一问题的唯一原语：``ctx.state`` 是容器级共享字典，并发请求交错时「当前会话」会被
后写覆盖。
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass

from .state_keys import StateKeys

DEFAULT_TENANT = "default"
DEFAULT_USER = "default"
DEFAULT_SESSION = "default"

# 身份片段字符集白名单。禁止 ``:``，否则可伪造键层级与 Redis glob
# 通配符 ``* ? [ ]``，否则前缀扫描会越权命中他人会话。长度 1-64 防滥用。
_SEG_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")
_ILLEGAL_HINT = "仅允许 [A-Za-z0-9_.-]，长度 1-64，且不得含 ':' 与通配符"


class SessionIdError(ValueError):
    """身份片段非法。适配层应把它转成 400。

 不静默回退到 ``default`` 是有意的：若伪造/非法的会话标识被悄悄改写为
 ``default``，用户会以为自己在私密会话，实则落进了与所有人共享的桶。
 """


def _seg(value: object, fallback: str, name: str) -> str:
    """校验并归一化一个身份片段；空值取 ``fallback``，非法值抛 ``SessionIdError``。"""
    s = str(value or "").strip()
    if not s:
        return fallback
    if not _SEG_RE.match(s):
        raise SessionIdError(f"{name} 含非法字符（{_ILLEGAL_HINT}）：{s[:32]!r}")
    return s


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """不可变身份三元组。``tenant``/``user`` 服务端配置决定，``session`` 可来自客户端。

 信任边界：``tenant``/``user`` 永不取自客户端。
 """

    tenant: str = DEFAULT_TENANT
    user: str = DEFAULT_USER
    session: str = DEFAULT_SESSION

    def __post_init__(self) -> None:
        # frozen dataclass 需用 object.__setattr__ 写回归一化后的值
        object.__setattr__(self, "tenant", _seg(self.tenant, DEFAULT_TENANT, "tenant"))
        object.__setattr__(self, "user", _seg(self.user, DEFAULT_USER, "user"))
        object.__setattr__(self, "session", _seg(self.session, DEFAULT_SESSION, "session"))

    # ---------- 派生键 ----------

    @property
    def session_key(self) -> str:
        """会话维度稳定键 ``{user}:{session}``。"""
        return f"{self.user}:{self.session}"

    @property
    def scope(self) -> str:
        """完整作用域 ``{tenant}:{user}:{session}``。"""
        return f"{self.tenant}:{self.user}:{self.session}"

    def task_id(self, run_id: str) -> str:
        """run 键 ``{user}:{session}:{run_id}``。"""
        return build_task_id(self, run_id)

    def snapshot_prefix(self) -> str:
        """本会话的快照键前缀 ``run_snapshot:{tenant}:{user}:{session}:``。

 SCAN 用；末尾带 ``:`` 是刻意的——保证不会把 ``session=abc`` 匹配到
 ``session=abcd`` 的快照。
 """
        return f"run_snapshot:{self.tenant}:{self.user}:{self.session}:"

    def snapshot_key(self, run_id: str) -> str:
        """单个 run 的快照键。"""
        return f"run_snapshot:{self.tenant}:{build_task_id(self, run_id)}"


def build_task_id(ident: SessionIdentity, run_id: str) -> str:
    """``{user}:{session}:{run_id}``。``run_id`` 由调用方生成。"""
    rid = str(run_id or "").strip()
    if not rid:
        raise SessionIdError("run_id 不得为空")
    if ":" in rid or not _SEG_RE.match(rid):
        raise SessionIdError(f"run_id 含非法字符（{_ILLEGAL_HINT}）：{rid[:32]!r}")
    return f"{ident.user}:{ident.session}:{rid}"


def parse_task_id(task_id: str) -> tuple[str, str, str]:
    """宽容解析 run 键 → ``(user, session, run_id)``。

 三段→ ``(user, session, run)``；两段
 → ``(DEFAULT_USER, session, run)``。存量快照因此不会失效。
 """
    parts = str(task_id or "").split(":")
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    if len(parts) == 2:
        return DEFAULT_USER, parts[0], parts[1]
    raise SessionIdError(f"task_id 形应为 {{user}}:{{session}}:{{run}}，得到 {task_id!r}")


def make_identity(*, tenant: object = None, user: object = None,
                  session: object = None) -> SessionIdentity:
    """构造身份；``None``/空串取默认值。非法输入抛 ``SessionIdError``。"""
    return SessionIdentity(tenant=tenant or DEFAULT_TENANT,
                           user=user or DEFAULT_USER,
                           session=session or DEFAULT_SESSION)


def coerce_identity(session_id: object) -> SessionIdentity:
    """由裸 ``session_id`` 退化出身份。

 - ``"user:session"``→ 拆两段；
 - ``"session"``→ user 取默认；
 - 任何非法值 → 退全默认，绝不因键形状拖垮主链路；身份是增强维度不是硬前提。
 """
    raw = str(session_id or "")
    try:
        if ":" in raw:
            user, _, sess = raw.partition(":")
            return make_identity(user=user or None, session=sess or None)
        return make_identity(session=raw or None)
    except SessionIdError:
        return make_identity()


# ---------- 请求作用域绑定 ----------
# 用 ContextVar 而不是继续只用 ``ctx.state``：后者是容器级共享字典，并发请求
# 交错时「当前会话」会被后写覆盖。ContextVar 按 asyncio task 隔离，而一个 HTTP
# 请求就是一个 task ⇒ 天然按请求隔离；请求内派生的子任务在创建瞬间复制当前上下文
# ⇒ 子任务读到的仍是本请求的值。内核同类做法见 ``context.py`` 的 ``CURRENT_PLUGIN``
# 读取优先 ContextVar，未绑定才回退 ``ctx.state``：既有「测试直接写
# ``root.state[SESSION_KEY]``」的写法与旧调用点因此保持有效。
# 「未绑定」与「绑定为空串」严格区分——否则收尾显式置空会被
# 回退分支重新读回上一轮的陈旧值。
_SESSION_KEY_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "prism_session_key", default=None)
_RUN_ID_VAR: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "prism_run_id", default=None)


def _state_value(ctx, key: str) -> str:
    """回退读取：从 ``ctx.state`` 取。"""
    if ctx is None:
        return ""
    try:
        return str((getattr(ctx, "state", None) or {}).get(key) or "")
    except Exception:                     # noqa: BLE001 —— 无状态通道即视作未绑定
        return ""


def set_current_session_key(session_key: object) -> None:
    """绑定当前请求的会话键 ``{user}:{session}``。

 传 ``None`` = 解除绑定——与
 ``bind_request_id`` 的语义对齐；收尾与测试隔离靠它复位，否则绑定会顺着 ContextVar
 泄漏到后续任务/用例，表现为"未绑定却读到上一个请求的值"这类跨任务串扰。
 """
    _SESSION_KEY_VAR.set(None if session_key is None else str(session_key or ""))


def current_session_key(ctx=None) -> str:
    """当前请求的会话键：ContextVar 优先，未绑定才回退 ``ctx.state``。"""
    v = _SESSION_KEY_VAR.get()
    return v if v is not None else _state_value(ctx, StateKeys.SESSION_KEY)


def set_current_run_id(run_id: object) -> None:
    """绑定当前 run 的归组键；run 收尾显式置空；``None`` = 解除绑定。"""
    _RUN_ID_VAR.set(None if run_id is None else str(run_id or ""))


def current_run_id(ctx=None) -> str:
    """当前 run 的归组键：ContextVar 优先，未绑定才回退 ``ctx.state``。"""
    v = _RUN_ID_VAR.get()
    return v if v is not None else _state_value(ctx, StateKeys.REACT_RUN_ID)


__all__ = ["SessionIdentity", "SessionIdError", "DEFAULT_TENANT", "DEFAULT_USER",
           "DEFAULT_SESSION", "build_task_id", "parse_task_id", "make_identity",
           "coerce_identity", "set_current_session_key", "current_session_key",
           "set_current_run_id", "current_run_id"]

