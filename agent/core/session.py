"""agent.core.session —— 会话/租户身份原语（阶段 2.2 多会话与租户模型）。

与 ``state_keys.py`` / ``services_catalog.py`` 同层：**纯值对象 + 纯函数**，
无 IO、无插件依赖、不注册服务。身份的**解析**在适配层（``run/chat.py``），
身份的**消费**在各插件——本模块只负责「身份长什么样、键怎么拼」。

为什么这一层必须存在（而不是各消费点自己拼字符串）：

1. **键构造单一真相源**：``task_id`` / 快照键 / 会话键的形状若有第二处实现，
   迟早出现形状漂移（例如 2.1 已落地的 ``run_snapshot:{tenant}:{task_id}``
   与 ``task_id={user}:{session}:{run}`` 一旦不一致，续接就会读空）。
2. **输入净化的唯一闸门**：``session`` 可来自**客户端请求头**（不可信）。
   若不净化，``X-Session-Id: *`` 会让 ``snapshot_list`` 的前缀扫描匹配到
   **别的会话甚至全租户**——这不是样式问题，是越权读取（H1 的安全面）。
   净化只在此处做一次，下游拿到的必是安全片段。
3. **宽容兼容既有键**：2.1 已写入的快照 task_id 是二元组 ``{session}:{run}``，
   ``parse_task_id`` 需能无损读回，否则存量快照全部失效。

键设计（遵 D11「一次到位」+ P-a 推荐方案）：

    会话键   session_key = {user}:{session}
    run 键   task_id     = {user}:{session}:{run_id}
    快照键   run_snapshot:{tenant}:{user}:{session}:{run_id}

⇒ 快照前缀可精确到**会话级**（``run_snapshot:{tenant}:{user}:{session}:``），
既消除「续接别的会话」，又把每请求的 SCAN 从全租户缩小几个数量级。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

DEFAULT_TENANT = "default"
DEFAULT_USER = "default"
DEFAULT_SESSION = "default"

# 身份片段字符集白名单。**禁止 ``:``**（否则可伪造键层级）与 **Redis glob
# 通配符 ``* ? [ ]``**（否则前缀扫描会越权命中他人会话）。长度 1-64 防滥用。
_SEG_RE = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")
_ILLEGAL_HINT = "仅允许 [A-Za-z0-9_.-]，长度 1-64，且不得含 ':' 与通配符"


class SessionIdError(ValueError):
    """身份片段非法。适配层应把它转成 400（fail-fast，不静默回退）。

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

    信任边界：**``tenant``/``user`` 永不取自客户端**（客户端可伪造 ⇒ 不下放）。
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
        """会话维度稳定键 ``{user}:{session}``（短期记忆分组、审批归属用）。"""
        return f"{self.user}:{self.session}"

    @property
    def scope(self) -> str:
        """完整作用域 ``{tenant}:{user}:{session}``（日志/审计用，可读）。"""
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
    """``{user}:{session}:{run_id}``。``run_id`` 由调用方生成（现有 ``uuid4().hex``）。"""
    rid = str(run_id or "").strip()
    if not rid:
        raise SessionIdError("run_id 不得为空")
    if ":" in rid or not _SEG_RE.match(rid):
        raise SessionIdError(f"run_id 含非法字符（{_ILLEGAL_HINT}）：{rid[:32]!r}")
    return f"{ident.user}:{ident.session}:{rid}"


def parse_task_id(task_id: str) -> tuple[str, str, str]:
    """宽容解析 run 键 → ``(user, session, run_id)``。

    三段（现行）→ ``(user, session, run)``；两段（2.1 存量 ``{session}:{run}``）
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
    """构造身份；``None``/空串取默认值。非法输入抛 ``SessionIdError``（适配层转 400）。"""
    return SessionIdentity(tenant=tenant or DEFAULT_TENANT,
                           user=user or DEFAULT_USER,
                           session=session or DEFAULT_SESSION)


def coerce_identity(session_id: object) -> SessionIdentity:
    """由裸 ``session_id`` **退化**出身份（``identity`` 未被显式线程化时的兼容路径）。

    - ``"user:session"``（2.2 起适配层传的会话键）→ 拆两段；
    - ``"session"``（旧调用方）→ user 取默认；
    - 任何非法值 → 退全默认（**绝不因键形状拖垮主链路**；身份是增强维度不是硬前提）。
    """
    raw = str(session_id or "")
    try:
        if ":" in raw:
            user, _, sess = raw.partition(":")
            return make_identity(user=user or None, session=sess or None)
        return make_identity(session=raw or None)
    except SessionIdError:
        return make_identity()


__all__ = ["SessionIdentity", "SessionIdError", "DEFAULT_TENANT", "DEFAULT_USER",
           "DEFAULT_SESSION", "build_task_id", "parse_task_id", "make_identity",
           "coerce_identity"]
