"""插件包：sandbox —— 外部工具的隔离档位提供方 + 隔离自述。

定位
----------------------------------------
本插件不再做准入判定。它只回答一个问题：

 这次外部工具调用，声明在什么隔离档位下跑、实际强度是多少。

三件套分工：

 ========== ================================== ==========================
 插件 判什么 报什么
 ========== ================================== ==========================
 ``sys.guard`` 谁能调什么 策略矩阵
 ``plugins.approval`` 唯一真问人 审批结果
 ``plugins.sandbox`` 不判准入 隔离档位与强度
 ========== ================================== ==========================

为什么要转：此前本插件同时持有一份 allow/deny/risk 名单，与 ``sys/guard.py``
功能重叠 —— 两份配置并存、互相打洞、无单一真相。核定过程见
``docs/sandbox/沙箱与权限管理器重叠核定-.md``。
名字匹配与域限定已迁入 guard。

两根正交的轴
----------------------------------------
- ``mode`` = 承诺限制到什么
- ``enforcement`` = 实际做到了多少

例：Windows 受限 token 能兑现 ``workspace-write`` 的写限制，但读/网络不隔离
⇒ ``mode=workspace-write`` + ``enforcement=partial``。这不是"低一档"，
而是"同一语义、不同强度"。对齐 DeepSeek Harness 的 ``SandboxMode`` / ``SandboxEnforcement``。

现状：无任何进程级隔离实现 ⇒ 所有档位的实际强度都是 ``none``。
这不是缺陷陈述，是事实陈述 —— 把它报成 ``full`` 才是缺陷。
插件页此前显示 ``enabled · assembled`` + ``error: null`` 看起来健康，是本项目最大的误导源之一。

fail-closed 纪律
----------------------------------------
- 无可��后端时不静默降级：``enforcement`` 如实为 ``none``，并有 ``gap`` 标记落差。
- ``enforced_by`` 区分"沙箱正常评估"与"沙箱自身故障"——
 对应 DSH 的 ``denialSignatures`` vs ``runnerFailureRules``。不把基础设施故障误报成策略结果，
 否则会触发本项目既有的 hang 自愈把工具故障当策略故障重启桥。

配置
----------------------------------------
容器级 ``config["agent"]["sandbox"]``，``core/config_schema.py`` 登记逐键严格 schema，
否则写了配置就装配失败；会话级覆盖走既有 ``plugins.sandbox`` 节
：

 mode 声明的隔离档位：none/ read-only / workspace-write
 targets 受本插件管辖的工具名模式
 default/allow/deny/risk 已废弃

四键为何不删：`sandbox` 节是严格键集，删键会让写了这些键的用户配置在装配期
抛 ``ConfigError`` ⇒ 服务起不来。故保留，并由 ``describe()`` 检出
``deprecated_used`` 并计数 —— 迁移期可见，不静默。

模式语法：仅支持尾部 ``*``，其余按精确名。刻意不用正则：
这份配置是运维手写的，语义越简单越不会错配。

waterfall 位置
----------------------------------------
挂 ``agent/tasks/pre-execute`` 且 ``prepend=True``。但 ``core/events.py`` 的 ``prepend``
是 ``insert(0, …)`` ⇒ 后注册者排前面。装配序是 ``profiles.py:137`` 先装
``SYS_PLUGINS``、``:145`` 再装目录插件⇒ 实际顺序 sandbox → guard → approval。
本插件恒放行，所以顺序不再影响裁决结果——这正是转型顺带消解掉的一处隐患。

审计
----------------------------------------
每次调用落 ``agent/sandbox/confinement``。
载荷含 ``mode``/``enforcement``/``ceiling``/``gap``/``enforced_by``/``bridge``/
``portable``/``session``。
事件名由 ``agent/sandbox/decision`` 改名而来：留着 "decision" 会让读者误以为它仍在做准入决策。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

from agent.api import (SESSION_CFG_VIEW, Events, Services, current_session_key,
                       durable, inc_counter, now_iso, plugin)
# P0 分级沙箱后端：enforcement 改由后端真实强度驱动，
# 不再硬编码"所有档位恒 none"的天花板。
from agent.api import (BackendUnavailable, NullBackend, SpawnOptions,
                       describe_backend, is_portable, select_backend,
                       unportable_bridges)

logger = logging.getLogger("agent.plugins.sandbox")

# 兜底档位 = ``none``。
# 与 guard 的 ``DEFAULT_POLICY["targets"]`` 同源。
DEFAULT_POLICY: dict = {
    "mode": "none",        # 声明的隔离档位
    "targets": ["mcp.*"],  # 受本插件管辖的工具名模式，默认只管 MCP
    # ---- 以下四键已废弃：准入判定归 guard，问人归 approval。
    # 保留是为了向后兼容既有配置文件——`core/config_schema.py` 的 `sandbox` 节是严格键集，
    # 直接删键会让写了这些键的用户配置在装配期抛 ``ConfigError`` ⇒ 服务起不来。
    # 它们不再有任何裁决效果，但会被 ``describe()`` 检出并计数。
    "default": "allow",
    "allow": [],
    "deny": [],
    "risk": {},
    # ---- P0 新增三键。
    "backend": "auto",   # state 轴：auto|off|null|win-token|wsl2|container|cloud
    "network": {},       # 网络策略；dict，内含 allow_domains 等
    "strict": False,     # true ⇒ 显式要求后端不可得时抛错，默认告警+回落，不打断操作
}

# 插件自管默认。
DEFAULT_CONFIG: dict = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                       for k, v in DEFAULT_POLICY.items()}


def match(pattern: str, target: str) -> bool:
    """工具名模式匹配：仅支持尾部 ``*``，其余按精确名。

     刻意不用正则：这份配置是运维手写的，语义越简单越不会错配。
     """
    p = str(pattern or "").strip()
    if not p:
        return False
    if p == "*":
        return True
    if p.endswith("*"):
        return target.startswith(p[:-1])
    return p == target


def _bridge_of(target: str) -> str:
    """从工具名推出桥名；非 MCP 形态返回空。

     隔离档位是按桥配置的，
     所以审计里必须带桥名才追得到"是哪个桥没隔离"。
     """
    parts = str(target or "").split(".")
    return parts[1] if len(parts) >= 3 and parts[0] == "mcp" else ""


# 落点收拢：把外部子进程的写入落点挪出项目根
# 为什么这一层值得做、以及它不是什么：
# - 现状⇒ 子进程继承宿主 cwd = 项目根，
# 而项目根有 ``local_secrets.json`` 与 ``data/*.db``。
# 叠加 ``child_env`` 会剥掉宿主的安全删除守卫 ⇒ 第三方进程在项目根拥有
# 不受宿主约束的读写删能力。这是 ``enforcement="none"`` 最具体的后果。
# - 本函数把它收拢到 ``<root>/<session>`` 私有目录。
# - 它不是强制：绝对路径仍可写别处，故 ``enforcement`` 不得因此抬成
# partial/full。它消灭的是相对路径误伤面——实践中更高频的一类。
# 落点写进审计⇒ 「东西落在哪」可查。
# - 它还是未来 restricted token 的前置步骤：先有"允许写的那一块"，才指得过去。

# : 重定向环境变量名：
# : 临时目录 + 各语言/工具的缓存家目录。把子进程能"按相对路径写"的地方尽量收进去。
_REDIRECT_ENV_KEYS = (
    "TMP", "TEMP", "TMPDIR",                       # 临时目录
    "XDG_CACHE_HOME", "XDG_DATA_HOME",             # XDG
    "npm_config_cache",                            # npm/npx
    "PLAYWRIGHT_BROWSERS_PATH",                    # Playwright 浏览器下载
    "PIP_CACHE_DIR",                               # pip
    "UV_CACHE_DIR",                                # uv/uvx
)

# : cwd 与重定向的根目录。
_WORKSPACE_ROOT = "data/sandbox"


def _safe_seg(value: str) -> str:
    """会话键 → 目录名。

     两件事都必须成立，缺一不可：

     1. 不能穿越：``core/session.py`` 的字符集是 ``[A-Za-z0-9_.-]``，含 ``.``
     ⇒ 会话键 ``..`` 会直接拼出 ``data/sandbox/..``，逃出根目录。故 ``.`` 与 ``:``
     一律替换掉。
     2. 不能碰撞：净化不可逆，只靠净化会
     让两个不同会话落进同一目录 ⇒ 会话隔离失效。
     ⇒ 追加 8 位哈希：可读前缀 + 哈希，既唯一又安全。
     """
    raw = str(value or "")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:32] or "default"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def workspace_for(policy: dict, session_key: str = "", root: str = _WORKSPACE_ROOT) -> dict | None:
    """按隔离档位算出子进程的落点；``mode="none"`` ⇒ ``None``。

     返回 ``{"cwd": str, "env": {...}}``；``env`` 只含要覆盖的变量。
     纯函数 ⇒ 可穷举单测，且审计侧可复用同一函数算出同一个 cwd。
     """
    mode = str(policy.get("mode") or "none").strip()
    if mode not in MODES or mode == "none":
        return None                      # 未声明隔离 ⇒ 不重定向
    seg = _safe_seg(session_key or "container")
    cwd = f"{root.rstrip('/')}/{seg}"
    return {"cwd": cwd, "env": {k: cwd for k in _REDIRECT_ENV_KEYS}, "mode": mode}



def in_scope(policy: dict, target: str) -> bool:
    """该工具是否在本插件管辖范围内，不在 ⇒ 完全不过问，避免变成"第二个 guard"。"""
    return any(match(p, target) for p in (policy.get("targets") or []))


# 隔离档位与强度
# 两根正交轴：
# mode = 承诺限制到什么
# enforcement = 实际做到了多少
# 例：Windows 受限 token 能做到 workspace-write 的写限制，但读/网络不隔离 ⇒ mode=workspace-write
# + enforcement=partial。这不是"低一档"，而是"同一语义、不同强度"。
MODES = ("none", "read-only", "workspace-write")
ENFORCEMENTS = ("full", "partial", "none")

# enforcement 不再硬编码
# 旧实现在此处放了一张 ``_ENFORCEMENT_CEILING`` 表，把所有 mode 一律映射到 ``none``。
# 那张表是"当前无隔离实现"这一事实的忠实记录，但它把"能力上限"写死在插件里：
# 后端一落地就得改插件，且无法表达 partial。
# 现改由 ``core/sandbox_backend.py`` 的后端自报真实强度驱动：
# - 传了 ``backend_info`` ⇒ enforcement = 后端 strength
# - 没传⇒ 回落 none
_DEPRECATED_KEYS = ("default", "allow", "deny", "risk")


def describe(policy: dict, target: str = "", backend_info: dict | None = None) -> dict:
    """该工具本次调用的隔离自述。

     返回 ``{mode, enforcement, ceiling, backend, enforced_by, gap, reason,
     deprecated_used, backend_error}``：

     - ``mode`` 配置声明的档位
     - ``enforcement`` 实际能达到的强度
     - ``ceiling`` 同上
     - ``backend`` 实际生效的后端名
     - ``enforced_by`` 谁在强制
     - ``gap`` ``mode`` 与 ``enforcement`` 的落差
     - ``backend_error`` 显式要求的后端不可得
     - ``deprecated_used`` 仍在用已废弃的旧裁决键

     宁可 ``enforcement="none"`` 也不谎报 —— 插件页显示"看起来健康"比显示"无隔离"危险。
     """
    mode = str(policy.get("mode") or "none").strip()
    if mode not in MODES:
        mode = "none"
    used = [k for k in _DEPRECATED_KEYS if policy.get(k) not in (None, [], {}, "allow", "open")]
    info = backend_info or {}
    strength = str(info.get("strength") or "none")
    if strength not in ENFORCEMENTS:
        strength = "none"
    # ``mode="none"`` 表示没声明隔离档位 —— 此时后端根本不会被消费
    # ⇒ 报出 ``partial`` 就是谎报。
    # P4 落地 wsl2 后这条才暴露出来：后端"可用"≠"这次调用被隔离了"。
    effective = strength if mode != "none" else "none"
    enforced_by = str(info.get("enforced_by") or "none") if mode != "none" else "none"
    if mode == "none":
        reason = "未配置隔离档位"
    elif strength == "none":
        # 有后端理由就用后端的；没有则给通用说明。
        # 注意 ``or`` 的短路：``info.get("reason")`` 为 None/空串时必须回落模板，
        # 否则 ``str(None)`` 会变成字面量 "None" 混进审计理由里。
        # 措辞沿用旧实现的三条关键信息，
        # 既有诚实性单测据此断言"理由能解释为什么没隔离"，改措辞会破契约。
        reason = (str(info.get("reason")) if info.get("reason") else
                  f"档位 {mode!r} 已声明，但本插件**未实现任何进程级隔离**"
                  f"（无可用后端兑现）⇒ 实际无隔离")
    else:
        reason = f"档位 {mode!r} 生效（强度 {strength}，后端 {enforced_by}）"
    return {"mode": mode, "enforcement": effective, "ceiling": effective,
            "backend": str(info.get("backend") or "null") if mode != "none" else "null",
            "enforced_by": enforced_by,
            "gap": mode != "none" and effective == "none",
            "backend_error": bool(info.get("error")),
            "reason": reason, "deprecated_used": used}


def _config(ctx) -> dict:
    """生效配置 = 插件默认 ← 容器级 ← 会话级。

     ``SESSION_CFG_VIEW`` 接缝是可调用对象 ``(section, sk) -> dict``，调用它就已
     拿到解包后的插件节⇒ 不要再套
     一次 ``section_of``。写成字典用法会抛 ``AttributeError``，且因为本函数在每个任务
     调用前都跑，等于整个插件把工具调用全打断。此处与 ``sys/budget.py`` 的读法同构。
     """
    merged: dict = {**DEFAULT_CONFIG}
    container = ((ctx.config or {}).get("agent") or {}).get("sandbox") or {}
    merged.update({k: v for k, v in container.items() if v is not None})
    sk = current_session_key(ctx)
    if not sk:
        return merged
    try:
        view = ctx.get(SESSION_CFG_VIEW)
        sess = view("sandbox", sk) if callable(view) else {}
    except Exception:                       # noqa: BLE001 —— 视图不可用即回落容器级
        return merged
    merged.update({k: v for k, v in (sess or {}).items() if v is not None})
    return merged


@plugin
class Sandbox:
    """外部工具隔离档位提供方。

         转型：
         本插件曾同时做"策略判定"与"审计"，而策略判定与 ``sys/guard.py`` 功能重叠。现按下述分工收敛：

         ========== ================================== ==========================
         插件 判什么 不判什么
         ========== ================================== ==========================
         guard 谁能调什么 不管隔离强度
         approval 唯一真问人 不判 allow/deny
         sandbox 在什么隔离档位下跑 / 强度多少 不判 allow/deny
         ========== ================================== ==========================

         因此本插件永远放行，只落一条 ``agent/sandbox/confinement``
         审计。"未实现隔离"这件事现在是可见的一等事实，
         而不是"看起来健康"——后者是插件页此前最大的误导源。
         """

    name = "plugins.sandbox"
    layer = "system"
    provides = []
    inject = [Services.EVENTS, Services.DB]

    async def apply(self, ctx) -> None:
        # 运行时观测到被降级为 off 的桥。status() 会把它与
        # 静态可移植性表合并后暴露 ⇒ "哪个桥实际没隔离"可见，不静默。
        self._unportable = set()
        # 落点接缝：提供能力，不替调用方决定。MCP 桥装配时取本函数算 cwd/env；
        # 沙箱未装配 ⇒ 桥侧 ``ctx.get`` 返回 None ⇒ 行为与今天完全一致。
        def _workspace(session_key: str = "", owner: str = "") -> dict | None:
            # 修缺陷：此前本函数直接使用未定义的 ``policy`` ⇒
            # 每次调用都抛 ``NameError`` ⇒ 被桥侧 ``except`` 吞掉 ⇒ 落点收拢与后端
            # 隔离在运行时从未真正生效。
            # 这里补上取配置，并让后端选择真正跑起来。
            policy = _config(ctx)
            ws = workspace_for(policy, session_key)
            if not ws:
                return None
            # 目录必须真的存在：``create_subprocess_exec(cwd=...)`` 指向不存在的
            # 目录会直接 ``FileNotFoundError`` ⇒ 桥起不来。
            # 创建失败⇒ 退回 None，落点是增强维度，不是硬前提，
            # 但必须可见，不静默退化成"以为收拢了其实没有"。
            try:
                os.makedirs(ws["cwd"], exist_ok=True)
            except Exception:                   # noqa: BLE001
                inc_counter("agent_sandbox_workspace_failed_total")
                logger.warning("沙箱落点目录创建失败 %s ⇒ 子进程沿用宿主 cwd", ws["cwd"])
                return None
            # 在落点之上叠加后端的隔离信息。
            # 契约纪律：接缝形状必须是 ``{"cwd","env",...}`` 字典，
            # 且既有桥只消费 cwd/env 两个键 ⇒ ``SpawnOptions`` 是内核内部类型，
            # 在此 ``to_dict()`` 后返回，向后兼容；新增键今天无人读，P1/P4/P7 才用。
            try:
                backend, _binfo = select_backend(str(policy.get("backend") or "auto"),
                                                 strict=bool(policy.get("strict")))
            except BackendUnavailable as exc:
                # strict=true 且显式后端不可得 ⇒ 可见+ 回落无隔离。
                # 诚实边界：这不等于拒绝 spawn——接缝没有"拒绝"通道，
                # 桥拿到 None 仍会起进程；真正的 spawn 级拒绝需桥配合。
                inc_counter("agent_sandbox_backend_error_total")
                logger.warning("沙箱后端不可得（strict=true）：%s ⇒ 回落无隔离", exc)
                backend = NullBackend()
            # P8：目标可移植性 —— 该桥跑不了这个后端，如 windows_mcp 进 WSL/容器
            # ⇒ 对它单独降级为 off，绝不硬套包装，硬套必崩：原生 UI 自动化在 WSL 内
            # 根本没有 Windows 窗口可碰。降级必须可见：计数 + 告警 + status 暴露。
            if owner and not is_portable(owner, backend.name):
                inc_counter("agent_sandbox_unportable_total")
                logger.warning("桥 %s 在后端 %s 下不可运行（Windows 原生依赖）⇒ 本桥降级 off",
                               owner, backend.name)
                self._unportable.add(owner)
                backend = NullBackend()
            opts = SpawnOptions(cwd=ws.get("cwd"), env=dict(ws.get("env") or {}),
                                mode=str(ws.get("mode") or "none"))
            return backend.spawn_options(opts, policy, session_key).to_dict()

        ctx.provide(Services.CHILD_SPAWN_OPTIONS, _workspace)

        async def _pre_task(payload: dict, next) -> dict:
            target = str(payload.get("task") or "")
            policy = _config(ctx)
            if not in_scope(policy, target):
                return await next()          # 不在作用域：完全不过问
            # 先问后端"实际能给什么"，再让 describe 据真实强度自述
            binfo = describe_backend(str(policy.get("backend") or "auto"),
                                     strict=bool(policy.get("strict")))
            info = describe(policy, target, binfo)
            bridge = _bridge_of(target)
            # 该桥在所选后端下跑不了⇒
            # spawn 侧已对它单独降级 off。审计必须与实际生效
            # 的一致：否则同一行审计会同时写着 enforcement=full 和 portable=False，
            # 读的人以为这次调用被隔离了 —— 那正是本项目最警惕的"看起来健康"。
            # 顺序：portable 必须在覆盖 info["backend"] 之前算。
            portable = is_portable(bridge, info["backend"])
            if not portable:
                sel = info["backend"]
                info = {**info, "enforcement": "none", "ceiling": "none", "gap": True,
                        "backend": "null", "enforced_by": "none",
                        "reason": f"桥 {bridge or target} 在后端 {sel} 下不可运行"
                                  f"（Windows 原生依赖）⇒ 已单独降级为无隔离"}
            if binfo.get("error"):
                # 显式要求了后端却不可得 ⇒ 不静默
                inc_counter("agent_sandbox_backend_error_total")
            if info["deprecated_used"]:
                # 迁移期提示：不静默
                inc_counter("agent_sandbox_deprecated_keys_total")
            sk = current_session_key(ctx)
            ws = workspace_for(policy, sk)
            await durable(ctx, Events.SANDBOX_CONFINEMENT, {
                "who": str(payload.get("caller") or "unknown"), "level": "task",
                "target": target, "risk": str(payload.get("risk") or ""),
                "bridge": _bridge_of(target),
                # 该桥能否在选中的后端下运行。
                "portable": portable,
                "mode": info["mode"], "enforcement": info["enforcement"],
                "ceiling": info["ceiling"], "gap": info["gap"],
                # 后端与"谁在强制" —— 旧实现恒填 "none"，无法区分
                # "沙箱正常评估为无隔离" 与 "沙箱自身故障/后端不可得"。
                "backend": info["backend"], "backend_error": info["backend_error"],
                # 落点：声明了档位 ⇒ 子进程被挪到会话私有目录。
                # 与桥侧共用同一个纯函数 ⇒ 审计里的 cwd 就是实际生效的那个，不会漂移。
                # 注意：这只说明"东西落在哪"，不代表强制。
                "cwd": (ws or {}).get("cwd", ""),
                "env_redirected": bool(ws),
                # enforced_by 区分"沙箱正常评估"与"沙箱自身故障"。当前后端恒 "none"；
                # 未来落地后填真实后端名，失败时填 "backend-error"。
                "enforced_by": info["enforced_by"],
                # 迁移期可见性：仍在用已废弃的裁决键 ⇒ 审计里能查到
                "deprecated_used": list(info["deprecated_used"]),
                "by": "sandbox", "session": sk,
                "reason": info["reason"], "at": now_iso(),
            })
            inc_counter("agent_sandbox_confinement_total")
            if info["gap"]:
                inc_counter("agent_sandbox_enforcement_gap_total")
            # 恒放行：本插件不再拥有准入裁决权。
            return await next()

        ctx.on(Events.TASKS_PRE_EXECUTE, _pre_task, prepend=True)

    def policy(self, ctx) -> dict:
        """当前生效配置副本。"""
        return _config(ctx)

    async def status(self, ctx) -> dict:
        """运行态摘要⇒ 插件页展示真实隔离状态。

         策略由 guard 的 ``status()`` 报；本条只报隔离，两者不互相代答。
         """
        policy = _config(ctx)
        binfo = describe_backend(str(policy.get("backend") or "auto"),
                                 strict=bool(policy.get("strict")))
        info = describe(policy, "", binfo)
        ws = workspace_for(policy, current_session_key(ctx))
        return {
            "mode": info["mode"],
            "enforcement": info["enforcement"],
            "ceiling": info["ceiling"],
            "gap": info["gap"],
            # 后端自述
            "backend": info["backend"],
            "enforced_by": info["enforced_by"],
            "backend_error": info["backend_error"],
            "reason": info["reason"],
            # 落点——与审计同源，前端可直接展示
            "workspace": (ws or {}).get("cwd", ""),
            "env_redirected": bool(ws),
            "targets": list(policy.get("targets") or []),
            "session": current_session_key(ctx) or "container",
            "deprecated_used": list(info["deprecated_used"]),
            "decides_admission": False,      # 恒 False：准入不归本插件
            # 该后端下跑不了的桥。
            # 它们被单独降级为 off—— 必须可见，否则运维会以为全都隔离了。
            "unportable_targets": sorted(
                set(unportable_bridges(info["backend"]))
                | set(getattr(self, "_unportable", ()))),
        }


__all__ = ["Sandbox", "describe", "match", "in_scope", "MODES", "ENFORCEMENTS",
           "DEFAULT_CONFIG", "DEFAULT_POLICY", "workspace_for", "_REDIRECT_ENV_KEYS"]

