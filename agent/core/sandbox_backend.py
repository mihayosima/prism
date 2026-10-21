"""沙箱 confinement 后端骨架。

设计依据 ``docs/sandbox/分级沙箱对齐ClaudeCode方案-.md`` v1.2。

为什么落 ``core``：后端要被插件/传输层构造，而 ``agent/api.py``
不得 import ``agent.sys`` ⇒ 任何需被插件取用的对象必须能被 api 导出 ⇒ 只能落 core。
本模块零依赖，与 ``SkillsCatalog`` 下沉同一纪律。

本模块只定义"机制契约"，不做平台判定：
真隔离属 P1/P4，本轮只出 ``NullBackend``。``select_backend`` 对尚未实现的后端如实报不可用并回落，
绝不谎报已隔离。

契约形状：
    ``Services.CHILD_SPAWN_OPTIONS`` 的真实形状是 ``(session_key) -> {"cwd":…, "env":…} | None``，
    桥只消费 ``cwd`` / ``env`` 两个键。
    P8 起 provider 可接受第二个可选参数 ``owner``用于按桥降级；
    桥侧统一经 ``call_spawn_provider()`` 调用。
    ⇒ 本模块产出的 ``SpawnOptions`` 是内核内部类型，由插件侧 ``to_dict()`` 成字典后
    经接缝返回；新增键对既有桥向后兼容，但不得把接缝改成返回
    dataclass —— 那会直接改崩桥。
"""

from __future__ import annotations

import inspect
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

__all__ = [
    "STRENGTHS", "BACKENDS", "NET_POLICIES",
    "SpawnOptions", "ConfinementBackend", "NullBackend", "Wsl2Backend",
    "ContainerBackend",
    "BackendUnavailable", "select_backend", "describe_backend",
    "apply_wrap", "to_wsl_path",
    # 目标可移植性
    "BRIDGE_PORTABILITY", "runnable_in", "is_portable", "unportable_bridges",
    "call_spawn_provider",
]

# : 强度三档：实际做到了多少。
STRENGTHS = ("none", "partial", "full")

# : 后端枚举。``auto`` = 按平台能力探测；``off``/``null`` = 一等"非沙箱"档。
BACKENDS = ("auto", "off", "null", "win-token", "wsl2", "container", "cloud")

# : 网络限制轴。
NET_POLICIES = ("allow", "deny", "proxy")

# --------------------------------------------------------------------------- #
# 目标可移植性 —— 桥能不能在给定 backend 下运行
# --------------------------------------------------------------------------- #
# 隔离后端意味着子进程不在原生 Windows 上跑。但有些桥按设计
# 就只能跑在原生 Windows 上，
# 把它塞进 WSL/容器不是"隔离"而是"必崩"。这类目标必须：
# ① 明确声明可运行的 backend；② 在选中不可运行的 backend 时降级为 off；
# ③ 在 status/审计里可见，不能悄悄无隔离。
# 键 = 桥 id。
# : 值 = 该桥可运行的后端集合。
BRIDGE_PORTABILITY: dict[str, tuple[str, ...]] = {
    "skills_mcp": ("off", "wsl2", "container"),
    "playwright_mcp": ("off", "wsl2", "container"),
    # Windows 原生 UI 自动化：跨不了边界。这是 MCP 自身的设计约束，
    # 不是沙箱的失职 —— 诚实暴露即可。
    "windows_mcp": ("off",),
}

# : 未登记的桥按"可移植"处理。
_DEFAULT_PORTABILITY: tuple[str, ...] = ("off", "wsl2", "container")


def runnable_in(bridge: str) -> tuple[str, ...]:
    """该桥声明能在哪些 backend 下运行。

        两种写法都接受：桥 id与工具名里的域。审计按工具名推域，桥侧按 id 自称 ⇒ 必须都认，否则
        ``is_portable("windows", …)`` 会落到默认值、把不可移植目标误判为可移植。
        """
    b = str(bridge or "").strip()
    if b in BRIDGE_PORTABILITY:
        return BRIDGE_PORTABILITY[b]
    if f"{b}_mcp" in BRIDGE_PORTABILITY:
        return BRIDGE_PORTABILITY[f"{b}_mcp"]
    return _DEFAULT_PORTABILITY


def is_portable(bridge: str, backend: str) -> bool:
    """该桥能否在 ``backend`` 下运行。``off``/``null`` 恒 True。"""
    be = str(backend or "null").strip().lower()
    if be in ("off", "null", ""):
        return True
    return be in runnable_in(bridge)


def unportable_bridges(backend: str,
                       bridges: Any = None) -> list[str]:
    """在 ``backend`` 下跑不了的桥。"""
    names = list(bridges) if bridges is not None else list(BRIDGE_PORTABILITY)
    return [b for b in names if not is_portable(b, backend)]


def call_spawn_provider(fn: Callable[..., Any], session_key: str,
                        owner: str = "") -> dict | None:
    """以可选 owner 调用 ``CHILD_SPAWN_OPTIONS`` 接缝。

        为什么需要 owner：接缝按 session 给落点，但可移植性是按桥判定的。桥把自己的 id 传进来，
        提供方才能为该桥单独降级。

        向后兼容：历史/第三方 provider 可能只接受单参 ⇒ 用签名元数判定，
        单参就单参调，扩容不破坏既有 provider。
        """
    if not callable(fn):
        return None
    try:
        arity = len(inspect.signature(fn).parameters)
    except (TypeError, ValueError):       # 内建/奇怪可调用对象 ⇒ 保守按单参
        arity = 1
    return fn(session_key, owner) if arity >= 2 else fn(session_key)


class BackendUnavailable(RuntimeError):
    """显式要求的后端在本机不可得。

     语义边界：抛出只代表"要不到隔离"，不代表调用被拒绝。真正的"拒绝 spawn"
     需要桥侧配合，本轮
     ``strict`` 的落点是可见告警 + 计数 + 回落，而非硬阻断。
     """


@dataclass
class SpawnOptions:
    """子进程启动选项。

     向后兼容纪律：``to_dict()`` 必须至少含 ``cwd`` / ``env`` 两键，
     因为既有桥只读这两个。其余键是增强信息：
     今天无消费者，P1/P4/P7 才用得上，
     提前把字段定义好是为了让后端实现不必再改接缝形状。
     """

    # : 落点收拢：子进程工作目录。
    cwd: str | None = None
    # : 需覆盖的环境变量。
    env: dict[str, str] = field(default_factory=dict)
    # : 声明的文件限制。
    mode: str = "none"
    # : 实际生效的后端名。
    backend: str = "null"
    # : 实际强度——诚实值，不由配置反推。
    strength: str = "none"
    # : 谁在强制。
    enforced_by: str = "none"
    # : 网络限制。
    net_policy: str = "allow"
    # : 代理地址。
    proxy_addr: str | None = None
    # : 是否进独立网络命名空间。
    netns: bool = False
    # : Windows 受限令牌句柄。
    token: int | None = None
    # : Windows 进程创建标志。
    creationflags: int | None = None
    # : POSIX preexec 闭包。
    preexec: Callable[[], None] | None = None
    # : Linux seccomp filter fd。
    seccomp_fd: int | None = None
    # : 命令包装：隔离要作用在命令本身，光靠 cwd/env 不够。
    # : ``wrap_mode="shell"`` 时最终 argv = ``wrap_argv + [wrap_inner + " " + env 前缀 + 转义后的原命令]``。
    wrap_mode: str = "none"                 # "none" | "shell"
    # : 外层 argv，如 ``("wsl.exe", "-e", "bash", "-lc")``。
    wrap_argv: tuple[str, ...] = ()
    # : inner 命令前缀，与原命令拼接后作为外层最后一个参数。
    wrap_inner: str = ""
    # : 命令翻译表：
    # : ``[{"match_args": ["-m", "agent.plugins.skills.adapter"], "exe": "python3"}]``
    # : —— Windows 侧的可执行文件在 WSL/容器内不存在，必须换成目标环境里的命令。
    translations: tuple[dict, ...] = ()
    # : P5：容器内路径映射 ``[[宿主要替换前缀, 容器内前缀], ...]``。
    # : 环境变量里的 Windows 绝对路径在容器内无效，
    # : 必须按挂载点改写；``apply_wrap`` 消费。
    path_map: tuple[tuple[str, str], ...] = ()
    # : P5：``wrap_mode="argv"`` 时，镜像名必须排在 env 之后、命令之前
    # : ⇒ 单独存，由 apply_wrap 组装。
    image: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转成接缝字典。

         只输出非空的可选字段，避免把一堆 ``None`` 塞给今天不消费它们的桥。
         ``cwd`` / ``env`` / ``mode`` 恒在。
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
        # preexec 是闭包，不可序列化 ⇒ 不入字典
        if self.wrap_mode != "none" and self.wrap_argv:
            # 新增键：既有桥不读，只有显式支持的桥才传给它自己的客户端。
            wrap: dict[str, Any] = {"mode": self.wrap_mode, "argv": list(self.wrap_argv),
                                    "inner": self.wrap_inner,
                                    "translations": [dict(t) for t in self.translations]}
            if self.path_map:
                wrap["path_map"] = [list(p) for p in self.path_map]
            if self.image:
                wrap["image"] = self.image
            out["wrap"] = wrap
        return out


class ConfinementBackend(Protocol):
    """隔离后端协议。

         实现者只回答两件事：能不能与怎么做。不判准入——那是 guard/approval 的活。
         """

    # : 后端名。
    name: str
    # : 该后端实际能达到的强度。
    strength: str

    def capability_probe(self) -> dict:
        """探测本机/本平台能否启用：``{"available": bool, "reason": str}``。"""
        ...

    def can_enforce(self, mode: str) -> bool:
        """该后端能否兑现 ``mode`` 这一文件限制。"""
        ...

    def spawn_options(self, base: SpawnOptions, policy: dict,
                      session_key: str = "") -> SpawnOptions:
        """在落点收拢之上叠加本后端的隔离信息。

         约定：不改 ``base.cwd``/``base.env``，
         只填 ``backend``/``strength``/``enforced_by`` 与平台相关字段。
         """
        ...

    def describe(self) -> dict:
        """自述：``{name, strength, available, reason, gaps}``。"""
        ...


class NullBackend:
    """非沙箱后端——诚实 ``none``。

     它是档位不是"失败"：CC 日常执行即此态。保留落点收拢，
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


# ----------------------"非沙箱"------------------------------------------------ #
# WSL2 进程级后端
# --------------------------------------------------------------------------- #

# : win-token 的出局结论。保留枚举以兼容配置，
# : 但恒不可用且给出可追溯的原因，避免日后重复踩坑。
WIN_TOKEN_VERDICT = (
    "win-token 不可用（P1 spike 实测）：非管理员下 CreateRestrictedToken 能成功，"
    "但施加任何 restricting SID 后子进程必崩 0xC0000142(STATUS_DLL_INIT_FAILED)"
    "（换系统目录 cmd.exe 同样崩）；不施加则毫无隔离。绕法需放宽系统目录 ACE=安全倒退，不做"
)

#"；不施加则毫无隔离。绕法需放宽系统目录 ACE=安全倒退，不做"译成目标环境里的命令。
# : 这是数据，翻译动作由 ``apply_wrap`` 执行。
#: ：技能 MCP 适配器已归位 ``plugins/skills_mcp/adapter.py``——翻译表跟着模块路径走，桥的 spawn 命令不变。
_WSL2_TRANSLATIONS: tuple[dict, ...] = (
    {"match_args": ["-m", "agent.plugins.skills.adapter"], "exe": "python3"},
)

# : 探测结果缓存。
_WSL2_PROBE_CACHE: dict | None = None


def to_wsl_path(p: str) -> str:
    """Windows 路径 → WSL 挂载路径：``C:\\a\\b`` ⇒ ``/mnt/c/a/b``。

     非盘符路径原样返回。
     """
    s = str(p or "").replace("\\", "/")
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        return f"/mnt/{s[0].lower()}{s[2:]}"
    return s


def _looks_win_abs(v: str) -> bool:
    """值是否像 Windows 绝对路径。"""
    s = str(v or "")
    return len(s) >= 3 and s[0].isalpha() and s[1] == ":" and s[2] in "\\/"


def _bwrap_inner(*, repo_wsl: str, cwd_wsl: str | None, writable_wsl: str | None,
                 netns: bool) -> str:
    """拼 bwrap 命令串。"""
    parts = [
        "bwrap",
        "--ro-bind /usr /usr", "--ro-bind /bin /bin", "--ro-bind /lib /lib",
        "--ro-bind /lib64 /lib64", "--ro-bind /etc /etc",
        "--tmpfs /tmp", "--proc /proc", "--dev /dev",
        f"--ro-bind {repo_wsl} {repo_wsl}",     # 项目根只读：代码可读、不可改
        # 子进程 cwd 是落点，``python -m agent.run.xxx`` 靠这个 import；
        # 由包装命令自己保证，不依赖调用方有没有把 PYTHONPATH 并进 env。
        f"--setenv PYTHONPATH {repo_wsl}",
    ]
    if writable_wsl:                            # workspace-write ⇒ 仅工作目录可写
        parts.append(f"--bind {writable_wsl} {writable_wsl}")
    if cwd_wsl:
        parts.append(f"--chdir {cwd_wsl}")
    if netns:
        parts.append("--unshare-net")           # 内核级断网
    parts.append("--unshare-pid --die-with-parent")
    return " ".join(parts)


class Wsl2Backend:
    """WSL2 内 Linux 进程级隔离。

         与 Claude Code 同构：CC 的进程级沙箱在 Windows 上同样借 WSL2 的 Linux 原语；本仓 Windows 原生进程级
         经 P1 spike 实测不可用。

         诚实强度：partial——bwrap 提供 mount/pid/net 命名空间与只读绑定，
         但本轮未加载 seccomp BPF、也未写 Landlock 规则，故不宣称 full。
         """

    name = "wsl2"
    # : 诚实值：namespace + mount 隔离 + 内核断网，但无 seccomp/Landlock 细粒度规则。
    strength = "partial"

    def __init__(self, probe_result: dict | None = None) -> None:
        """``probe_result`` 用于注入探测结果，生产走真实探测。"""
        self._probe_override = probe_result

    def capability_probe(self) -> dict:
        if self._probe_override is not None:
            return dict(self._probe_override)
        return _wsl2_probe()

    def can_enforce(self, mode: str) -> bool:
        return str(mode or "none") in ("read-only", "workspace-write")

    def spawn_options(self, base: SpawnOptions, policy: dict,
                      session_key: str = "") -> SpawnOptions:
        mode = str(policy.get("mode") or base.mode or "none")
        net = str(policy.get("network") or "allow")
        cwd_wsl = to_wsl_path(base.cwd) if base.cwd else None
        repo_wsl = to_wsl_path(os.getcwd())
        writable = cwd_wsl if mode == "workspace-write" else None
        netns = net == "deny"                 # proxy 档走应用层代理，netns 内连不回宿主代理
        # 环境变量里的 Windows 绝对路径在 WSL 内无效
        # ⇒ 一并映射；非路径值原样保留。
        base.env = {k: (to_wsl_path(v) if _looks_win_abs(v) else v)
                    for k, v in (base.env or {}).items()}
        # 路径映射之后再补 PYTHONPATH：子进程以落点为 cwd，
        # 而 ``python -m agent.run.xxx`` 必须能从项目根 import —— 少了这行就是 exit 1。
        paths = [p for p in str(base.env.get("PYTHONPATH") or "").split(os.pathsep) if p]
        if repo_wsl not in paths:
            base.env["PYTHONPATH"] = os.pathsep.join([repo_wsl] + paths)
        base.mode = mode
        base.backend = self.name
        base.strength = self.strength
        base.enforced_by = "wsl2-bwrap"
        base.net_policy = net
        base.netns = netns
        base.wrap_mode = "shell"
        base.wrap_argv = ("wsl.exe", "-e", "bash", "-lc")
        base.wrap_inner = _bwrap_inner(repo_wsl=repo_wsl, cwd_wsl=cwd_wsl,
                                       writable_wsl=writable, netns=netns)
        base.translations = _WSL2_TRANSLATIONS
        return base

    def describe(self) -> dict:
        return {
            "name": self.name, "strength": self.strength, "available": True,
            "reason": "WSL2 内 bwrap（namespace+mount+netns）",
            "gaps": [
                "未加载 seccomp BPF（仅拦 openat 的热点过滤尚未实现）",
                "未写 Landlock 规则（文件系统白名单靠 bwrap 只读绑定近似）",
                "network=proxy 仅应用层（netns 内无法回连宿主代理，裸 socket 可绕）",
            ],
        }


#: 容器档默认镜像，须含 ``python3`` 以运行本仓 stdio 适配器。
DEFAULT_CONTAINER_IMAGE = "python:3.12-alpine"

# : 探测结果缓存。
_CONTAINER_PROBE_CACHE: dict | None = None

# : 容器内命令翻译。
_CONTAINER_TRANSLATIONS: tuple[dict, ...] = (
    {"match_args": ["-m"], "exe": "python3"},
)


class ContainerBackend:
    """Docker 容器级隔离。

     这是本仓强度最高的一档：文件、
     网络、进程/权限三个维度都能兑现，故诚实强度为 ``full``。

     代价：依赖引擎与镜像可得；容器内默认以镜像自带用户
     运行；每次 spawn 起容器
     比 WSL2 进程级重。
     """

    name = "container"
    # : 三个维度均可兑现 ⇒ full；剩余缺口见 ``describe()["gaps"]``。
    strength = "full"

    def __init__(self, probe_result: dict | None = None) -> None:
        self._probe_override = probe_result

    def capability_probe(self) -> dict:
        if self._probe_override is not None:
            return dict(self._probe_override)
        return _container_probe()

    def can_enforce(self, mode: str) -> bool:
        return str(mode or "none") in ("read-only", "workspace-write")

    def spawn_options(self, base: SpawnOptions, policy: dict,
                      session_key: str = "") -> SpawnOptions:
        mode = str(policy.get("mode") or base.mode or "none")
        net = str(policy.get("network") or "allow")
        image = str(policy.get("image") or DEFAULT_CONTAINER_IMAGE)
        repo = os.getcwd()
        argv: list[str] = [
            "docker", "run",
            "-i",                                # 保 stdio：MCP 桥靠 stdin/stdout 通信
            "--rm",                              # 用完即删，不留残留容器
            "--cap-drop", "ALL",                 # 去掉全部 capabilities
            "--security-opt", "no-new-privileges",
            "--pids-limit", "256",               # 防 fork 炸弹拖垮宿主
        ]
        if net == "deny":
            argv += ["--network", "none"]        # 内核级无网：CC 容器档同款，无 net_gap
        if mode == "read-only":
            argv += ["--read-only", "--tmpfs", "/tmp"]
        # 项目根只读挂载 ⇒ 代码可读、不可改
        argv += ["-v", f"{repo}:/repo:ro"]
        if mode == "workspace-write" and base.cwd:
            argv += ["-v", f"{base.cwd}:/workspace:rw"]
            argv += ["-w", "/workspace"]
        else:
            argv += ["-w", "/repo"]
        base.env = {k: v for k, v in (base.env or {}).items()}
        base.mode = mode
        base.backend = self.name
        base.strength = self.strength
        base.enforced_by = "docker-container"
        base.net_policy = net
        base.netns = net == "deny"               # 容器内即独立 netns
        base.wrap_mode = "argv"
        base.wrap_argv = tuple(argv)
        base.wrap_inner = ""
        base.translations = _CONTAINER_TRANSLATIONS
        base.path_map = ((repo, "/repo"),)
        base.image = image
        return base

    def describe(self) -> dict:
        return {
            "name": self.name, "strength": self.strength, "available": True,
            "reason": "Docker 容器（--network none / --cap-drop ALL / 只读挂载）",
            "gaps": [
                f"依赖引擎与镜像可得（默认镜像 {DEFAULT_CONTAINER_IMAGE}，缺失需 pull）",
                "容器内用户由镜像决定（alpine 官方镜像为 root）——与宿主隔离，但非最小权限",
                "network=proxy 未实现（本档要么 --network none 全断，要么 allow 全放）",
                "启动比 WSL2 进程级重（每次 spawn 一个容器）",
            ],
        }


def _container_probe() -> dict:
    """真实探测：Docker 引擎可达且能跑容器。"""
    global _CONTAINER_PROBE_CACHE
    if _CONTAINER_PROBE_CACHE is not None:
        return dict(_CONTAINER_PROBE_CACHE)
    info = _container_probe_uncached()
    _CONTAINER_PROBE_CACHE = info
    return dict(info)


def _container_probe_uncached() -> dict:
    try:
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, timeout=45, text=True,
                           encoding="mbcs", errors="replace")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"available": False, "reason": f"docker 不可用：{type(exc).__name__}"}
    if r.returncode != 0:
        return {"available": False,
                "reason": f"docker 引擎未就绪（rc={r.returncode}）："
                          f"{(r.stderr or '').strip()[:120]}"}
    version = (r.stdout or "").strip()
    return {"available": True, "reason": f"Docker {version} 可用", "version": version}


def _wsl2_probe() -> dict:
    """真实探测：Windows 宿主 + 有 Running 的发行版 + WSL 内有 bwrap。"""
    global _WSL2_PROBE_CACHE
    if _WSL2_PROBE_CACHE is not None:
        return dict(_WSL2_PROBE_CACHE)
    info = _wsl2_probe_uncached()
    _WSL2_PROBE_CACHE = dict(info)
    return info


def _wsl2_probe_uncached() -> dict:
    if os.name != "nt":
        return {"available": False, "reason": "wsl2 backend 仅在 Windows 宿主上有意义"}
    try:
        r = subprocess.run(["wsl.exe", "-l", "-v"], capture_output=True, timeout=90,
                           text=True, encoding="mbcs", errors="replace")
    except Exception as exc:                                      # noqa: BLE001
        return {"available": False, "reason": f"wsl.exe 不可达: {type(exc).__name__}"}
    if r.returncode != 0:
        return {"available": False, "reason": f"wsl -l -v 失败 rc={r.returncode}"}
    distro = ""
    for line in (r.stdout or "").replace("\x00", "").splitlines():
        parts = line.replace("*", " ").split()
        if len(parts) >= 2 and parts[1] == "Running" and parts[0].upper() != "NAME":
            distro = parts[0]
            break
    if not distro:
        return {"available": False, "reason": "无 Running 状态的 WSL 发行版"}
    try:
        r2 = subprocess.run(["wsl.exe", "-e", "bash", "-lc", "command -v bwrap"],
                            capture_output=True, timeout=90, text=True,
                            encoding="mbcs", errors="replace")
    except Exception as exc:                                      # noqa: BLE001
        return {"available": False, "reason": f"WSL 内探测 bwrap 异常: {type(exc).__name__}"}
    if "bwrap" not in (r2.stdout or ""):
        return {"available": False, "reason": f"WSL 发行版 {distro} 内缺少 bwrap"}
    return {"available": True, "reason": f"wsl2 可用（{distro} + bwrap）", "distro": distro}


def _contains_subseq(seq: list[str], sub: list[str]) -> bool:
    """``sub`` 是否为 ``seq`` 的连续子序列。"""
    n, m = len(seq), len(sub)
    return any(seq[i:i + m] == list(sub) for i in range(max(n - m + 1, 0)))


def apply_wrap(command: str, args: list[str] | tuple[str, ...] | None = None,
               env: dict[str, str] | None = None, cwd: str | None = None,
               wrap: dict | None = None
               ) -> tuple[str, list[str], dict[str, str], str | None, bool]:
    """把包装配置应用到一条待启动命令上。

         返回 ``(command, args, env, cwd, applied)``。

         ``applied=False`` 时调用方必须原样启动——硬套包装会必崩。此时该子进程退化为
         无隔离运行，属诚实降级，不是静默：调用方应记日志/审计。
         """
    argv_in = list(args or [])
    if not wrap:                        # 顺序别动：先判空再取键
        return command, argv_in, dict(env or {}), cwd, False
    mode = str(wrap.get("mode") or "")
    if mode not in ("shell", "argv") or not wrap.get("argv"):
        return command, argv_in, dict(env or {}), cwd, False
    exe, hit = command, False
    for t in (wrap.get("translations") or []):
        match = [str(x) for x in (t.get("match_args") or [])]
        if match and _contains_subseq(argv_in, match):
            exe = str(t.get("exe") or command)
            hit = True
            break
    if not hit:
        return command, argv_in, dict(env or {}), cwd, False
    outer = [str(x) for x in wrap["argv"]]
    if mode == "argv":
        # P5 容器档：``docker run [flags] -e K=V … <image> <cmd>``
        # 顺序铁律：``-e`` 必须在 image 之前；
        # 环境变量值若含宿主绝对路径，要先按挂载点映射。
        for k, v in (env or {}).items():
            outer += ["-e", f"{k}={_map_path(v, wrap)}"]
        if wrap.get("image"):
            outer.append(str(wrap["image"]))
        return outer[0], outer[1:] + [exe] + argv_in, dict(env or {}), cwd, True
    # ---- shell 档
    # 两个易错点：
    # ① env 赋值必须在 bwrap 之前；放到 bwrap 参数之后会被
    # bwrap 当成要 exec 的程序名。
    # ② 桥给的 env 值是 Windows 路径，WSL 内无效 ⇒ 必须映射。
    # 放在这里而不是后端里做，因为桥的 env 才是权威来源。
    mapping = to_wsl_path if wrap.get("map_env_paths", True) else (lambda v: str(v))
    prefix = "".join(f"{k}={shlex.quote(mapping(v))} " for k, v in (env or {}).items())
    inner = f"{prefix}{wrap.get('inner') or ''} {shlex.join([exe] + argv_in)}".strip()
    return outer[0], outer[1:] + [inner], dict(env or {}), cwd, True


def _map_path(value: Any, wrap: dict) -> str:
    """按 ``wrap["path_map"]`` 把宿主路径改写成容器内路径。"""
    s = str(value)
    for pair in (wrap.get("path_map") or []):
        try:
            src, dst = str(pair[0]), str(pair[1])
        except (TypeError, IndexError, KeyError):     # noqa: PERF203 —— 坏数据不该炸
            continue
        if not src:
            continue
        for cand in (src, src.replace("\\", "/")):
            if s.lower().startswith(cand.lower()):
                rest = s[len(cand):].lstrip("\\/")
                return f"{dst.rstrip('/')}/{rest}" if rest else dst
    return s


# : auto 的探测顺序：进程级轻量优先；
# : wsl2 不可得时由容器级承接；都不可得 ⇒ off。
_AUTO_CANDIDATES: tuple[str, ...] = ("wsl2", "container")

# : 已实现的后端类表。P0 有 null/off；P4 加入 wsl2；P5 加入 container。
_BACKEND_CLASSES: dict[str, type] = {"null": NullBackend, "off": NullBackend,
                                     "wsl2": Wsl2Backend, "container": ContainerBackend}


def _probe(name: str) -> dict:
    """探测单个后端是否可用；未登记即不可用。"""
    if name == "win-token":
        return {"available": False, "reason": WIN_TOKEN_VERDICT}
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

     降级语义：
     - ``auto``：按 ``_PROCESS_CANDIDATES`` 顺序探测，都不可用 ⇒ 回落 NullBackend(*off*)
     —— 不报错。
     - 显式点名某后端但不可得 ⇒ ``fell_back=True`` + ``error=True`` +
     ``enforced_by="backend-error"``；``strict=True`` 时额外抛 ``BackendUnavailable``。

     诚实边界：``strict=True`` 只抛异常 + 计数，不等于拒绝 spawn。
     ``CHILD_SPAWN_OPTIONS`` 接缝的返回值只有"给不给落点"两态，没有"拒绝"通道，
     桥侧拿到 ``None`` 仍会照常起进程。真正的 spawn 级拒绝需桥配合。
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

    # ---- auto：按序探测备选后端
    if req == "auto":
        reasons = []
        for cand in _AUTO_CANDIDATES:
            info = _probe(cand)
            if info.get("available"):
                return _BACKEND_CLASSES[cand](), _info(          # type: ignore[call-arg]
                    _BACKEND_CLASSES[cand](),                    # type: ignore[call-arg]
                    f"auto 选中 {cand}", fell_back=False, error=False)
            reasons.append(f"{cand}={info.get('reason')}")
        nb = NullBackend()
        return nb, _info(nb, "auto 无可用的进程级后端 ⇒ Sandbox Runtime off（"
                             + "; ".join(reasons) + "）", fell_back=False, error=False)

    # ---- 显式点名
    info = _probe(req)
    if info.get("available"):
        cls = _BACKEND_CLASSES[req]
        inst = cls()                                            # type: ignore[call-arg]
        return inst, _info(inst, f"显式后端 {req} 可用", fell_back=False, error=False)

    # 不可得 ⇒ 回落 + 可见；strict ⇒ 额外抛
    nb = NullBackend()
    result = _info(nb, f"显式要求后端 {req!r} 但不可得：{info.get('reason')}",
                   fell_back=True, error=True)
    if strict:
        raise BackendUnavailable(result["reason"])
    return nb, result


def describe_backend(requested: str = "auto", *, strict: bool = False) -> dict:
    """``select_backend`` 的纯信息包装。

     与 ``select_backend`` 分离的理由：``status()`` 在装配/诊断路径上跑，
     抛异常会让插件页拿不到状态；故这里吞掉 strict 异常但保留 ``error`` 标记。
     """
    try:
        backend, info = select_backend(requested, strict=strict)
    except BackendUnavailable as exc:
        return {"requested": requested, "backend": "null", "strength": "none",
                "enforced_by": "backend-error", "reason": str(exc),
                "fell_back": True, "error": True}
    info["gaps"] = list(backend.describe().get("gaps") or [])
    return info
