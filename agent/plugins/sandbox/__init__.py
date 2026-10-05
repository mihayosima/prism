"""插件包：sandbox —— 外部工具（默认 MCP）的**隔离档位提供方 + 隔离自述**。

定位（2026-10-03 转型后）
----------------------------------------
本插件**不再做准入判定**。它只回答一个问题：

    这次外部工具调用，**声明在什么隔离档位下跑、实际强度是多少**。

三件套分工（此前本插件越界做了第一件，已收口）：

    ==========  ==================================  ==========================
    插件        判什么                                报什么
    ==========  ==================================  ==========================
    ``sys.guard``      谁能调什么（allow/deny/risk/targets）  策略矩阵（``status()``）
    ``plugins.approval``  **唯一真问人**（provider + 弹窗）   审批结果
    ``plugins.sandbox``  **不判准入**                      **隔离档位与强度**
    ==========  ==================================  ==========================

**为什么要转**：此前本插件同时持有一份 allow/deny/risk 名单，与 ``sys/guard.py``
**功能重叠** —— 两份配置并存、互相打洞、无单一真相。核定过程见
``docs/reform of production line/沙箱与权限管理器重叠核定-2026-10-03.md``。
名字匹配（尾部 ``*``）与域限定（``targets``）已迁入 guard（guard 是 ``LLM_PRE_CALL``
的**唯一**挂载者，也是唯一覆盖非 MCP 内置工具的裁决者）。

两根正交的轴（**不可压成一维阶梯** —— 压扁会逼你撒谎）
----------------------------------------
- ``mode``        = 承诺**限制到什么**（文件效应；``none`` / ``read-only`` / ``workspace-write``）
- ``enforcement`` = 实际**做到了多少**（``full`` / ``partial`` / ``none``）

例：Windows 受限 token 能兑现 ``workspace-write`` 的**写限制**，但读/网络不隔离
⇒ ``mode=workspace-write`` + ``enforcement=partial``。这不是"低一档"，
而是"同一语义、不同强度"。对齐 DeepSeek Harness 的 ``SandboxMode`` / ``SandboxEnforcement``。

★ 现状：**无任何进程级隔离实现** ⇒ 所有档位的实际强度都是 ``none``。
这不是缺陷陈述，是**事实陈述** —— 把它报成 ``full`` 才是缺陷。
插件页此前显示 ``enabled · assembled`` + ``error: null`` 看起来健康，是本项目最大的误导源之一。

fail-closed 纪律（对齐 DSH）
----------------------------------------
- 无可��后端时**不静默降级**：``enforcement`` 如实为 ``none``，并有 ``gap`` 标记落差。
- ``enforced_by`` 区分"沙箱正常评估"（当前恒 ``none``）与"沙箱自身故障"（``backend-error``）——
  对应 DSH 的 ``denialSignatures`` vs ``runnerFailureRules``。**不把基础设施故障误报成策略结果**，
  否则会触发本项目既有的 hang 自愈把工具故障当策略故障重启桥。

配置
----------------------------------------
容器级 ``config["agent"]["sandbox"]``（``core/config_schema.py`` 登记**逐键严格** schema，
否则写了配置就装配失败）；会话级覆盖走既有 ``plugins.sandbox`` 节
（``SESSION_CFG_VIEW`` + ``section_of``，与 G2 同构 ⇒ 天然按会话隔离）：

    mode     声明的隔离档位：none（默认，当前唯一能兑现的）/ read-only / workspace-write
    targets  受本插件管辖的**工具名模式**（默认 ``["mcp.*"]`` ⇒ 默认只管 MCP）
    default/allow/deny/risk   **已废弃**（准入判定归 guard，问人归 approval）

★ **四键为何不删**：`sandbox` 节是**严格键集**，删键会让写了这些键的用户配置在装配期
抛 ``ConfigError`` ⇒ **服务起不来**。故保留（无裁决效果），并由 ``describe()`` 检出
``deprecated_used`` 并计数 —— 迁移期**可见**，不静默。

模式语法：仅支持**尾部** ``*``（``mcp.*`` / ``*``），其余按精确名。刻意**不用正则**：
这份配置是运维手写的，语义越简单越不会错配。

waterfall 位置（★ 与文档旧说法相反，此处是更准的版本）
----------------------------------------
挂 ``agent/tasks/pre-execute`` 且 ``prepend=True``。但 ``core/events.py`` 的 ``prepend``
是 ``insert(0, …)`` ⇒ **后注册者排前面**。装配序是 ``profiles.py:137`` 先装
``SYS_PLUGINS``（guard）、``:145`` 再装目录插件（本插件）⇒ **实际顺序 sandbox → guard → approval**。
本插件**恒放行**，所以顺序不再影响裁决结果——这正是转型顺带消解掉的一处隐患。

审计
----------------------------------------
每次调用落 ``agent/sandbox/confinement``（durable 直写，**另立**事件——用户 2026-10-03 裁定）。
载荷含 ``mode``/``enforcement``/``ceiling``/``gap``/``enforced_by``/``bridge``/``session``。
事件名由 ``agent/sandbox/decision`` 改名而来：留着 "decision" 会让读者误以为它仍在做准入决策。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re

from agent.api import (SESSION_CFG_VIEW, Events, Services, current_session_key,
                       durable, inc_counter, now_iso, plugin)

logger = logging.getLogger("agent.plugins.sandbox")

# 兜底档位 = ``none``（**无隔离**，是当前唯一诚实可报的强度）。
# ★ 与 guard 的 ``DEFAULT_POLICY["targets"]`` 同源（都是 ``["*"]``/``["mcp.*"]`` 口径的等价物：
# 本插件只看 MCP 域，guard 看全域）。
DEFAULT_POLICY: dict = {
    "mode": "none",        # 声明的隔离档位（none/read-only/workspace-write）
    "targets": ["mcp.*"],  # 受本插件管辖的工具名模式（默认只管 MCP）
    # ---- 以下四键**已废弃**（2026-10-03 策略收口）：准入判定归 guard，问人归 approval。
    # **保留是为了向后兼容既有配置文件**——`core/config_schema.py` 的 `sandbox` 节是**严格键集**，
    # 直接删键会让写了这些键的用户配置在装配期抛 ``ConfigError`` ⇒ 服务起不来。
    # 它们**不再有任何裁决效果**，但会被 ``describe()`` 检出并计数（迁移期可见，不静默）。
    "default": "allow",
    "allow": [],
    "deny": [],
    "risk": {},
}

# 插件自管默认（与 core/config_schema.py 登记的键集一致）。
DEFAULT_CONFIG: dict = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v)
                       for k, v in DEFAULT_POLICY.items()}


def match(pattern: str, target: str) -> bool:
    """工具名模式匹配：仅支持**尾部** ``*``（``mcp.*`` / ``*``），其余按精确名。

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
    """从工具名推出**桥名**（``mcp.<bridge>.<tool>`` → ``<bridge>``）；非 MCP 形态返回空。

    隔离档位是**按桥**配置的（每个 MCP 桥一个子进程 ⇒ 一份 confinement），
    所以审计里必须带桥名才追得到"是哪个桥没隔离"。
    """
    parts = str(target or "").split(".")
    return parts[1] if len(parts) >= 3 and parts[0] == "mcp" else ""


# ---------------------------------------------------------------------------
# 落点收拢（2026-10-04）：把外部子进程的**写入落点**挪出项目根
# ---------------------------------------------------------------------------
# 为什么这一层值得做、以及它**不是**什么：
#   - 现状（``core/mcp_client.py`` spawn 时不设 cwd）⇒ 子进程继承宿主 cwd = 项目根，
#     而项目根有 ``local_secrets.json``（明文 API key）与 ``data/*.db``（全部转写/审计/记忆）。
#     叠加 ``child_env`` 会剥掉宿主的安全删除守卫 ⇒ 第三方进程在项目根拥有
#     **不受宿主约束**的读写删能力。这是 ``enforcement="none"`` 最具体的后果。
#   - 本函数把它收拢到 ``<root>/<session>`` 私有目录（cwd + 已知缓存/临时目录变量）。
#   - ★ **它不是强制**：绝对路径仍可写别处，故 ``enforcement`` **不得**因此抬成
#     partial/full（那才是谎报）。它消灭的是相对路径误伤面——实践中更高频的一类。
#     落点写进审计（``cwd`` / ``env_redirected``）⇒ 「东西落在哪」可查。
#   - 它还是未来 restricted token 的**前置步骤**：先有"允许写的那一块"，才指得过去。

#: 重定向环境变量名（**通用工具链约定**，非本项目业务概念）：
#: 临时目录 + 各语言/工具的缓存家目录。把子进程能"按相对路径写"的地方尽量收进去。
_REDIRECT_ENV_KEYS = (
    "TMP", "TEMP", "TMPDIR",                       # 临时目录（POSIX/Windows）
    "XDG_CACHE_HOME", "XDG_DATA_HOME",             # XDG
    "npm_config_cache",                            # npm/npx（playwright 走 npx）
    "PLAYWRIGHT_BROWSERS_PATH",                    # Playwright 浏览器下载
    "PIP_CACHE_DIR",                               # pip
    "UV_CACHE_DIR",                                # uv/uvx（windows_mcp 走 uvx）
)

#: cwd 与重定向的根目录（相对项目根；``data/`` 已被 .gitignore 忽略）。
_WORKSPACE_ROOT = "data/sandbox"


def _safe_seg(value: str) -> str:
    """会话键 → 目录名（防路径穿越 + 防碰撞）。

    两件事都必须成立，缺一不可：

    1. **不能穿越**：``core/session.py`` 的字符集是 ``[A-Za-z0-9_.-]``，**含 ``.``**
       ⇒ 会话键 ``..`` 会直接拼出 ``data/sandbox/..``，逃出根目录。故 ``.`` 与 ``:``
       （Windows 非法目录字符）一律替换掉。
    2. **不能碰撞**：净化**不可逆**（``a.b`` 与 ``a_b`` 都会变成 ``a_b``），只靠净化会
       让两个不同会话落进同一目录 ⇒ 会话隔离失效。
       ⇒ 追加 8 位哈希：可读前缀 + 哈希，**既唯一又安全**。
    """
    raw = str(value or "")
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw)[:32] or "default"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{digest}"


def workspace_for(policy: dict, session_key: str = "", root: str = _WORKSPACE_ROOT) -> dict | None:
    """按隔离档位算出子进程的落点；``mode="none"`` ⇒ ``None``（**保持现状，不劣化**）。

    返回 ``{"cwd": str, "env": {...}}``；``env`` 只含**要覆盖**的变量（调用方合并）。
    纯函数 ⇒ 可穷举单测，且审计侧可复用同一函数算出同一个 cwd（两处不会漂移）。
    """
    mode = str(policy.get("mode") or "none").strip()
    if mode not in MODES or mode == "none":
        return None                      # 未声明隔离 ⇒ 不重定向（opt-in）
    seg = _safe_seg(session_key or "container")
    cwd = f"{root.rstrip('/')}/{seg}"
    return {"cwd": cwd, "env": {k: cwd for k in _REDIRECT_ENV_KEYS}, "mode": mode}



def in_scope(policy: dict, target: str) -> bool:
    """该工具是否在本插件管辖范围内（不在 ⇒ 完全不过问，避免变成"第二个 guard"）。"""
    return any(match(p, target) for p in (policy.get("targets") or []))


# ---------------------------------------------------------------------------
# 隔离档位与强度（2026-10-03 转型：停止策略判定，改做 confinement）
# ---------------------------------------------------------------------------
# 两根**正交**轴（不可压成一维阶梯——压扁会逼你撒谎）：
#   mode        = 承诺**限制到什么**（文件效应，对齐 DSH 的 read-only/workspace-write）
#   enforcement = 实际**做到了多少**（full/partial/none）
# 例：Windows 受限 token 能做到 workspace-write 的**写限制**，但读/网络不隔离 ⇒ mode=workspace-write
# + enforcement=partial。这不是"低一档"，而是"同一语义、不同强度"。
MODES = ("none", "read-only", "workspace-write")
ENFORCEMENTS = ("full", "partial", "none")

# 能力矩阵：mode ⇒ 能给出的最大强度。**诚实的一侧是下限**：
# 当前无任何进程级隔离实现 ⇒ 所有 mode 的实际强度都是 ``none``。
# 未来落地 restricted token 后才可变，且**必须由实测决定**（DSH 的 Windows 后端恒 partial：
# Everyone SID 必须在 keep-alive 组 + NTFS 硬链接能跨路径别名）。
_ENFORCEMENT_CEILING: dict[str, str] = {
    "none": "none",
    "read-only": "none",          # 尚无进程级隔离可兑现只读承诺
    "workspace-write": "none",
}

_DEPRECATED_KEYS = ("default", "allow", "deny", "risk")


def describe(policy: dict, target: str = "") -> dict:
    """该工具本次调用的**隔离自述**（纯函数，便于穷举单测）。

    返回 ``{mode, enforcement, ceiling, gap, reason, deprecated_used}``：

    - ``mode``        配置声明的档位
    - ``enforcement`` **实际**能达到的强度（当前恒 ``none`` —— 无进程级隔离）
    - ``ceiling``     该 mode 在当前平台的能力上限
    - ``gap``         ``mode`` 与 ``enforcement`` 的落差（配置了但兑现不了 ⇒ 必须可见）
    - ``deprecated_used`` 仍在用已废弃的旧裁决键（迁移期提示，不静默）

    ★ 宁可 ``enforcement="none"`` 也不谎报 —— 插件页显示"看起来健康"比显示"无隔离"危险。
    """
    mode = str(policy.get("mode") or "none").strip()
    if mode not in MODES:
        mode = "none"
    ceiling = _ENFORCEMENT_CEILING.get(mode, "none")
    used = [k for k in _DEPRECATED_KEYS if policy.get(k) not in (None, [], {}, "allow", "open")]
    if mode == "none":
        reason = "未配置隔离档位"
    elif ceiling == "none":
        reason = f"档位 {mode!r} 已声明，但本插件**未实现任何进程级隔离** ⇒ 实际无隔离"
    else:
        reason = f"档位 {mode!r} 生效（强度 {ceiling}）"
    return {"mode": mode, "enforcement": ceiling, "ceiling": ceiling,
            "gap": mode != "none" and ceiling == "none",
            "reason": reason, "deprecated_used": used}


def _config(ctx) -> dict:
    """生效配置 = 插件默认 ← 容器级 ← 会话级（后者优先）。

    ⚠️ ``SESSION_CFG_VIEW`` 接缝是**可调用对象** ``(section, sk) -> dict``，调用它就已
    拿到**解包后**的插件节（``ConfigStore.view`` 内部已做过 ``section_of``）⇒ **不要再套
    一次 ``section_of``**。写成字典用法会抛 ``AttributeError``，且因为本函数在每个任务
    调用前都跑，等于**整个插件把工具调用全打断**。此处与 ``sys/budget.py`` 的读法同构。
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
    """外部工具**隔离档位提供方**（不再裁决准入——那是 guard / approval 的活）。

    2026-10-03 转型（依据 ``docs/reform of production line/沙箱与权限管理器重叠核定-2026-10-03.md``）：
    本插件曾同时做"策略判定"与"审计"，而策略判定与 ``sys/guard.py`` **功能重叠**（两份名单
    并存、互相打洞、无单一真相）。现按下述分工收敛：

    ==========  ==================================  ==========================
    插件        判什么                                不判什么
    ==========  ==================================  ==========================
    guard       谁能调什么（allow/deny/risk/targets）  不管隔离强度
    approval    唯一真问人（provider + 弹窗）          不判 allow/deny
    sandbox     **在什么隔离档位下跑 / 强度多少**      **不判 allow/deny**
    ==========  ==================================  ==========================

    因此本插件**永远放行**（`return await next()`），只落一条 ``agent/sandbox/confinement``
    审计。**"未实现隔离"这件事现在是可见的一等事实**（``enforcement="none"``），
    而不是"看起来健康"——后者是插件页此前最大的误导源。
    """

    name = "plugins.sandbox"
    layer = "system"
    provides = []
    inject = [Services.EVENTS, Services.DB]

    async def apply(self, ctx) -> None:
        # 落点接缝：**提供能力，不替调用方决定**。MCP 桥装配时取本函数算 cwd/env；
        # 沙箱未装配 ⇒ 桥侧 ``ctx.get`` 返回 None ⇒ 行为与今天完全一致（不劣化）。
        def _workspace(session_key: str = "") -> dict | None:
            ws = workspace_for(_config(ctx), session_key)
            if not ws:
                return None
            # ★ 目录**必须真的存在**：``create_subprocess_exec(cwd=...)`` 指向不存在的
            # 目录会直接 ``FileNotFoundError`` ⇒ **桥起不来**。
            # 创建失败（只读盘/权限）⇒ 退回 None（落点是增强维度，不是硬前提），
            # 但**必须可见**（计数 + warning），不静默退化成"以为收拢了其实没有"。
            try:
                os.makedirs(ws["cwd"], exist_ok=True)
            except Exception:                   # noqa: BLE001
                inc_counter("agent_sandbox_workspace_failed_total")
                logger.warning("沙箱落点目录创建失败 %s ⇒ 子进程沿用宿主 cwd", ws["cwd"])
                return None
            return ws

        ctx.provide(Services.CHILD_SPAWN_OPTIONS, _workspace)

        async def _pre_task(payload: dict, next) -> dict:
            target = str(payload.get("task") or "")
            policy = _config(ctx)
            if not in_scope(policy, target):
                return await next()          # 不在作用域：完全不过问
            info = describe(policy, target)
            if info["deprecated_used"]:
                # 迁移期提示：**不静默**（旧键仍在生效会让用户以为改了新键就生效了）
                inc_counter("agent_sandbox_deprecated_keys_total")
            sk = current_session_key(ctx)
            ws = workspace_for(policy, sk)
            await durable(ctx, Events.SANDBOX_CONFINEMENT, {
                "who": str(payload.get("caller") or "unknown"), "level": "task",
                "target": target, "risk": str(payload.get("risk") or ""),
                "bridge": _bridge_of(target),
                "mode": info["mode"], "enforcement": info["enforcement"],
                "ceiling": info["ceiling"], "gap": info["gap"],
                # ★ 落点（2026-10-04）：声明了档位 ⇒ 子进程被挪到会话私有目录。
                # 与桥侧**共用同一个纯函数** ⇒ 审计里的 cwd 就是实际生效的那个，不会漂移。
                # 注意：这只说明"东西落在哪"，**不代表强制**（见 workspace_for 的说明）。
                "cwd": (ws or {}).get("cwd", ""),
                "env_redirected": bool(ws),
                # ★ enforced_by 区分"沙箱正常评估"与"沙箱自身故障"（DSH 的
                # denialSignatures vs runnerFailureRules）。当前后端恒 "none"；
                # 未来落地后填真实后端名，失败时填 "backend-error"。
                "enforced_by": "none",
                # 迁移期可见性：仍在用已废弃的裁决键 ⇒ 审计里能查到（不静默）
                "deprecated_used": list(info["deprecated_used"]),
                "by": "sandbox", "session": sk,
                "reason": info["reason"], "at": now_iso(),
            })
            inc_counter("agent_sandbox_confinement_total")
            if info["gap"]:
                inc_counter("agent_sandbox_enforcement_gap_total")
            # ★ 恒放行：本插件不再拥有准入裁决权。
            return await next()

        ctx.on(Events.TASKS_PRE_EXECUTE, _pre_task, prepend=True)

    def policy(self, ctx) -> dict:
        """当前生效配置副本（诊断用，与 guard 的 ``policy()`` 同形状）。"""
        return _config(ctx)

    async def status(self, ctx) -> dict:
        """运行态摘要（``Container.plugin_statuses`` 消费）⇒ 插件页展示真实隔离状态。

        ★ 策略（谁能调什么）由 guard 的 ``status()`` 报；本条**只报隔离**，两者不互相代答。
        """
        policy = _config(ctx)
        info = describe(policy)
        ws = workspace_for(policy, current_session_key(ctx))
        return {
            "mode": info["mode"],
            "enforcement": info["enforcement"],
            "ceiling": info["ceiling"],
            "gap": info["gap"],
            "reason": info["reason"],
            # 落点（当前是否把子进程挪出项目根）——与审计同源，前端可直接展示
            "workspace": (ws or {}).get("cwd", ""),
            "env_redirected": bool(ws),
            "targets": list(policy.get("targets") or []),
            "session": current_session_key(ctx) or "container",
            "deprecated_used": list(info["deprecated_used"]),
            "decides_admission": False,      # 恒 False：准入不归本插件
        }


__all__ = ["Sandbox", "describe", "match", "in_scope", "MODES", "ENFORCEMENTS",
           "DEFAULT_CONFIG", "DEFAULT_POLICY", "workspace_for", "_REDIRECT_ENV_KEYS"]

