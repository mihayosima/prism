"""沙箱 confinement **后端**骨架（P0，2026-10-13）。

设计依据 ``docs/refactor/分级沙箱对齐ClaudeCode方案-2026-10-13.md`` v1.2。

★ **为什么落 ``core``**（分层铁律）：后端要被插件/传输层构造，而 ``agent/api.py``
**不得 import ``agent.sys``** ⇒ 任何需被插件取用的对象必须能被 api 导出 ⇒ 只能落 core。
本模块零依赖（只用 stdlib），与 ``SkillsCatalog`` 下沉同一纪律。

★ **本模块只定义"机制契约"，不做平台判定**：
真隔离（Windows 受限令牌 / WSL2 内 Landlock+seccomp）属 P1/P4，本轮只出 ``NullBackend``
（诚实 none）。``select_backend`` 对尚未实现的后端**如实报不可用**并回落，
绝不谎报已隔离。

契约形状（★ 由代码复核得出，勿改）：
    ``Services.CHILD_SPAWN_OPTIONS`` 的真实形状是 ``(session_key) -> {"cwd":…, "env":…} | None``，
    桥**只用一个参数**调用它，且**只消费 ``cwd`` / ``env`` 两个键**
    （见 ``plugins/playwright_mcp/__init__.py:99-104``）。
    ⇒ 本模块产出的 ``SpawnOptions`` 是**内核内部类型**，由插件侧 ``to_dict()`` 成字典后
    经接缝返回；**新增键对既有桥向后兼容**（它们不读），但**不得**把接缝改成三参或返回
    dataclass —— 那会直接改崩两个桥。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

__all__ = [
    "STRENGTHS", "BACKENDS", "NET_POLICIES",
    "SpawnOptions", "ConfinementBackend", "NullBackend",
    "BackendUnavailable", "select_backend", "describe_backend",
]

#: 强度三档（**报告用**，不是配置档位）：实际做到了多少。
STRENGTHS = ("none", "partial", "full")

#: 后端枚举（state 轴）。``auto`` = 按平台能力探测；``off``/``null`` = 一等"非沙箱"档。
BACKENDS = ("auto", "off", "null", "win-token", "wsl2", "container", "cloud")

#: 网络策略轴。
NET_POLICIES = ("allow", "deny", "proxy")


class BackendUnavailable(RuntimeError):
    """显式要求的后端在本机不可得（fail-closed 用）。

    ★ 语义边界：抛出**只代表"要不到隔离"**，不代表调用被拒绝。真正的"拒绝 spawn"
    需要桥侧配合（接缝无"拒绝"通道，见 ``select_backend`` 的 docstring），本轮
    ``strict`` 的落点是**可见告警 + 计数 + 回落**，而非硬阻断（默认 strict=false）。
    """


@dataclass
class SpawnOptions:
    """子进程启动选项（内核内部类型）。

    ★ 向后兼容纪律：``to_dict()`` 必须**至少**含 ``cwd`` / ``env`` 两键，
    因为既有桥（playwright / windows）只读这两个。其余键是**增强信息**：
    今天无消费者，P1（令牌）/P4（WSL2）/P7（网络代理）才用得上，
    提前把字段定义好是为了让后端实现不必再改接缝形状。
    """

    #: 落点收拢：子进程工作目录（``None`` = 继承宿主 cwd，历史行为）。
    cwd: str | None = None
    #: 需**覆盖**的环境变量（调用方合并，不是全量环境）。
    env: dict[str, str] = field(default_factory=dict)
    #: 声明的文件策略（``none``/``read-only``/``workspace-write``）。
    mode: str = "none"
    #: 实际生效的后端名。
    backend: str = "null"
    #: 实际强度（``none``/``partial``/``full``）——**诚实值**，不由配置反推。
    strength: str = "none"
    #: 谁在强制（``none`` / ``windows-restricted-token`` / ``wsl2-landlock`` / ``backend-error``）。
    enforced_by: str = "none"
    #: 网络策略。
    net_policy: str = "allow"
    #: 代理地址（``net_policy="proxy"`` 时注入子进程 env）。
    proxy_addr: str | None = None
    #: 是否进独立网络命名空间（内核级断网；仅容器/WSL2 档可兑现）。
    netns: bool = False
    #: Windows 受限令牌句柄（P1；``None`` = 不用令牌）。
    token: int | None = None
    #: Windows 进程创建标志（P1 与令牌配套）。
    creationflags: int | None = None
    #: POSIX preexec 闭包（P4：unshare/landlock）。
    preexec: Callable[[], None] | None = None
    #: Linux seccomp filter fd（P4）。
    seccomp_fd: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """转成接缝字典（``CHILD_SPAWN_OPTIONS`` 的返回值形状）。

        ★ **只输出非空的可选字段**，避免把一堆 ``None`` 塞给今天不消费它们的桥。
        ``cwd`` / ``env`` / ``mode`` 恒在（既有消费者 + 审计同源）。
        """
        out: dict[str, Any] = {
            "cwd": self.cwd,
            "env": dict(self.env),
            "mode": self.mode,
            "backend": self.backend,
            "strength": self.strength,
            "enforced_by": self.enforced_by,
            "net_policy": self.net_policy,
        }
        if self.proxy_addr:
            out["proxy_addr"] = self.proxy_addr
        if self.netns:
            out["netns"] = True
        if self.token is not None:
            out["token"] = self.token
        if self.creationflags is not None:
            out["creationflags"] = self.creationflags
        if self.seccomp_fd is not None:
            out["seccomp_fd"] = self.seccomp_fd
        # preexec 是闭包，**不可序列化** ⇒ 不入字典（仅内核内部传递用）
        return out


class ConfinementBackend(Protocol):
    """隔离后端协议。

    实现者只回答两件事：**能不能**（``capability_probe``）与**怎么做**
    （``spawn_options``）。**不判准入**——那是 guard/approval 的活（2026-10-03 收口）。
    """

    #: 后端名（与配置 ``backend`` 枚举一致）。
    name: str
    #: 该后端**实际**能达到的强度（诚实值，不由配置反推）。
    strength: str

    def capability_probe(self) -> dict:
        """探测本机/本平台能否启用：``{"available": bool, "reason": str}``。"""
        ...

    def can_enforce(self, mode: str) -> bool:
        """该后端能否兑现 ``mode`` 这一文件策略。"""
        ...

    def spawn_options(self, base: SpawnOptions, policy: dict,
                      session_key: str = "") -> SpawnOptions:
        """在落点收拢（``base``）之上叠加本后端的隔离信息。

        ★ 约定：**不改 ``base.cwd``/``base.env``**（那是落点收拢的职责），
        只填 ``backend``/``strength``/``enforced_by`` 与平台相关字段。
        """
        ...

    def describe(self) -> dict:
        """自述（供 ``status()`` / 审计）：``{name, strength, available, reason, gaps}``。"""
        ...


class NullBackend:
    """**非沙箱**后端（一等档位 off/null）——诚实 ``none``。

    ★ 它是**档位**不是"失败"：CC 日常执行即此态。保留落点收拢（``base`` 原样返回），
    但不叠加任何强制隔离 ⇒ ``strength="none"``、``enforced_by="none"``。
    """

    name = "null"
    strength = "none"

    def capability_probe(self) -> dict:
        return {"available": True, "reason": "非沙箱档位（off/null）：无隔离，一等档位"}

    def can_enforce(self, mode: str) -> bool:
        # 只有"本就没声明隔离"才算可兑现；声明了任何档位都兑现不了 ⇒ 调用方据此记 gap。
        return str(mode or "none") == "none"

    def spawn_options(self, base: SpawnOptions, policy: dict,
                      session_key: str = "") -> SpawnOptions:
        base.backend = self.name
        base.strength = self.strength
        base.enforced_by = "none"
        base.net_policy = str(policy.get("network") or "allow")
        return base

    def describe(self) -> dict:
        return {"name": self.name, "strength": self.strength, "available": True,
                "reason": "非沙箱（off/null）", "gaps": []}


#: 进程级后端的**探测顺序**（Windows）：wsl2 优先（与 CC 同构、最接近完整隔离），
#: 其次 Windows 原生令牌兜底。二者当前**均未实现**（P1/P4）⇒ 探测恒不可用。
#: ★ 这里只登记名字与"未实现"原因，实现落地时把类填进 ``_BACKEND_CLASSES`` 即可，
#: 不改 ``select_backend`` 的调用形状。
_PROCESS_CANDIDATES: tuple[str, ...] = ("wsl2", "win-token")

#: 已实现的后端类表。P0 只有 null；P1/P4 落地后在此登记，``auto`` 自动开始探测。
_BACKEND_CLASSES: dict[str, type] = {"null": NullBackend, "off": NullBackend}


def _probe(name: str) -> dict:
    """探测单个后端是否可用；**未登记即不可用**（诚实，不假装可用）。"""
    cls = _BACKEND_CLASSES.get(name)
    if cls is None:
        return {"available": False, "reason": f"后端 {name!r} 尚未实现（P1/P4）"}
    try:
        info = cls().capability_probe()          # type: ignore[call-arg]
        return dict(info or {"available": False, "reason": "探测无返回"})
    except Exception as exc:                      # noqa: BLE001 —— 探测失败=不可用
        return {"available": False, "reason": f"探测异常: {type(exc).__name__}"}


def select_backend(requested: str = "auto", *, strict: bool = False
                   ) -> tuple[ConfinementBackend, dict]:
    """按配置选后端，返回 ``(backend, info)``。

    ``info`` = ``{requested, backend, strength, enforced_by, reason, fell_back, error}``。

    降级语义（v1.2 用户裁定 + strict 默认 false）：
      - ``auto``：按 ``_PROCESS_CANDIDATES`` 顺序探测，都不可用 ⇒ 回落 NullBackend(*off*)
        —— **不报错**（进程级运行时不满足时 Sandbox Runtime 默认 off）。
      - 显式点名某后端但不可得 ⇒ ``fell_back=True`` + ``error=True`` +
        ``enforced_by="backend-error"``；``strict=True`` 时额外抛 ``BackendUnavailable``。

    ★ **诚实边界（代码复核所得）**：``strict=True`` 只抛异常 + 计数，**不等于拒绝 spawn**。
    ``CHILD_SPAWN_OPTIONS`` 接缝的返回值只有"给不给落点"两态，没有"拒绝"通道，
    桥侧拿到 ``None`` 仍会照常起进程。真正的 spawn 级拒绝需桥配合（P1+ 待办）。
    """
    req = str(requested or "auto").strip().lower() or "auto"
    if req not in BACKENDS:
        req = "auto"

    def _info(backend: ConfinementBackend, reason: str, *, fell_back: bool,
              error: bool) -> dict:
        return {"requested": requested, "backend": backend.name,
                "strength": backend.strength,
                "enforced_by": "backend-error" if error else backend.name,
                "reason": reason, "fell_back": fell_back, "error": error}

    # ---- off / null：一等档位，直接给，不探测、不算降级。
    if req in ("off", "null"):
        return NullBackend(), _info(NullBackend(), "显式非沙箱档位", fell_back=False, error=False)

    # ---- auto：按序探测进程级候选
    if req == "auto":
        reasons = []
        for cand in _PROCESS_CANDIDATES:
            info = _probe(cand)
            if info.get("available"):
                return _BACKEND_CLASSES[cand](), _info(          # type: ignore[call-arg]
                    _BACKEND_CLASSES[cand](),                    # type: ignore[call-arg]
                    f"auto 选中 {cand}", fell_back=False, error=False)
            reasons.append(f"{cand}={info.get('reason')}")
        nb = NullBackend()
        return nb, _info(nb, "auto 无可用的进程级后端 ⇒ Sandbox Runtime off（"
                             + "; ".join(reasons) + "）", fell_back=False, error=False)

    # ---- 显式点名（wsl2 / win-token / container / cloud）
    info = _probe(req)
    if info.get("available"):
        cls = _BACKEND_CLASSES[req]
        inst = cls()                                            # type: ignore[call-arg]
        return inst, _info(inst, f"显式后端 {req} 可用", fell_back=False, error=False)

    # 不可得 ⇒ 回落 + 可见；strict ⇒ 额外抛（fail-closed 的**告警侧**）
    nb = NullBackend()
    result = _info(nb, f"显式要求后端 {req!r} 但不可得：{info.get('reason')}",
                   fell_back=True, error=True)
    if strict:
        raise BackendUnavailable(result["reason"])
    return nb, result


def describe_backend(requested: str = "auto", *, strict: bool = False) -> dict:
    """``select_backend`` 的**纯信息**包装（供 ``status()``/审计；不抛异常）。

    ★ 与 ``select_backend`` 分离的理由：``status()`` 在装配/诊断路径上跑，
    抛异常会让插件页拿不到状态；故这里**吞掉** strict 异常但保留 ``error`` 标记。
    """
    try:
        backend, info = select_backend(requested, strict=strict)
    except BackendUnavailable as exc:
        return {"requested": requested, "backend": "null", "strength": "none",
                "enforced_by": "backend-error", "reason": str(exc),
                "fell_back": True, "error": True}
    info["gaps"] = list(backend.describe().get("gaps") or [])
    return info
