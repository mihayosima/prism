"""plugins.react · 主循环（Think → Act+Observe → 收敛）+ 三把闸。

循环形状取自 ``loong-agent``（PyPI 0.0.2）：每轮 Think；模型**不再返回工具调用**即为
最终答复；有则 Act+Observe，把结果回灌后进入下一轮。相比 ``LoongFlow`` 的
"final-answer-as-tool"，它与 function-calling 天然契合，也更贴本仓库的接缝模型。

补上 loong-agent 缺的三把闸：

===================  =========================================  ============================
闸                   位置                                        行为
===================  =========================================  ============================
``max_steps``        循环条件（配置，默认 8）                      触顶 → 统一收尾
``deadline_s``       每轮开头检查（配置，默认 120s）               触顶 → 统一收尾
token 预算           **不用写**：每步都走 ``ctx.llm``，           被拒（CapabilityNotImplemented）
                     ``agent/llm/pre-call`` 上 guard/budget 已生效  → 转"触顶"收尾（不崩）
===================  =========================================  ============================

收尾一律**诚实标注**：durable ``agent/react/exhausted`` + 文本如实说明（不静默失败、
不假装成功）。

S3（循环内上下文治理）
----------------------
循环内 Think/Act/Observe 是**任务级草稿**（``TaskContext.chain``）——**不写入**
``agent_messages``、不进工作记忆、不写 Redis；循环结束只把终局文本交 chat 落库。

窗口构造拆成两层，**职责不重叠**（详见 docs/governance/S3-循环内上下文-设计方案.md §3.6）：

========================  ==============================================  ==============
层                        做什么                                          丢条目？
``_prepare_chain``（本插件）  块内准备：老观察**逐条首尾折叠**（只折叠）          否
``_trim_messages``（治理器）   预算裁剪：**整块丢弃** + 末条保底折叠              是
========================  ==============================================  ==============

- 预算口径 ``react_budget = input_budget() - prior_tokens``（**可由负**：prior 超预算时
  由 chat 调度层的预算闸先压缩一次再重建，见 ``sys/chat._ensure_react_headroom``）；
- ``prior`` 以 **rigid** 入块（不参与常规裁剪）；chain 为可裁块；
- 条目元数据：``type``（``think``/``act``/``observe``）是**本插件私有语义**；
  ``pinned`` 是**通用元数据**，交给治理器识别（块内先占额度）；
- 治理器缺失 / 无 ``assemble`` 接缝 → 退回旧 ``_trim`` 条数裁剪，行为不变（不劣化）。

G2.0（P2：治理动词 + 收尾执行点）
---------------------------------
- **收尾执行点＝``_archive_chain`` 之前**（§4.5 ⑤）：``_govern_at_teardown`` 做
  ``assess → plan →（``on`` 时）execute``，整单元替换只允许发生在这一时机（run 内
  append-only 高命中区绝不改写）；``shadow``（缺省）只算不做、决策写遥测；
- **``on_compact`` 由本插件提供**（``_chain_compactor`` → ``_summarize_units``）：
  **确定性**过程摘要（不额外调 LLM），覆盖「调用意图 / 参数要点 / 结果要点」三成员信息；
- **``wm_read`` 读回元工具**（P2-4）：补全渐进披露闭环——D2 折叠把大参数替换为引用标记时
  逐条给出**精确 checkpoint 键**，模型由此按需读回完整原文。原设计的「工作记忆目录块」
  **不入线**（会打断 F-1 严格前缀；见 ``_loop_blocks`` 与方案 §6.1 的偏差记录）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Any

from agent.api import (CapabilityNotImplemented, Events, Services, SessionIdentity,
                      StateKeys, coerce_identity, durable, estimate_request_cost,
                      estimate_tokens, inc_counter, now_iso, current_permission_mode,
                      set_current_run_id, set_current_session_key)

from .components import Actor, Finalizer, Observer, Reasoner, ToolResult
from .prompt import BUDGET_TIGHT_SYSTEM, build_system, render_params

logger = logging.getLogger("agent.react")


@dataclass(slots=True)
class ReactConfig:
    """配置（``config["agent"]["react"]``）。"""

    max_steps: int = 8                 # 循环上限（步数闸）
    deadline_s: float = 120.0          # 整任务时延上限（时延闸）
    # 注：单条工具结果上限**不在此处** —— 归源头护栏（S4：``context.source_clip_budget()``
    #     经 ``sys/tasks.invoke`` 在结果离开工具时净化，token 口径）。
    #     旧 ``tool_result_max_chars``（字符口径）已删除，避免与治理器口径打架。
    history_keep: int = 20             # 喂给模型的最近消息条数（**降级路径**裁剪口径）
    max_rejects: int = 3               # 同一工具被拒多少次即终止（防死循环）
    # P1-1（2026-10-10）：无进展检测 —— 泛化上方连拒护栏到「同参数重复调用」。
    # 把 no_progress_repeat 设极大（如 999）即等效关闭检测器（一键回退旧行为）。
    no_progress_window: int = 5        # 滑动窗口步数（只看最近 N 步的请求指纹）
    no_progress_repeat: int = 3        # 窗口内同一 (工具, 参数) 指纹出现次数阈值
    finalize_on_exceed: bool = True    # 触顶时再答一次（不给工具）
    # ---- S3 循环内上下文：**兜底默认**（治理器缺该方法时才用到） ----
    # 注：折叠**阈值**（单条上限 / 头尾保留量）归治理器（``clip_*``），本插件不持该策略；
    #     治理器无 ``clip_head_tail`` 接缝 → 不做折叠（与「无治理器退旧路径」一致）。
    react_keep_recent: int = 6         # 最近 N 条链原文保留，更老者才折叠
    # ---- D2：老 act 条目大参数折叠（token/值；<=0 关闭；需工作记忆在位才生效） ----
    act_fold_tokens: int = 256
    # ---- L1：懒加载（目录卡 + 元工具按需装载完整 schema；默认 off 灰度） ----
    lazy_tools: bool = False
    lazy_tools_max: int = 12
    # ---- G2.0（P0/F-1）：续接前缀对齐 ----
    # True = 续接时**复用链存档里的头部快照**（system/prior/prompt 三块逐字节不变，
    # 用户本次消息追加为链尾），使上一轮请求成为本轮请求的真前缀 → 整链命中；
    # False = 一键回退旧行为（头部按本轮重建，续接首步整链 miss）。
    alignment_enabled: bool = True


@dataclass(slots=True)
class TaskContext:
    """循环内任务视图（**不持久化**；任务结束即弃）。

    职责边界（S3 钉死，防双份真相）：
    - ``prior``：入口已装配的会话上下文——**rigid**，循环内不参与常规裁剪；
    - ``chain``：**仅**循环产生的 Think/Act/Observe——任务级草稿，**不进** ``agent_messages``、
      不进工作记忆、不写 Redis；
    - ``task_id``：工作记忆键的**引用**（非副本）——进度态 SSOT 仍在
      ``plugins.work_memory``，此处**不自持进度字段**。

    条目元数据归属：
    - ``type``（``think``/``act``/``observe``）= **本插件私有语义**，供 ``_prepare_chain`` 筛选；
    - ``pinned`` = **通用元数据**，交给治理器识别（块内先占额度），本插件只负责打标。
    """

    prior: list[dict] = field(default_factory=list)
    chain: list[dict] = field(default_factory=list)
    task_id: str = ""
    step: int = 0
    loaded: set[str] = field(default_factory=set)   # L1：已加载完整定义的工具名
    skill_docs: list[str] = field(default_factory=list)   # 技能说明（经 skill 工具按需注入，下一轮喂给模型；循环内草稿，不持久化）
    # G2.0（P0/F-1）：**冻结的头部快照** ``{system_text, prior, prompt}``。
    # 一旦冻结，本会话后续每个 run 都复用同一份头部字节——这是「续接请求的前缀
    # 不被头部变更打断」的前提（否则易变的头部会让整条链在缓存上失效）。
    # 非进度字段：任务结束即弃；跨 run 只经链存档流通。None = 本 run 未冻结（降级）。
    head: dict | None = None


# G2.0（P0）声明式注册：本端点「应有块」清单（**配置期常量**；声明幂等，以最后一次为准）。
# ``required=False`` 者表示「无该内容时合法缺席」，不计入漏报红灯。
_LOOP_DECLS = [
    {"key": "react_system", "kind": "text"},
    {"key": "react_prompt", "kind": "messages"},
    {"key": "react_chain", "kind": "messages"},
    {"key": "react_prior", "kind": "messages", "required": False},
]


def _declare_loop_blocks(gov) -> None:
    """把本端点的应有块清单注入治理器（幂等；无该接缝 → 静默跳过，不劣化）。

    完整性校验的口径是「**连提交都没有**」——``present=False`` 的块仍算已提交，
    因此只有整块从装配管线里消失才会亮红灯（低误报、直指真实的漏报缺陷）。
    """
    fn = getattr(gov, "declare", None)
    if not callable(fn):
        return
    try:
        fn(_LOOP_DECLS, phase="react")
    except Exception as exc:      # noqa: BLE001 —— 声明失败不阻断循环
        logger.warning("react declare blocks failed: %s", exc)


def _gov_str(gov, name: str, default: str) -> str:
    """从治理器取字符串配置（缺方法/异常 → 回落默认，不劣化）。"""
    fn = getattr(gov, name, None)
    if not callable(fn):
        return str(default)
    try:
        return str(fn())
    except Exception:      # noqa: BLE001 —— 配置查询异常不阻断循环
        return str(default)


def _gov_int(gov, name: str, default: int) -> int:
    """从治理器取整型配置（缺方法/异常 → 回落本插件兜底默认，不劣化）。"""
    fn = getattr(gov, name, None)
    if not callable(fn):
        return int(default)
    try:
        return int(fn())
    except Exception:      # noqa: BLE001 —— 预算查询异常不阻断循环
        return int(default)


@dataclass(slots=True)
class ReactResult:
    """一次 run 的结果。``ok`` 只表示"是否给出了答复"；``exhausted`` 表示"是否被闸截停"。"""

    ok: bool
    text: str
    steps: int
    tokens_in: int = 0
    tokens_out: int = 0
    exhausted: bool = False
    reason: str | None = None
    tools: list[str] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)
    summary: str = ""                     # C：收尾摘要（chat 并入 memory.remember 载荷）
    run_id: str = ""                      # 本次 run 的自生成 run_id（uuid4.hex）；子 Agent 返回契约回填 request_id 用

    def as_evidence(self) -> dict:
        """机器证据（供 chat 转写 / 前端展示；纯数据）。"""
        return {"intent": "react", "ok": self.ok, "steps": self.steps,
                "exhausted": self.exhausted, "reason": self.reason,
                "tools": list(self.tools), "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "observations": list(self.observations),
                "summary": self.summary}


def _brief(value: Any, limit: int = 120) -> str:
    """把任意值压成一行短摘要（过程摘要用；只做投影，不解读语义）。"""
    if isinstance(value, str):
        text = " ".join(value.split())
    elif isinstance(value, dict):
        text = ", ".join(f"{k}={_brief(v, 40)}" for k, v in list(value.items())[:6])
    else:
        text = str(value)
    text = text.strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _slim_description(desc: str, limit: int = 120) -> str:
    """D4：工具描述压成一行（首行，截 ``limit`` 字符）——纯投影，原目录不动。"""
    text = str(desc or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    return line[:limit]


def visible_tools(ctx) -> list[dict]:
    """工具可见集（权限模式裁剪；第 3 期启用预留，依据《审批插件》§6.1）。

    ``plan`` 模式 ⇒ 只保留 ``risk == "read"``（**软约束**：模型看不见写工具，
    防误导；幻觉调用仍由 approval 的 plan 硬兜底拒绝——软硬两层缺一不可）。
    判定只经内核助手 ``current_permission_mode(ctx)`` 读该状态键（SSOT 前向契约），对 approval
    **零耦合**；R1 补（2026-10-02）起该键是 ``dict[会话键, mode]`` 分桶（此前单值 ⇒ 档位
    跨会话互相覆盖）。其余模式恒全量（行为不变）。

    D4（描述瘦身）：返回前把 ``description`` 压成一行（首行 ≤120 字符）。这是
    **react 投影层**的改动——``ctx.tasks.catalog()`` 原始目录不动，approval /
    管理页等其它消费方拿到的仍是全文；系统提示词目录与 native tools 数组共用
    本投影，两处口径天然一致。
    """
    catalog = ctx.tasks.catalog()
    # R1 补：档位按会话分桶 ⇒ 经内核助手取**本会话**的值（此前读容器级单值，
    # 会把别人的档位当自己的：A 在 plan 会让 B 的工具也只剩只读）。
    if current_permission_mode(ctx, "") == "plan":
        catalog = [t for t in catalog if t.get("risk") == "read"]
    return [{**t, "description": _slim_description(t.get("description"))}
            for t in catalog]


def _split_whitelist_ids(whitelist) -> tuple[set[str], set[str]]:
    """把白名单拆成 ``(放行标识符, 排除标识符)``（**纯函数**；阶段 3 新增排除项）。

    - ``"agent.read"`` / ``"web.*"`` ⇒ 放行标识符（工具名或 ``owner`` 域）；
    - ``"-agent.delegate"`` ⇒ **排除**标识符（``-`` 前缀）；
    - 放行集为空（只写了排除项）⇒ 语义为「其余全放行」（由调用方归一为 ``{"*"}``）。

    为什么需要排除项（设计稿 v0.4 §3.2a / R1）：「除了某几个工具以外都要」表达不出来，
    而「子 Agent 永远是叶子节点」正是这一形态——子必须继承父的宽工具面，
    但**委派工具本身绝不能下传**（否则形成子↔子 mesh）。用具体名单枚举「其余全部」
    会在目录变化时静默失效，故必须有排除语义。
    """
    pos: set[str] = set()
    neg: set[str] = set()
    for w in whitelist or ():
        s = str(w or "")
        if not s or s == "-":                # "" 与裸 "-" 都不是有效标识符 ⇒ 丢弃
            continue                          # 注意："*" 是合法标识符，不能按长度裁掉
        if s.startswith("-"):
            neg.add(s[1:])
        else:
            pos.add(s)
    return pos, neg


def _apply_whitelist(base: list[dict], whitelist: list[str] | None) -> list[dict]:
    """按白名单收窄可见工具集（**纯函数、幂等、不改入参**；子 Agent 方案 v0.3 §3.2）。

    标识符语义（与工厂 ``tools_override`` 口径一致）：
    - ``whitelist is None`` / 空列表 ⇒ 不收窄，**原样返回同一列表对象**（默认态零行为变化）；
    - ``"*"`` ∈ 白名单且无排除项 ⇒ 不额外收窄（仍受 ``visible_tools`` 权限裁剪，
      白名单只能收窄不能放宽）；
    - 具体工具名（``"agent.read"``）⇒ 命中该名工具；
    - 域（``owner``，如 ``"plugins.explorer"`` 或 ``"web.*"``）⇒ 命中该域全部工具；
    - ``"-x"`` 前缀 ⇒ **排除**该工具名/域（阶段 3；排除优先级高于放行）。
    - 只写排除项（放行集为空）⇒ 视为 ``"*"`` + 排除项。

    交集语义：``role.tools`` / ``param.tools`` 任意一层都只能**再收窄**，不能放宽
    （审查意见 #2 / #R3：``role.tools`` 是安全下界）。
    """
    if not whitelist:
        return base
    pos, neg = _split_whitelist_ids(whitelist)
    if not neg and "*" in pos:
        return base
    if not pos:                       # 只有排除项 ⇒ 其余全放行
        pos = {"*"}
    out = []
    for t in base:
        name = t.get("name")
        dom = _domain_of(t)
        if name in neg or dom in neg:
            continue
        if "*" in pos or name in pos or dom in pos:
            out.append(t)
    return out


def _trim(messages: list[dict], keep: int) -> list[dict]:
    """历史裁剪：保 system + 首个用户请求 + 最近 ``keep`` 条（多步循环的上下文治理）。

    只裁剪**喂给模型的窗口**，不丢循环自身的状态（消息全集由调用方持有）。

    ★ 未超阈时返回**浅拷贝**（而非原对象）：调用方（``_run_impl``）会在预算预警时
    对返回值 ``append`` 一条系统提示——若返回原 list，就会**就地改到共享的 ``messages``**，
    下一步再 append 一次 ⇒ 提示随步数累积、prompt 持续膨胀（此前注释自称"幂等"，
    实为累积；仅在无治理器的降级路径触发）。返回新 list 后 append 落在这份副本上，幂等。
    """
    if keep <= 0 or len(messages) <= keep + 2:
        return list(messages)
    first_user = next((i for i, m in enumerate(messages)
                       if m.get("role") == "user" and isinstance(m.get("content"), str)), None)
    cut = len(messages) - keep
    # 400-fix A：切点不得落在 act 与其观察回灌之间——kept 后缀首条若是观察回灌，
    # 其前置 act（紧邻上一条）必须一并保留（回退直至切点停在配对边界之外）。
    while 0 < cut < len(messages) and _is_observe(messages[cut]):
        cut -= 1
    idx = {0, *range(cut, len(messages))}
    if first_user is not None:
        idx.add(first_user)
    return [m for i, m in enumerate(messages) if i in idx]


def _is_observe(m: dict) -> bool:
    """内部消息形状判据：观察回灌（content 为含 ``tool_results`` 的 dict）。"""
    c = m.get("content")
    return isinstance(c, dict) and c.get("tool_results") is not None


def _card(t: dict) -> dict:
    """L1：工具目录卡——名称 + 一行描述 + 参数名（无 JSON Schema），供懒加载模式暴露。"""
    return {"name": t.get("name"), "description": _slim_description(t.get("description")),
            "params": {}, "required": [], "risk": t.get("risk", "write"),
            "owner": t.get("owner", "")}


# ---------- G（路线 2）：投影层「域级裁剪」纯函数 ----------
# 域 ≡ 注册来源 owner（``sys/tasks.py`` 注册时写入，未显式传则取当前插件 id）。
# ⇒ 新 MCP 插件**自动成为一个新域**，本机制零额外工作量（详见实施方案 §3.7）。


def _domain_of(t: dict) -> str:
    """工具所属域（= 注册来源 ``owner``）；缺失归一为空串（渲染域清单时标注为「未归属」）。"""
    return str(t.get("owner") or "")


def clip_by_domain(tools: list[dict],
                   allowed: set[str] | None) -> tuple[list[dict], dict[str, int]]:
    """按域裁剪工具列表（**纯函数、幂等、不改入参**）。

    ``allowed is None`` ⇒ 不裁剪，**原样返回同一列表对象**（默认态零行为变化）。
    否则返回 ``(放行工具, 被隐藏域计数)``；计数供提示层渲染「域清单」（可发现性线索）。

    注意本函数只作用于**投影**：``tools`` 变量在调用点仍保持全量并交给
    ``tool_search`` / ``tool_load``，故隐藏域「对模型不可见、对检索可见」——
    这是本方案与「注册层裁剪」的根本区别（后者会让隐藏工具永远搜不回来）。
    """
    if allowed is None:
        return tools, {}
    kept: list[dict] = []
    hidden: dict[str, int] = {}
    for t in tools:
        d = _domain_of(t)
        if d in allowed:
            kept.append(t)
        else:
            hidden[d] = hidden.get(d, 0) + 1
    return kept, hidden


def allowed_domains(ctx) -> set[str] | None:
    """放行域集合（来源 = 配置 ``agent.react``；返回 ``None`` 表示**不裁剪**）。

    - ``tool_domains_enabled`` 为假 ⇒ ``None``（灰度默认，逐字节等价现状）；
    - 白名单为空/全空白 ⇒ ``None``（**不做「全隐藏」**，避免一处笔误把工具全关掉；
      确需全隐藏可在调用点以 ``allowed=set()`` 直接表达，不经配置）；
    - 其余 ⇒ 去空白后的域名集合。

    前向契约：将来若真有 per-session 通道，可在**此处**优先读取（注意 ``ctx.state``
    实为**容器级**而非会话级，直接写它会串扰所有会话——详见实施方案 §3.3）。
    """
    node = ((ctx.config or {}).get("agent") or {}).get("react") or {}
    if not node.get("tool_domains_enabled"):
        return None
    doms = node.get("tool_domains") or []
    if isinstance(doms, str):        # 容忍手写配置写成裸字符串（否则会被拆成单字符集合）
        doms = [doms]
    return {str(x).strip() for x in doms if str(x).strip()} or None


def _clip_with_fallback(tools: list[dict], allowed: set[str] | None
                        ) -> tuple[set[str] | None, list[dict], dict[str, int]]:
    """``clip_by_domain`` + 一道**安全阀**，返回 ``(生效 allowed, 放行工具, 隐藏域计数)``。

    安全阀：白名单与**任何**已注册域都不相交时（典型成因＝域名拼错，如把
    ``plugins.windows_mcp`` 写成 ``windows``），裁剪结果会退化成「全隐藏」。
    这与配置语义「空白名单 = 不裁剪」相悖，也会让「勾了开关却没省下一个 token」
    变成一个静默谜题。故此处**回退为不裁剪**并响亮告警。

    正常路径（白名单命中至少一个域）行为与 ``clip_by_domain`` 完全一致；
    ``allowed is None`` 或 ``tools`` 为空时零额外判断。
    """
    if allowed is None:
        return None, tools, {}
    tools_vis, hidden = clip_by_domain(tools, allowed)
    if tools and not tools_vis:
        logger.warning(
            "tool_domains 白名单 %s 未匹配任何已注册域（现有域：%s）⇒ 本轮不裁剪；"
            "请检查配置里的域名拼写", sorted(allowed),
            sorted({_domain_of(t) for t in tools}))
        return None, tools, {}
    return allowed, tools_vis, hidden


# L1：元工具完整定义（恒对模型可见；loop 内联处理，不进 ctx.tasks 注册表——
# 避免注册表污染与跨 run 状态，TaskContext.loaded 是唯一真相）
LAZY_META_TOOLS: list[dict] = [
    {"name": "tool_search", "description": "按关键词搜索可用工具（返回目录卡：名称/描述/参数名）",
     "params": {"keyword": {"type": "str"}}, "required": ["keyword"],
     "risk": "read", "owner": "plugins.react"},
    {"name": "tool_load", "description": "加载工具的完整参数定义（此后才能调用该工具；逗号分隔可多个）",
     "params": {"names": {"type": "str"}}, "required": ["names"],
     "risk": "read", "owner": "plugins.react"},
    # G2.0（P2-4）：检查点**读回**工具——补全渐进披露闭环：目录块给出「有哪些存档」，
    # 本工具按需取回完整原文（早前 act 大参数被折叠为引用标记时，模型由此自取回）。
    {"name": "wm_read", "description": "按检查点名读回工作记忆中的完整原文（本任务内）",
     "params": {"key": {"type": "str"}}, "required": ["key"],
     "risk": "read", "owner": "plugins.react"},
]


class ReactLoop:
    """ctx.react —— 多步工具循环（四组件可换；三把闸）。

    子 Agent 接缝（v0.3 §3.2）：构造期可选传入 ``tools_override`` / ``cfg_overrides`` /
    ``components_override`` / ``governance_override`` / ``depth``。全部默认 ``None``/``0``
    ⇒ **逐字节/逐值等价于改动前**（无 override 时 ``resolve_tools`` 回落 ``visible_tools``、
    ``resolve_cfg`` 回落 ``self._cfg``、``_resolve_governance`` 回落容器现取）。
    未来做「子 Agent 独立上下文治理（Q4）」/「组件层按 role 解耦（Q6）」只在工厂传参，
    调用点零改动 ⇒ 零返工。
    """

    def __init__(self, reasoner: Reasoner, actor: Actor, observer: Observer,
                 finalizer: Finalizer, cfg: ReactConfig, *,
                 tools_override: list[str] | None = None,
                 cfg_overrides: dict | None = None,
                 components_override: dict | None = None,
                 governance_override: object | None = None,
                 depth: int = 0):
        # Q6 接缝：按 key 替换组件，缺省回落构造传入组件（不传 components_override 时逐值等价）。
        comps = components_override or {}
        self._reasoner = comps.get("reasoner", reasoner)
        self._actor = comps.get("actor", actor)
        self._observer = comps.get("observer", observer)
        self._finalizer = comps.get("finalizer", finalizer)
        self._cfg = cfg
        # 其余接缝参数原值存储，由 resolve_* 方法在运行期应用（见下）。
        self._tools_override = tools_override
        self._cfg_overrides = cfg_overrides
        self._governance_override = governance_override
        self._depth = depth

    @property
    def config(self) -> ReactConfig:
        return self._cfg

    @property
    def depth(self) -> int:
        """当前循环深度（子 Agent 递归护栏；主循环为 0）。"""
        return self._depth

    def visible_tools(self, ctx) -> list[dict]:
        """当前工具可见集（读 ``permission_mode``；本次恒全量 —— 见模块级 ``visible_tools``）。"""
        return visible_tools(ctx)

    # ---------- 子 Agent 接缝方法（可单测；无 override 时回落既有行为） ----------

    def resolve_tools(self, ctx) -> list[dict]:
        """最终可见工具集（接缝点）：先经 ``visible_tools(ctx)`` 权限裁剪，再按白名单收窄。

        不传 ``tools_override`` ⇒ 等价于 ``visible_tools(ctx)``（决策路径与现状一致）。
        """
        tools = visible_tools(ctx)
        if self._tools_override is not None:
            tools = _apply_whitelist(tools, self._tools_override)
        return tools

    def resolve_cfg(self) -> ReactConfig:
        """合并 ``cfg_overrides`` 后的实际配置（接缝点）；不传 ⇒ 原样返回 ``self._cfg``。

        未知键被静默忽略（前向兼容未来新增字段）；已知键覆盖。
        """
        if not self._cfg_overrides:
            return self._cfg
        known = ReactConfig.__dataclass_fields__
        return replace(self._cfg, **{k: v for k, v in self._cfg_overrides.items() if k in known})

    def _resolve_governance(self, ctx):
        """治理器取用（Q4 接缝）：优先 ``governance_override``，否则回落容器现取。

        默认回落与历史行为逐值等价（``ctx.get(Services.CONTEXT)`` 现取）；
        ``governance_override`` 为未来「子 Agent 独立上下文治理」预留——传入即覆盖，
        本类 4 处调用点无需再改 ⇒ 零返工。
        """
        if self._governance_override is not None:
            return self._governance_override
        return ctx.get(Services.CONTEXT) if ctx.has(Services.CONTEXT) else None

    async def run(self, ctx, prompt: str, *, session_id: str = "default",
                  prior: list[dict] | None = None, resume: bool = False,
                  resume_state: dict | None = None,
                  identity: SessionIdentity | None = None) -> ReactResult:
        """公开入口：**唯一职责是给取消/异常路径兜住收尾**（H1）。

        为什么单独一层而不把 330 行主体缩进进 try/finally：
        主体已很长，整块重缩进会让 diff 变成"全文件重写"， review 失去焦点；
        而且**成功与触顶两条路径各有自己的收尾**（存档/治理水位/置完成），
        再套 finally 会**重复做一遍**（双份存档 + 二次 finish）。

        ⇒ 这里只兜**异常/取消**：任何从主体逸出的异常（含 ``CancelledError``）
        都会跳过正常收尾 ⇒ 快照停留在 ``status="running"`` ⇒ 下次请求
        ``_probe_resume`` 把它当崩溃残留**自动续接**（静默错误状态延续）。
        兜住后快照被标 ``cancelled``（不是 finished，见 backend 的说明），
        ``_probe_resume`` 只认 ``running`` ⇒ 不再误续接。

        ★ **绝不吞异常**：兜底只做"把状态挪走"，随后原样重抛。
        """
        box: dict = {}
        try:
            return await self._run_impl(ctx, prompt, box, session_id=session_id,
                                        prior=prior, resume=resume,
                                        resume_state=resume_state, identity=identity)
        except BaseException as exc:
            await self._abandon(ctx, box, exc)
            raise

    async def _abandon(self, ctx, box: dict, exc: BaseException) -> None:
        """取消/异常路径的**最小收尾**：挪走快照状态 + 清 run 通道 + 落审计。

        ★ 这里处在**取消上下文**中：任何 ``await`` 都可能立刻再抛 ``CancelledError``。
        故每一步都独立吞掉全部异常（含 BaseException）——目标是"尽力而为"，
        绝不能让收尾动作替掉**原始异常**（那才是真正要传给调用方的信息）。
        """
        task_id = box.get("task_id")
        if not task_id:
            return                                   # 还没跑到生成 task_id ⇒ 无状态可挪
        cancelled = isinstance(exc, asyncio.CancelledError)
        try:
            await durable(ctx, Events.REACT_EXHAUSTED, {
                "steps": box.get("steps", 0), "at": now_iso(),
                "cancelled": cancelled, "abandoned": not cancelled,
                "reason": f"{'已取消' if cancelled else '异常中断'}：{type(exc).__name__}",
                "task_id": task_id})
        except BaseException:                        # noqa: BLE001
            pass
        wm, ident = box.get("wm"), box.get("ident")
        if wm is not None and ident is not None:
            try:
                await _snapshot_call(wm, "snapshot_mark_abandoned", ident.tenant, task_id)
            except BaseException:                    # noqa: BLE001
                inc_counter("agent_react_abandon_marks_failed_total")
        try:
            set_current_run_id("")                   # R1：两通道同步置空
            ctx.state[StateKeys.REACT_RUN_ID] = ""
        except BaseException:                        # noqa: BLE001
            pass

    async def _run_impl(self, ctx, prompt: str, box: dict, *, session_id: str = "default",
                        prior: list[dict] | None = None, resume: bool = False,
                        resume_state: dict | None = None,
                        identity: SessionIdentity | None = None) -> ReactResult:
        """``identity``：2.2 会话/租户身份（由适配层解析后**显式线程化**传入）。

        为什么是显式参数而非 ``ctx.get(...)``：``ctx.state`` 与服务都是**容器级**且被并发
        请求共享，把「当前会话」塞进共享槽会互相覆盖（H5/H6）。显式传参零歧义、零竞态。
        缺失时按 ``session_id`` 退化（保持既有调用方可用，见内核 ``coerce_identity``）。
        """
        cfg = self.resolve_cfg()       # 子 Agent 接缝：合并 cfg_overrides（无 override 时回落 self._cfg）
        # 身份 → 键空间（task_id / 快照 tenant / 会话键）一次定型，全程不变
        ident = identity if identity is not None else coerce_identity(session_id)
        session_key = ident.session_key
        await durable(ctx, Events.REACT_START, {
            "prompt": (prompt or "")[:500], "session_id": session_key,
            "tenant": ident.tenant, "user": ident.user,
            "max_steps": cfg.max_steps, "at": now_iso()})

        tools = self.resolve_tools(ctx)   # 子 Agent 接缝：权限裁剪 + tools_override 收窄（无 override 时等价于 visible_tools）
        # G（路线 2）：投影层域裁剪——隐藏域「对模型不可见、对检索可见」。
        # 硬前提：只在懒加载时启用（非懒加载无 tool_search/tool_load 检索通道，
        # 裁剪即硬隐藏＝不可回溯）；故 allowed 在此处被 lazy_tools 一票否决。
        allowed = allowed_domains(ctx) if cfg.lazy_tools else None
        allowed, tools_vis, hidden = _clip_with_fallback(tools, allowed)
        # 技能常驻元工具：独立于 lazy_tools 开关，non-lazy 模式下也须可见（否则默认失效）
        _cat = ctx.get(Services.SKILLS, None)
        if _cat is not None:
            tools_vis = list(tools_vis) + [dict(m) for m in _cat.meta_tools()]
        # L1：懒加载时系统提示只挂目录卡（完整定义按需经 native tools 分步装载）；
        # 目录同样按域裁剪（只裁 native 不裁目录 ⇒ 模型仍从名册看见隐藏域，「隐藏」不成立）。
        prompt_catalog = ([_card(t) for t in tools_vis] if cfg.lazy_tools else tools_vis)
        _sys_mem = ctx.get(Services.SYSTEM_MEMORY) or ""
        _cat_skills = ctx.get(Services.SKILLS, None)
        if _cat_skills is not None:
            _idx = _cat_skills.index_block()
            if _idx:
                _sys_mem = f"{_sys_mem}\n\n{_idx}" if _sys_mem else _idx
        system_prompt = build_system(prompt_catalog, _sys_mem, hidden_domains=hidden)

        # 工作记忆（第 3 层）：任务级进度缓存。接缝获取，缺失则跳过（不破解耦）。
        wm = ctx.get(Services.WORK_MEMORY)
        run_id = uuid.uuid4().hex
        # 2.2：task_id = {user}:{session}:{run}（键构造唯一真源 core/session.py）
        task_id = ident.task_id(run_id)
        # 阶段 2（2.1/D19）：跨进程 run 快照保留最近 N 条 chain
        _wm_cfg = ((ctx.config or {}).get("agent") or {}).get("work_memory") or {}
        snap_keep = int(_wm_cfg.get("snapshot_chain_keep", 20))
        # F2：账单归组键（budget 的 pre-call 读此键落 agent_budget_log.run_id）。
        # 2.2 起存 **task_id**（自带 user/session）。
        # R1（2026-10-02）：**主通道是 ContextVar**（按 asyncio task 隔离 ⇒ 每请求独立），
        # 否则并发请求交错时账单行会取到别人的 run；``ctx.state`` 仍照写（兼容直接读
        # state 的既有测试与调用点），收尾清空时两通道同步置空（见本文件下方）。
        set_current_run_id(task_id)
        ctx.state[StateKeys.REACT_RUN_ID] = task_id
        # 2.2：当前会话提示（approval 等无 ctx 入参的通道据此判定归属）；R1：同走请求作用域通道
        set_current_session_key(session_key)
        ctx.state[StateKeys.SESSION_KEY] = session_key
        if wm is not None:
            await wm.begin(task_id, {"session_id": session_key, "tenant": ident.tenant,
                                     "run_id": run_id, "max_steps": cfg.max_steps})
        # 异常/取消路径的收尾依据（H1）：外层 run() 的兜底据此挪走快照状态。
        box.update(task_id=task_id, ident=ident, wm=wm, steps=0)

        # 任务级视图（S3）：prior = 入口已装配上下文（rigid，循环内不重裁）；
        # chain = **仅**循环产生的 Think/Act/Observe（草稿，循环结束即弃）。
        task = TaskContext(prior=[dict(m) for m in (prior or [])], task_id=task_id)
        # P2：「继续」续接——取回上轮链存档作初始 chain（再走 _prepare_chain 治理；
        # 无存档如实回退现状，不伪造）
        # G2.0（P0/F-1）：同时取回**头部快照**并整体冻结 —— 使续接请求的首个差异点
        # 落在链尾（追加）而非头部（改写），上一轮请求由此成为本轮请求的真前缀。
        resumed = False
        if (resume or resume_state) and wm is not None:
            try:
                if resume_state:
                    # 阶段 2（2.1/D6/D15）：崩溃自动续接——快照（run_snapshot）由调用方
                    # （sys/chat.py::_send_via_react 探测）直接传入，**优先于** react_chain。
                    data = dict(resume_state)
                else:
                    # 显式「继续」：读 run 收尾归档 react_chain:{session}（现有语义不变）
                    # 2.2：归档键按**会话键**（{user}:{session}）——不同用户同名会话不串档
                    arc = await wm.get(f"react_chain:{session_key}")
                    cps = json.loads((arc or {}).get("checkpoints") or "[]")
                    data = next((c.get("data") or {} for c in reversed(cps)
                                 if c.get("name") == "chain"), {})
                archived = [dict(m) for m in (data.get("chain") or [])
                            if isinstance(m, dict)]
                if archived:
                    task.chain.extend(archived)
                    resumed = True
                    await durable(ctx, Events.REACT_ARCHIVED, {
                        "session_id": session_id, "resumed": True,
                        "entries": len(archived), "at": now_iso()})
                snap = data.get("head") if cfg.alignment_enabled else None
                if isinstance(snap, dict) and isinstance(snap.get("prompt"), str):
                    task.head = {
                        "system_text": str(snap.get("system_text") or ""),
                        "prior": [dict(m) for m in (snap.get("prior") or [])
                                  if isinstance(m, dict)],
                        "prompt": snap["prompt"],
                    }
            except Exception as exc:                 # noqa: BLE001 —— 续接失败退全新 run
                logger.warning("react chain resume load failed: %s", exc)

        inherited = task.head is not None          # 头部是否来自存档快照（而非本轮新建）
        if inherited:
            # 冻结命中：头部三块取**存档字节**（不用本轮重建值：prior 会随会话推进而变，
            # prompt 会变成「继续」）；用户本次消息**追加为链尾 user 轮**——
            # 信息零丢失：上轮结论是链的最后一条，本次指令是新追加的那条。
            system_prompt = task.head["system_text"] or system_prompt
            task.prior = list(task.head["prior"])
            head_prompt = task.head["prompt"]
            task.chain.append({"role": "user", "content": prompt, "type": "resume"})
        else:
            # 未冻结（首轮 / 旧存档 / 开关关闭）：以本轮构造值冻结，供后续 run 复用
            head_prompt = prompt
            task.head = {"system_text": system_prompt,
                         "prior": [dict(m) for m in task.prior], "prompt": prompt}
        if resumed and not inherited:
            # F-1 体检红灯：续接了但头部**未**冻结 → 链之前的头部字节与本轮不同，
            # 整条链将在缓存上失效（本次事故的机制）。如实记账，不阻断。
            await durable(ctx, Events.CONTEXT_PREFIX_UNSTABLE, {
                "session_id": session_id, "stage": "react",
                "reason": "alignment_disabled" if not cfg.alignment_enabled else "no_snapshot",
                "chain_entries": len(task.chain), "at": now_iso()})

        # 消息全集：供 finalize 与「无治理器」降级裁剪使用；**绝不写入 agent_messages**。
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        messages.extend(task.prior)
        messages.append({"role": "user", "content": head_prompt})
        if resumed:
            # 续接：链上内容（存档链 + 本次消息）一并镜像，使 messages 与 wire 同序——
            # 「无治理器」降级路径因此同样看得到既有链与本次指令（不劣化）。
            messages.extend(dict(m) for m in task.chain)
        window: list[dict] = messages          # 最近一次喂给模型的窗口（治理后）
        win: dict = {}                         # 最近一次装配统计（P1：run 收尾水位体检取用）

        steps = 0
        tokens_in = tokens_out = 0
        rejected: dict[str, int] = defaultdict(int)
        call_sigs: list[tuple[str, str]] = []   # P1-1：本 run 每步请求的 (工具名, 参数指纹)
        tool_trace: list[str] = []
        observed: list[dict] = []
        # P1-1：无进展检测的**步级**结果指纹（每步一条；None = 该步无调用）。
        # 与 observed（调用级，供收尾摘要用）并存，互不干扰。
        step_fps: list[str | None] = []
        chain_peak = 0                         # 循环内 chain 实占峰值（可观测）
        chain_clips = 0                        # 累计逐条折叠次数（可观测）
        chain_act_folds = 0                    # D2：累计 act 参数折叠次数（可观测）
        started = time.monotonic()

        reason: str | None = None
        hard = False

        while steps < cfg.max_steps:
            if time.monotonic() - started > cfg.deadline_s:
                reason = f"超时（超过 {cfg.deadline_s:.0f}s）"
                break
            steps += 1
            await ctx.events.emit(Events.REACT_STEP_START,
                                  {"step": steps, "session_id": session_id})

            # ---- 窗口构造（S3：块内准备 → 治理器装配；无治理器退降级） ----
            # L1：懒加载时本步 native tools = 元工具 + 已加载完整定义 + 放行域目录卡
            tools_step = (self._tools_for_step(tools, task, allowed, ctx)
                          if cfg.lazy_tools else tools_vis)
            window, win = self._window(ctx, task, system_prompt, head_prompt, messages,
                                      steps, tools=tools_step)
            if win:
                rt = win.get("react", {})
                chain_clips += int(rt.get("clips", 0))
                chain_act_folds += int(rt.get("act_folds", 0))
                chain_peak = max(chain_peak, int(rt.get("chain_tokens", 0)))
                rt["clips"], rt["chain_peak_tokens"] = chain_clips, chain_peak
                rt["act_folds"] = chain_act_folds

            # ---- 记账（遥测）：本步窗口装配完成即发 governed（发射点＝窗口准备处） ----
            # 位置沿革：S3 原实现挂在本步「Act + Observe 之后」（见下方 step 落库处）。
            # 前移动机：模型首步直接回答（``not out.tool_calls``）会提前 return，
            # 原位置**永不执行** ⇒ 纯聊天轮在 agent_events 里无账本，``/api/context``
            # 组织不出该轮构成。前移后「装配即成账」：多步 run 仍每步一条（条数不变），
            # 仅补齐零工具轮。无治理器时 ``win == {}``，``_emit_window`` 内守卫静默跳过。
            await self._emit_window(ctx, win)

            # D1：预算预警旗标在位 → 本步窗口注入「尽快收束」提示（每步追加，幂等；
            # 只读状态键，对 budget 零直连——与 PERMISSION_MODE 同款前向契约）
            # G2：旗标已按**会话分桶**（budget 侧），此处按本轮会话键取。
            # 形状判定：**记录**（含 ``limit`` 字段）＝改造前单值形状，直接用它；
            # **分桶**（``{会话键: 记录}``）＝G2 新形状，按本轮会话取。两类都是 dict，
            # 故不能只看类型——以「是否含记录字段」区分，旧形状同样不劣化。
            _warn = ctx.state.get(StateKeys.BUDGET_WARNING)
            if isinstance(_warn, dict) and "limit" not in _warn:
                _warn_hit = _warn.get(session_key)
            else:
                _warn_hit = _warn
            if _warn_hit:
                window.append({"role": "system", "content": BUDGET_TIGHT_SYSTEM})

            # ---- Think ----
            try:
                skill_note = "\n\n".join(task.skill_docs) if task.skill_docs else None
                # 仅当确有技能说明要注入时才带 system_note——保持与抽象 ``Reasoner`` 协议
                # （``reason(ctx, messages, tools)``）的默认契约一致，避免破坏未实现该关键字的
                # reasoner 实现（含既有测试桩）。无技能加载时等价于不传该参数。
                reason_kwargs = {"system_note": skill_note} if skill_note else {}
                out = await self._reasoner.reason(ctx, window, tools_step, **reason_kwargs)
            except CapabilityNotImplemented as exc:      # guard/预算拒绝 → 触顶收尾，不崩
                reason = f"模型调用被拒：{exc}"
                break
            except Exception as exc:                     # noqa: BLE001 —— 推理失败：诚实失败
                logger.exception("react 推理失败（step=%s，完整堆栈如下）", steps)
                reason = f"推理失败：{type(exc).__name__}: {exc}"
                hard = True
                break
            # A 重试：收敛步若 reasoning 吃满输出（truncated）导致正文为空，
            # 临时调大 max_tokens + 抑制思考重试一次，避免直接降级为空响应。
            if (not out.text) and (not out.tool_calls) and getattr(out, "truncated", False):
                retried = await self._reason_retry_once(ctx, window, tools_step)
                if retried.text:
                    out = retried
            tokens_in += int(out.tokens_in or 0)
            tokens_out += int(out.tokens_out or 0)
            turn = {"role": "assistant", "content": out.text,
                    "tool_calls": [c.as_dict() for c in out.tool_calls],
                    "type": "act" if out.tool_calls else "think",
                    "id": f"step{steps}_{'act' if out.tool_calls else 'think'}"}
            # B-fix：输出过思考的 assistant 消息回传时必须携带 reasoning_content（DeepSeek 校验）
            if getattr(out, "reasoning_content", ""):
                turn["reasoning_content"] = out.reasoning_content
            messages.append(turn)
            task.chain.append(turn)

            if not out.tool_calls:                       # ← 收敛：不再需要工具
                await durable(ctx, Events.REACT_FINAL, {
                    "steps": steps, "tokens_in": tokens_in, "tokens_out": tokens_out,
                    "at": now_iso()})
                self._record(ctx, steps, tool_trace, exhausted=False)
                # G2.0（P2-3）：**收尾存档点**治理——assess → plan →（on 时）execute →
                # 整单元替换回写链。必须在 ``_archive_chain`` 之前（存档即「下一次请求的
                # 链副本」，在此替换不引入额外重建）。
                await self._govern_at_teardown(
                    ctx, task, int(win.get("total_used", 0)),
                    system_prompt=system_prompt, head_prompt=head_prompt, steps=steps)
                await self._archive_chain(ctx, wm, session_key, task, steps,
                                          tokens_in, tokens_out)
                await self._emit_waterline(ctx, int(win.get("total_used", 0)))
                if wm is not None:
                    # 阶段 2（2.1/D1/D16）：收尾置完成标记（不删快照——供「已完成」查询/审计）。
                    # 与 wm.finish（删工作记忆键）是两条独立路径。
                    await _snapshot_call(wm, "snapshot_mark_finished", ident.tenant, task_id)
                    await wm.finish(task_id)
                set_current_run_id("")          # R1：两通道同步置空（见 run 开头注释）
                ctx.state[StateKeys.REACT_RUN_ID] = ""
                # A（空响应静默失败防护）：正文为空时不再以 ok=True 占位，
                # 而是明确判失败 + 降级提示（截断时给出针对性引导）。
                final_text = out.text
                final_ok = True
                if not out.text:
                    final_ok = False
                    if getattr(out, "truncated", False):
                        final_text = ("（模型把输出额度都用在了思考过程上，正文被截断为空。"
                                      "请在「设置 → 模型」调大 max_tokens 后重试，或换用推理较轻的模型。）")
                    else:
                        final_text = "（模型未返回任何正文，请重试。）"
                return ReactResult(ok=final_ok, text=final_text, steps=steps,
                                   tokens_in=tokens_in, tokens_out=tokens_out,
                                   tools=tool_trace, observations=observed,
                                   run_id=run_id)

            # ---- Act + Observe ----
            # 技能元工具路由（独立于 lazy_tools 开关，常驻可用）：把 skill 调用交给
            # SkillsCatalog.handle_meta，剩余调用走既有（懒加载/真实）通道。react 不
            # import skills 插件——只经 ctx.get(Services.SKILLS) 反向读取契约。
            meta_results: list[ToolResult] = []
            _skill_calls = [c for c in out.tool_calls
                            if getattr(c, "name", "") == "skill"]
            for _c in _skill_calls:
                if _cat is not None:
                    _text, _err = await _cat.handle_meta(task, _c, ctx)
                    meta_results.append(ToolResult("skill", _text, is_error=_err,
                                                   tool_call_id=getattr(_c, "id", "")))
                else:
                    meta_results.append(ToolResult(
                        "skill", "技能系统未启用", is_error=True,
                        tool_call_id=getattr(_c, "id", "")))
            real_calls = [c for c in out.tool_calls
                          if getattr(c, "name", "") != "skill"]
            # L1：懒加载模式下先拦截元工具与未加载调用（loop 内联处理，不进注册表）
            # P1-1：收集本步请求的 (工具名, 参数指纹) —— 先于一切过滤/执行，
            # 使「被拒/被放行但原地打转」都计入无进展检测（泛化连拒护栏）。
            for _c in out.tool_calls:
                call_sigs.append((getattr(_c, "name", "?"),
                                  self._arg_fp(getattr(_c, "arguments", None))))
            if cfg.lazy_tools:
                meta_results, real_calls = await self._partition_meta(
                    wm, task, real_calls, tools)
            results = list(meta_results)
            if real_calls:
                # ★ 子 Agent 执行层白名单硬拦截（v0.3 §3.4「安全下界」的兑现）：
                # tools_override 只裁剪**可见目录**（提示层软约束），模型幻觉/越权
                # 仍可能点名白名单外���具。此处按同一标识符语义（工具名或域）在**执行前**
                # 二次校验，越界 ⇒ 拒绝观察（不执行、不改 state），让模型自纠。
                # 无 tools_override ⇒ _tools_override is None ⇒ 零行为变化。
                denied_results, real_calls = self._split_whitelist_denied(ctx, real_calls)
                results.extend(denied_results)
            if real_calls:
                results.extend(await self._act_with_budget(ctx, real_calls, started, cfg))
            stop_reason: str | None = None
            for r in results:
                tool_trace.append(r.name)
                observed.append(r.as_dict())
                if r.denied:
                    # 连拒闸（防死循环）——第 3 期语义细化：``denied_by == "user"``
                    # 是「用户本轮主动拒」（拒绝理由已回灌，模型应读理由换方案），
                    # **不计连拒**；步数/时延闸仍兜底。策略类拒绝（guard/approval
                    # 名单/超时）重试无益，照旧计数。
                    if str(getattr(r, "denied_by", "") or "") == "user":
                        pass
                    else:
                        rejected[r.name] += 1
                        if rejected[r.name] > cfg.max_rejects:
                            stop_reason = (f"工具 {r.name!r} 反复被拒"
                                           f"（超过 {cfg.max_rejects} 次），终止")
            obs = dict(await self._observer.observe(ctx, results))
            obs.setdefault("type", "observe")
            obs.setdefault("id", f"step{steps}_observe")
            obs.setdefault("pinned", False)          # 通用元数据：默认 False（可由 Observer 接缝注入）
            messages.append(obs)
            task.chain.append(obs)
            await durable(ctx, Events.REACT_STEP, {
                "step": steps, "tools": [r.name for r in results],
                "is_error": any(r.is_error for r in results),
                "tokens_in": tokens_in, "tokens_out": tokens_out, "at": now_iso()})
            await ctx.events.emit(Events.REACT_STEP_END, {
                "step": steps, "tools": [r.name for r in results],
                "is_error": any(r.is_error for r in results)})
            # 注：governed 遥测已在**本步窗口准备处**发出（见循环顶部的 ``_emit_window``）——
            # 不再挂在此处，否则每步会重复记账，且零工具轮依旧漏报。
            # 工作记忆：每步进度更新 + 里程碑检查点（tool_trace 仅工具名、已限 10，体积小）
            if wm is not None:
                last = tool_trace[-1] if tool_trace else ""
                await wm.update(task_id, step=steps, tools=list(tool_trace), last_action=last)
                # D2：act 完整参数随步存档——后续老 act 折叠的「按引用找回」依据
                await wm.checkpoint(task_id, f"step_{steps}",
                                    {"tool_trace": list(tool_trace[-10:]),
                                     "last_action": last, "note": "step completed",
                                     "act_calls": [c.as_dict() for c in out.tool_calls]})
                # 阶段 2（2.1/D2/D3/D19）：跨进程 run 快照——**只在完整一轮（Observe）之后**写，
                # 记录已完成轮的 chain（截断最近 N 条）+ 头部快照；崩溃可从最近完整轮恢复。
                # 单 key 单 JSON（D3，原子）；不随 wm.finish 删除（D1）。经 _snapshot_call 容错：
                # 旧/裸 wm 实现无该方法 → 跳过（快照是可选增强能力），不拖累 run。
                await _snapshot_call(wm, "snapshot_save", ident.tenant, task_id, {
                    "chain": _snapshot_chain(task.chain, snap_keep),
                    "head": dict(task.head) if task.head else None,
                    "step": steps, "budget_used": int(win.get("total_used", 0)),
                    "status": "running", "retry_count": 0, "ts": now_iso()})
            # P1-1：无进展检测（泛化连拒护栏）—— 命中即复用既有触顶收尾，零新增分支。
            # ★ 步级指纹**无条件**采集（不放在 if 内）：否则触发连拒的那一步会被漏记，
            #   使 step_fps 比实际步数少一位、后续"连续 N 步"判定错位。
            step_fps.append(self._step_fp(results))
            if not stop_reason:
                _np = self._detect_no_progress(call_sigs, observed, cfg, step_fps)
                if _np:
                    stop_reason = _np
            if stop_reason:
                reason = stop_reason
                break
        else:
            reason = f"已达步数上限 max_steps={cfg.max_steps}"

        # ---- 触顶统一收尾：不崩、不静默，如实标注 ----
        reason = reason or f"已达步数上限 max_steps={cfg.max_steps}"
        await durable(ctx, Events.REACT_EXHAUSTED, {
            "steps": steps, "reason": reason, "tokens_in": tokens_in,
            "tokens_out": tokens_out, "at": now_iso()})
        # C（方案）：收尾摘要——过程知识不落库，run 结束即弃；至少把「做了什么/
        # 败在哪/花多少」交还给用户与长期记忆，避免当晚「第 10-12 章蒸发」式烂尾。
        summary = self._closing_summary(steps, tool_trace, observed, tokens_in, tokens_out)
        if cfg.finalize_on_exceed and not hard:
            try:
                # 摘要以系统消息随行，finalizer 与其兜底文本均可取用
                text = await self._finalizer.finalize(
                    ctx, list(window or messages) + [{"role": "system", "content": summary}],
                    reason)
            except Exception:                            # noqa: BLE001 —— 收尾失败也要有话说
                text = f"（已达执行上限：{reason}）\n{summary}"
        else:
            text = f"（已达执行上限：{reason}）\n{summary}"
        self._record(ctx, steps, tool_trace, exhausted=True, reason=reason)
        # G2.0（P2-3）：触顶收尾同样走**收尾存档点**治理（与收敛路径同一时机闸）。
        await self._govern_at_teardown(
            ctx, task, int(win.get("total_used", 0)),
            system_prompt=system_prompt, head_prompt=head_prompt, steps=steps)
        await self._archive_chain(ctx, wm, session_key, task, steps, tokens_in, tokens_out)
        await self._emit_waterline(ctx, int(win.get("total_used", 0)))
        if wm is not None:
            # 阶段 2（2.1/D1/D16）：触顶收尾同样置完成标记（不删快照）
            await _snapshot_call(wm, "snapshot_mark_finished", ident.tenant, task_id)
            await wm.finish(task_id)
        set_current_run_id("")                  # R1：两通道同步置空（见 run 开头注释）
        ctx.state[StateKeys.REACT_RUN_ID] = ""
        return ReactResult(ok=not hard, text=text, steps=steps, tokens_in=tokens_in,
                           tokens_out=tokens_out, exhausted=True, reason=reason,
                           tools=tool_trace, observations=observed, summary=summary,
                           run_id=run_id)

    # ---------- 循环内上下文（S3：块内准备 → 治理器装配） ----------

    async def _act_with_budget(self, ctx, calls, started: float,
                               cfg: "ReactConfig") -> list[ToolResult]:
        """按**剩余 deadline** 执行本步工具调用（H3）。

        为什么必须下压到单次调用：``deadline`` 只在循环顶检查，而一个卡死的工具
        让 ``await`` 永不返回 ⇒ 永远到不了那个检查点（此前是真实的"run 永久挂住"）。
        剩余时间下压 ⇒ 最坏情况下这次 act 一定在 deadline 内结束，循环顶随即触顶收尾。

        注入的 ``Actor`` 可能不认 ``timeout_s``（旧实现）⇒ 逐级回退签名（与 ``_window``
        的 ``tries`` 同款纪律）：不劣化，但也拿不到超时保护（如实）。
        """
        remaining = float(cfg.deadline_s) - (time.monotonic() - started)
        if remaining <= 0:
            remaining = None                     # 已超期：交给循环顶统一触顶，不额外收紧
        # 首选：把剩余 deadline 作为 timeout_s 下压给 Actor（新签名，逐个下压给 tasks.invoke）
        try:
            return await self._actor.act(ctx, calls, timeout_s=remaining)
        except TypeError:                        # 注入实现不认该参数 → 退回旧签名
            pass
        # 旧签名兜底（H3）：Actor 不认 timeout_s ⇒ 本层用剩余 deadline 包一层硬上限。
        # 超时**不向上抛**（抛会绕过循环顶的触顶收尾、直接中断 run）——改成回灌"超时"观察，
        # 让循环正常走到顶端的 deadline 检查再统一收尾。
        if remaining is not None:
            try:
                return await asyncio.wait_for(self._actor.act(ctx, calls), timeout=remaining)
            except asyncio.TimeoutError:
                return [ToolResult(getattr(c, "name", "?"),
                                   f"工具执行超时（超过剩余 deadline {remaining:.1f}s）",
                                   is_error=True, tool_call_id=getattr(c, "id", ""))
                        for c in calls]
        return await self._actor.act(ctx, calls)

    # ---------- P1-1：无进展检测（泛化连拒护栏，零新增收尾逻辑） ----------

    @staticmethod
    def _arg_fp(args) -> str:
        """工具调用参数的稳定指纹（无序、可 JSON 序列化）。"""
        try:
            return json.dumps(args, sort_keys=True, ensure_ascii=False)
        except TypeError:
            return repr(args)

    @staticmethod
    def _step_fp(results: list) -> str | None:
        """一步内全部工具结果的**聚合指纹**（步级，非调用级）。

        ★ 为什么必须有这一步：``observed`` 是按**工具调用**累积的（一步内 N 个
        tool_calls 就 append N 次），而启发式2 原本把它当**步**切片 ⇒ 单步内多调用
        即可凑满 ``no_progress_repeat``，实测出现 ``steps=1`` 就报"最近 3 步"的误伤。
        本方法把一步内的结果合成**一个**指纹，交由启发式2 按步比较。

        返回 ``None`` 表示"本步无可比内容"（无调用）⇒ 调用方跳过该步，
        避免空串被当成"内容相同"而误判。
        """
        parts: list[str] = []
        for r in results or ():
            name = str(getattr(r, "name", "") or "")
            content = str(getattr(r, "content", "") or "")
            # 工具名进指纹：不同工具返回同文本不算"无进展"（如两个空列表查询）。
            parts.append(f"{name}:{hash(content)}")
        return "|".join(sorted(parts)) if parts else None

    @staticmethod
    def _detect_no_progress(call_sigs: list[tuple[str, str]],
                           observed: list[dict], cfg: "ReactConfig",
                           step_fps: list[str | None] | None = None) -> str | None:
        """同 (工具, 参数) 在窗口内反复出现 ⇒ 视为无进展。

        泛化 ``run`` 内连拒护栏（``rejected[r.name] > max_rejects``）：那只对「被拒」
        触发，本方法对「反复用同一参数调用同一工具」（含被放行但原地打转）也触发。
        命中返回原因串（走既有 ``stop_reason → break → REACT_EXHAUSTED`` 诚实收尾）；
        返回 ``None`` 表示正常。

        ``step_fps``：**步级**聚合指纹序列（每步一条，见 :meth:`_step_fp`）。
        启发式2 只在该序列上判定；缺失时（未传）退回旧的调用级判据以兼容旧调用方。
        """
        if len(call_sigs) < cfg.no_progress_repeat:
            return None
        # 启发式1：窗口内同一指纹超阈值（严格同参；迭代精修因参数不同而豁免）
        recent = call_sigs[-cfg.no_progress_window:]
        counts: dict[tuple[str, str], int] = {}
        for sig in recent:
            counts[sig] = counts.get(sig, 0) + 1
        for sig, n in counts.items():
            if n >= cfg.no_progress_repeat:
                return (f"无进展检测触发：最近 {cfg.no_progress_window} 步内重复调用 "
                        f"{sig[0]!r}（同参数）{n} 次")
        # 启发式2：最近 N **步**的结果指纹相同（反复读回同一结果，未产生新信息）
        #
        # ★ 2026-10-10 真模型实测修误伤：原实现直接切 ``observed``（**调用级**），
        #   一步内多个 tool_search 就凑满阈值，报出 ``steps=1`` 的"最近 3 步"——
        #   且 ``tool_search`` 连续检索返回**高度相似的候选文本**，hash 自然相同。
        #   改为步级指纹后，单步多调用只占 1 条、不再误判；跨步重复才命中。
        if step_fps is not None:
            tail = [fp for fp in step_fps[-cfg.no_progress_repeat:] if fp is not None]
            # 要求**连续** N 步都有内容且全同（中间有 None = 空步，不算重复）
            if len(tail) == cfg.no_progress_repeat and len(set(tail)) == 1:
                return (f"无进展检测触发：最近 {cfg.no_progress_repeat} 步结果"
                        f"高度重复（未产生新信息）")
            return None
        # 兼容路径：未传 step_fps（保留旧语义，仅供旧调用方）
        if len(observed) >= cfg.no_progress_repeat:
            tail = observed[-cfg.no_progress_repeat:]
            hashes = {hash(str(o.get("content", ""))) for o in tail}
            if len(hashes) == 1:
                return (f"无进展检测触发：最近 {cfg.no_progress_repeat} 步观察结果"
                        f"高度重复（未产生新信息）")
        return None

    def _prepare_chain(self, gov, chain: list[dict], keep_recent: int, *,
                       wm=None, task_id: str = "",
                       act_fold: int = 0) -> tuple[list[dict], int, int]:
        """**块内准备：只折叠、不丢弃**（与治理器 `_trim_messages` 的丢弃职责不重叠）。

        - 最近 ``keep_recent`` 条**原样保留**；
        - 更老者中 ``type == "observe"`` 且**未 pinned** 的条目 → 逐条首尾折叠（就地生效，幂等）；
        - D2：更老者中 ``type == "act"`` 的条目 → **大参数值折叠**（单值 token 超
          ``act_fold`` 的字符串参数替换为引用标记；``path``/``mode`` 等小参数原样保留）。
          **无损承诺**：完整原文在 act 发生时已存工作记忆 checkpoint（见 ``run``），
          故仅在 ``wm`` 在位且 ``act_fold > 0`` 时启用——不可找回就不折，绝不静默丢真；
        - ``think`` / 其余 **一律保留**——关键信息通常在 Think 里已转录；
        - **不丢任何条目**：超预算后的丢弃由治理器统一执行（避免双重截断）。

        返回 ``(本步待装配的 chain, observe 折叠数, act 参数折叠数)``。
        """
        clip_fn = getattr(gov, "clip_head_tail", None)
        if not callable(clip_fn):
            return list(chain), 0, 0
        cut = max(0, len(chain) - max(0, int(keep_recent)))
        newly = 0
        act_folds = 0
        for i, m in enumerate(chain):
            if i >= cut or m.get("pinned"):
                continue
            mtype = m.get("type")
            if mtype == "observe":
                content = m.get("content")
                folded, changed = self._fold_content(content, clip_fn)
                if changed:
                    m["content"] = folded
                    newly += 1
            elif mtype == "act" and wm is not None and act_fold > 0:
                act_folds += self._fold_act(m, act_fold, task_id)
        return list(chain), newly, act_folds

    @staticmethod
    def _fold_act(m: dict, act_fold: int, task_id: str) -> int:
        """D2：折叠一条 act 条目里的**大字符串参数值**（就地，幂等）。

        只折叠超阈值的字符串值（章节全文等）；``path``/``mode`` 等结构参数保留，
        模型仍能理解「这一步对哪个文件做了什么」。折叠标记引用工作记忆 checkpoint
        键（由条目 id ``step{N}_act`` 反解出 ``step_{N}``——与 run() 的存档键一致）。
        返回本次新折叠的参数个数。
        """
        calls = m.get("tool_calls")
        if not isinstance(calls, list):
            return 0
        sid = str(m.get("id") or "")
        step_key = f"step_{sid[4:].split('_', 1)[0]}" if sid.startswith("step") else sid
        folded = 0
        for tc in calls:
            if not isinstance(tc, dict):
                continue
            args = tc.get("arguments")
            if not isinstance(args, dict):
                continue
            new_args: dict = {}
            changed = False
            for k, v in args.items():
                if isinstance(v, str):
                    est = estimate_tokens(v)
                    if est > act_fold and not v.startswith("«[已折叠"):
                        new_args[k] = (f"«[已折叠·约{est} tokens] 完整原文见工作记忆 "
                                       f"checkpoint {task_id}·{step_key}»")
                        changed = True
                        continue
                new_args[k] = v
            if changed:
                tc["arguments"] = new_args
                folded += 1
        return folded

    @staticmethod
    def _fold_content(content, clip_fn) -> tuple[Any, bool]:
        """对条目内容做首尾折叠（**保结构**）：观察内容是 ``{"tool_results": [...]}`` 时，
        逐条折叠其 ``content`` 字段——**不把整个 dict 序列化成字符串**（否则 role=tool
        配对会被破坏）。纯字符串内容直接折叠。"""
        if isinstance(content, str):
            return clip_fn(content)
        if isinstance(content, dict):
            trs = content.get("tool_results")
            if isinstance(trs, list):
                changed = False
                new: list[Any] = []
                for tr in trs:
                    if isinstance(tr, dict) and isinstance(tr.get("content"), str):
                        text, cut = clip_fn(tr["content"])
                        if cut:
                            tr = {**tr, "content": text}
                            changed = True
                    new.append(tr)
                if changed:
                    return {**content, "tool_results": new}, True
        return content, False

    def _loop_blocks(self, task: TaskContext, system_prompt: str, head_prompt: str,
                     prepared: list[dict]) -> list[dict]:
        """本端点的块清单（**装配与收尾共用**——两处口径天然一致，杜绝双份真相）。

        G2.0（P1-4）层标注（层决定缺省 cap，§4.4）：

        ====================  =========  =====================================
        块                     layer      说明
        ====================  =========  =====================================
        ``react_system``      resident   系统提示（cap 8000）
        ``react_prior``       memory     会话前置上下文（无 cap）
        ``react_prompt``      input      本轮用户输入（cap 2000；volatile）
        ``react_chain``       runtime    ReAct 轨迹（无 cap；唯一可裁块）
        ====================  =========  =====================================

        G2.0（P2）：``react_chain`` 挂 ``on_compact``（过程摘要生成器）——``plan`` 的
        compact 候选。

        **P2-4 偏差（见方案 §6.1）**：原设计的「工作记忆目录块」（内存层 cap 300）**不入线**——
        目录按 run 变化（检查点属任务级），任何窗口内位置都会打断 F-1「上一轮请求为本轮请求
        严格前缀」这一旗舰不变量；改由「``wm_read`` 读回工具 + D2 折叠引用逐条给出精确
        checkpoint 键」承担等价可发现性，**零前缀代价**。
        """
        blocks: list[dict] = [
            {"key": "react_system", "kind": "text", "present": True,
             "payload": system_prompt, "rigid": True, "priority": -10,
             "layer": "resident"},
            {"key": "react_prior", "kind": "messages", "present": bool(task.prior),
             "payload": task.prior, "rigid": True, "priority": 0, "coverage": (0, 1),
             "layer": "memory"},
            {"key": "react_prompt", "kind": "messages", "present": True,
             "payload": [{"role": "user", "content": head_prompt}], "rigid": True,
             "priority": 5, "coverage": (1, 2), "volatile": True, "layer": "input"},
            {"key": "react_chain", "kind": "messages", "present": bool(prepared),
             "payload": prepared, "weight": 1.0, "priority": 10, "coverage": (2, 3),
             "layer": "runtime", "volatile": False,
             "on_compact": self._chain_compactor()},
        ]
        return blocks

    def _chain_compactor(self):
        """``on_compact`` 回调（消费方注入）：把被丢弃的一段链**压成过程摘要**。

        纪律：治理器只调用、不理解其内部（纯度）；本回调为**确定性**生成（不额外调 LLM，
        故收尾零成本、零 400 风险）。语义上满足 U-2「整单元替换为过程摘要」，且对带 tools
        的形态覆盖三成员信息（调用意图 / 参数要点 / 结果要点）——即 X-2 的合法性判据。
        """
        def _on_compact(messages, target_tokens):        # noqa: ARG001 —— 目标量由治理器掌控
            return self._summarize_units(messages)
        return _on_compact

    @staticmethod
    def _summarize_units(messages: list[dict]) -> str:
        """把若干条链消息压成一段**过程摘要**（保留结论与关键产物，丢弃原始字节）。"""
        lines: list[str] = []
        for m in messages:
            content = m.get("content")
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m.get("tool_calls") or []:
                    lines.append(f"调用 {tc.get('name')}（参数要点：{_brief(tc.get('arguments'))}）")
            elif isinstance(content, dict) and content.get("tool_results") is not None:
                for r in content.get("tool_results") or []:
                    flag = "失败 " if r.get("is_error") else ""
                    lines.append(f"{flag}结果（{r.get('name')}）：{_brief(r.get('content'))}")
        body = "；".join(lines) if lines else "（无工具调用）"
        return f"[过程摘要] 早前 {len(messages)} 条链路已压缩：{body}"

    def _window(self, ctx, task: TaskContext, system_prompt: str, prompt: str,
                messages: list[dict], step: int,
                tools: list[dict] | None = None) -> tuple[list[dict], dict]:
        """构造喂给模型的窗口（G1 装配；无治理器 → 退回旧 ``_trim`` 条数裁剪，行为不变）。

        块列表（注册制）：system / prior（rigid）/ 本轮 prompt（rigid）/ chain（可裁）。
        prior 与 prompt 为**刚性块**——循环内不参与常规裁剪（自身超预算时治理器仍会
        按刚性降级规则诚实标注，不静默）。

        G2.0（P0）：
        - ``prompt`` 入参为**头部快照里的那一份**（续接时是任务原文，不是「继续」）；
          它标 ``volatile=True``（跨 run 内容会变），供治理器算稳定前缀体检指标；
        - native tools 数组作为**请求附加项**显式登记（此前「扣预算但不入账」的黑洞）；
        - 装配前注入**声明**（本阶段应有块清单），使漏报可被结构性发现。
        """
        gov = self._resolve_governance(ctx)
        fn = getattr(gov, "assemble", None) if gov is not None else None
        if not callable(fn):
            return _trim(messages, self._cfg.history_keep), {}
        _declare_loop_blocks(gov)
        # D2：工作记忆接缝（act 原文存档的找回面；缺失则 act 折叠自动关闭——无损承诺）
        wm = ctx.get(Services.WORK_MEMORY) if ctx.has(Services.WORK_MEMORY) else None
        keep_recent = _gov_int(gov, "react_keep_recent", self._cfg.react_keep_recent)
        prepared, newly, act_folds = self._prepare_chain(
            gov, task.chain, keep_recent, wm=wm, task_id=task.task_id,
            act_fold=self._cfg.act_fold_tokens)
        # G2.0（P1-4/P2）：块清单（含层标注 + on_compact）由 ``_loop_blocks`` 统一产出——
        # 装配与收尾共用同一口径，杜绝双份真相。
        blocks = self._loop_blocks(task, system_prompt, prompt, prepared)
        try:
            # S3（彻底版·统一账本）：native tools 数组是请求附加项、不在 messages 预算内，
            # 经 assemble(reserve_tokens=) 先扣其占用，使窗口总占用（messages + native tools）
            # 对齐真实 prompt_tokens，根治「窗口预算不认 tools」的结构性隐患。
            # F1（彻底作用）：预算整体除以校准因子——真实计费高于估算时窗口装得更少，
            # 使真实请求体积贴近窗口预算（因子读 api.cost_factor 状态键，治理器自身无 ctx）。
            # G2.0（P0）：同一占用再以 attachments 显式**登记**（只记账：有效预留取较大值，
            # 故预算裁决与此前逐字节一致；登记使 unaccounted 归因成为可能）。
            from agent.api import cost_factor as _cf
            factor = _cf(ctx)
            tools_tokens = estimate_request_cost(messages=[], tools=tools)
            attach = ([{"key": "native_tools", "claimed_tokens": int(tools_tokens)}]
                      if tools_tokens else [])
            tries = ({"reserve_tokens": tools_tokens, "cost_factor": factor,
                      "attachments": attach, "phase": "react"},
                     {"reserve_tokens": tools_tokens, "cost_factor": factor},
                     {"reserve_tokens": tools_tokens},
                     {})
            asm = None
            for kwargs in tries:
                try:
                    asm = fn(blocks, **kwargs)
                    break
                except TypeError:            # 注入实现不认新参数 → 退回更旧签名（不劣化）
                    continue
            if asm is None:
                raise TypeError("assemble 不接受任一已知签名")
        except Exception as exc:                     # noqa: BLE001 —— 装配失败退降级，绝不裸崩
            logger.warning("react 窗口装配失败，退回条数裁剪：%s", exc)
            return _trim(messages, self._cfg.history_keep), {}
        window = [{"role": "system", "content": asm.system_text}] + list(asm.prior)
        return window, self._win_stats(asm, prepared, newly, step, keep_recent,
                                       act_folds=act_folds)

    @staticmethod
    def _win_stats(asm, prepared: list[dict], newly_clipped: int, step: int,
                   keep_recent: int, *, act_folds: int = 0) -> dict:
        """装配统计（遥测用；字段名与 S3 设计 §3.6.6 一致）。"""
        rep = getattr(asm, "report", None)
        blocks: list[dict] = []
        chain_tokens = chain_alloc = downgraded = 0
        for r in getattr(rep, "blocks", []):
            blocks.append({"key": r.key, "present": r.present, "reason": r.reason,
                           "requested": r.requested, "allocated": r.allocated, "used": r.used,
                           "layer": getattr(r, "layer", ""), "cap": getattr(r, "cap", 0)})
            if r.key == "react_chain":
                chain_tokens = int(r.used)
                chain_alloc = int(r.allocated)
                downgraded = len(getattr(r, "pinned_downgraded", []) or [])
        return {
            "step": step,
            "input_budget": int(getattr(rep, "input_budget", 0)),
            "budget_raw": int(getattr(rep, "budget_raw", 0)),      # P3：I-4 对照基数
            "total_used": int(getattr(rep, "total_used", 0)),
            "blocks": blocks,
            "rigid_overflow": [r.key for r in getattr(rep, "rigid_overflow", [])],
            # ---- G2.0（P0）只读观测：记账与缓存友好度（旧治理器无这些字段 → 恒 0/空）----
            "unaccounted_tokens": int(getattr(rep, "unaccounted_tokens", 0)),
            "stable_prefix_tokens": int(getattr(rep, "stable_prefix_tokens", 0)),
            "volatile_blocks": list(getattr(rep, "volatile_blocks", []) or []),
            "missing_blocks": list(getattr(rep, "missing_blocks", []) or []),
            "attachments": [
                {"key": a.key, "claimed": int(a.claimed), "used": int(a.used)}
                for a in getattr(rep, "attachments", []) or []
            ],
            "react": {
                "chain_tokens": chain_tokens,
                "chain_peak_tokens": chain_tokens,       # 由 run() 累计为任务内峰值
                "clips": newly_clipped,                  # 由 run() 累计
                "act_folds": int(act_folds),             # D2：act 大参数折叠数（由 run() 累计）
                "pinned": sum(1 for m in prepared if m.get("pinned")),
                "pinned_downgraded": downgraded,
                "keep_recent": int(keep_recent),
                "budget": chain_alloc,
            },
        }

    async def _emit_window(self, ctx, stats: dict) -> None:
        """发 ``agent/context/governed``（``stage="react"``）遥测；失败绝不阻断循环。

        遥测落库（一期）：此前仅 live emit、不落 agent_events——当晚诊断只能靠
        tokens_in 曲线反推裁剪决策。改为 durable（可跨重启回放）+ live 双通道。
        """
        if not stats:
            return
        try:
            await durable(ctx, Events.CONTEXT_GOVERNED,
                          {"stage": "react", "at": now_iso(), **stats})
        except Exception as exc:                     # noqa: BLE001 —— 遥测失败不阻断
            logger.warning("react context governed durable failed: %s", exc)
        try:
            await ctx.events.emit(Events.CONTEXT_GOVERNED, {
                "stage": "react", "at": now_iso(), **stats})
        except Exception as exc:                     # noqa: BLE001 —— 遥测失败不阻断
            logger.warning("react context governed emit failed: %s", exc)
        # ---- G2.0（P3-2）：装配后**不变量断言**（I-3 前缀可测 / I-4 预算守恒）----
        # 记录 + 告警（不阻断）；记账面判定经治理器门面**软调用**（不 import 插件内部）。
        gov = self._resolve_governance(ctx)
        inv_fn = getattr(gov, "check_invariants", None)
        if callable(inv_fn):
            payload = {"stage": "react", **stats}
            try:
                res = inv_fn(payload)
            except Exception as exc:                 # noqa: BLE001 —— 断言失败不阻断
                logger.warning("react invariant check failed: %s", exc)
                return
            bad = [k for k, ok in (res or {}).items() if not ok]
            if bad:
                logger.warning("react invariant violation: %s", bad)
                try:
                    await ctx.events.emit(Events.CONTEXT_INVARIANT_VIOLATION, {
                        "stage": "react", "invariants": bad,
                        "detail": {"unaccounted_tokens": stats.get("unaccounted_tokens"),
                                   "budget_raw": stats.get("budget_raw"),
                                   "stable_prefix_tokens": stats.get("stable_prefix_tokens"),
                                   "volatile_blocks": stats.get("volatile_blocks")}})
                except Exception as exc:             # noqa: BLE001 —— 告警失败不阻断
                    logger.warning("react invariant emit failed: %s", exc)

    async def _emit_waterline(self, ctx, used: int, *,
                              phase: str = "run_teardown",
                              remaining_steps: int | None = None) -> None:
        """发 ``agent/context/waterline``（P1-3：run 收尾存档点的**水位体检**）。

        纪律（纯观测 + 零状态）：`assess` 是纯函数（同样入参恒得同样结果），本方法只做
        「取治理器 → 判水位 → 发遥测」，**不写入任何状态**；治理器缺失即静默跳过（软依赖）。
        执行点＝run 收尾存档点（§4.5 ②；与链压缩⑤同一时机闸：一 run 一次）。
        """
        gov = self._resolve_governance(ctx)
        assess = getattr(gov, "assess", None)
        if not callable(assess):
            return
        try:
            rep = assess(int(used), phase=phase, remaining_steps=remaining_steps)
        except Exception as exc:                     # noqa: BLE001 —— 体检失败不阻断
            logger.warning("react waterline assess failed: %s", exc)
            return
        payload = {"stage": "react", "phase": phase, "at": now_iso(),
                   "used": rep.used, "window": rep.working_window,
                   "capability": rep.capability_window, "ratio": rep.load_ratio,
                   "level": rep.level, "suggested": rep.suggested, "gates": rep.gates}
        try:
            await durable(ctx, Events.CONTEXT_WATERLINE, payload)
        except Exception as exc:                     # noqa: BLE001 —— 遥测失败不阻断
            logger.warning("react waterline durable failed: %s", exc)
        try:
            await ctx.events.emit(Events.CONTEXT_WATERLINE, payload)
        except Exception as exc:                     # noqa: BLE001 —— 遥测失败不阻断
            logger.warning("react waterline emit failed: %s", exc)

    async def _reason_retry_once(self, ctx, window: list[dict],
                                 tools_step: list[dict] | None):
        """A 重试：正文被 reasoning 截断时，调大 max_tokens + 抑制思考重试一次。

        仅在收敛步（无 tool_calls）正文为空且 truncated 时调用；调用方只在拿到正文后才替换。
        单次覆盖 max_tokens（不改动全局配置），并注入抑制思考的 system note 降低 reasoning 占比。
        """
        llm_cfg = (((ctx.config or {}).get("agent") or {}).get("llm") or {})
        base = int(llm_cfg.get("max_tokens", 8192) or 0)
        big = min(max(base * 2, 16384), 65536) if base > 0 else 16384
        note = ("请直接给出最终答复，不要展开思考过程或推理链；只输出答案本身，"
                "避免把输出额度消耗在内部推理上。")
        return await self._reasoner.reason(ctx, window, tools_step,
                                          max_tokens=big, system_note=note)

    async def _govern_at_teardown(self, ctx, task: TaskContext, used: int, *,
                                  system_prompt: str, head_prompt: str,
                                  steps: int) -> None:
        """**run 收尾存档点**：assess → plan →（``on`` 时）execute → 回写链。

        - 执行点＝**收尾存档点**（§4.5 ⑤；与 ``_emit_waterline`` 同一时机闸：一 run 一次）。
          时机属消费方语义——治理器只提供 ``plan``/``execute`` 原语与不变量校验；
        - ``shadow``（缺省）：``plan`` 照常计算，``execute`` 不执行；决策与理由写
          ``CONTEXT_COMPACT_DECIDED``（**shadow 数据的唯一来源**，供阈值校准）；
        - ``on``：执行并写 ``CONTEXT_COMPACT_APPLIED``；不变量被违反时补发
          ``CONTEXT_INVARIANT_VIOLATION``；链被替换才回写 ``task.chain``；
        - 全程**不阻断收尾**（任何异常吞掉并记日志）——治理失败不得影响用户的答复交付。
        """
        gov = self._resolve_governance(ctx)
        plan_fn = getattr(gov, "plan", None)
        if not callable(plan_fn):
            return
        try:
            mode = _gov_str(gov, "compact_mode", "shadow")
            report = gov.assess(int(used), phase="run_teardown", remaining_steps=steps)
            blocks = self._loop_blocks(task, system_prompt, head_prompt, list(task.chain))
            actions = plan_fn(report, blocks=blocks, mode=mode)
            await durable(ctx, Events.CONTEXT_COMPACT_DECIDED, {
                "stage": "react", "mode": mode, "shadow": mode != "on",
                "level": report.level, "used": report.used,
                "window": report.working_window, "gates": report.gates,
                "actions": [{"kind": a.kind, "target": a.target,
                             "expected_release": a.expected_release,
                             "reason": a.reason} for a in actions],
                "at": now_iso()})
            if mode != "on":
                return
            exec_fn = getattr(gov, "execute", None)
            if not callable(exec_fn):
                return
            new_blocks, res = exec_fn(actions, blocks, mode=mode)
            await durable(ctx, Events.CONTEXT_COMPACT_APPLIED, {
                "stage": "react", "applied": list(res.applied),
                "skipped": [list(x) for x in res.skipped], "released": res.released,
                "invariants": dict(res.invariants), "degraded": res.degraded,
                "at": now_iso()})
            for key, ok in (res.invariants or {}).items():
                if ok:
                    continue
                await durable(ctx, Events.CONTEXT_INVARIANT_VIOLATION, {
                    "stage": "react", "invariant": key, "target": "react_chain",
                    "detail": {"skipped": [list(x) for x in res.skipped],
                               "degraded": res.degraded}, "at": now_iso()})
            new_chain = next((b.payload for b in new_blocks
                              if b.key == "react_chain"), None)
            if isinstance(new_chain, list) and new_chain != list(task.chain):
                task.chain[:] = list(new_chain)          # 仅「已替换」才回写（身份判定）
        except Exception as exc:                         # noqa: BLE001 —— 治理失败不阻断收尾
            logger.warning("react teardown governance failed: %s", exc)

    @staticmethod
    def _closing_summary(steps: int, tool_trace: list[str], observed: list[dict],
                         tokens_in: int, tokens_out: int) -> str:
        """C：组装收尾摘要（诚实交代：做了什么 / 败在哪 / 花多少）。"""
        s = (f"[收尾摘要] 本次运行已执行 {steps} 步；工具调用 {len(tool_trace)} 次"
             f"（{', '.join(tool_trace[-8:]) or '无'}）；"
             f"tokens 累计 in={tokens_in}/out={tokens_out}。")
        last = next((o for o in reversed(observed) if o.get("is_error")), None)
        if last:
            s += (f" 最后一次失败：{last.get('name')}："
                  f"{str(last.get('content') or '')[:200]}")
        else:
            s += " 无失败记录，但未形成最终答复。"
        return s

    # ---------- L1：懒加载（目录卡 / 元工具拦截 / 按需装载） ----------

    def _split_whitelist_denied(self, ctx, calls) -> tuple[list[ToolResult], list]:
        """按 ``tools_override`` 白名单拆分调用：越界者拒绝放行，合法者交回继续执行。

        为什么执行层还要再拦一道（v0.3 §3.4「role.tools 是安全下界」的兑现）：
        - ``resolve_tools`` 只把白名单作用于**可见目录**（喂给模型的 tools 数组），
          属提示层**软约束**——模型幻觉、被prompt 注入、或工具名被伪造时仍可能点名
          白名单外的工具；
        - ``sys.guard`` 的``task_allow``/``task_deny`` 是**配置驱动**的全局策略，
          无法表达「仅本子循环内生效」的动态白名单（那必须按 asyncio task 隔离）。

        语义与 ``_apply_whitelist`` 完全一致（工具名或 ``owner`` 域命中即放行），
        保证「目录里看得见」与「执行时放得过」是同一套口径，不会出现
        「提示说不能用、实际却调得动」的裂缝。

        ``self._tools_override`` 为 None / 含 ``"*"``（且无排除项）⇒ 原样返回，
        **零行为变化**（父循环与不委派的主路径完全不受影响）。
        """
        wl = self._tools_override
        if not wl:
            return [], list(calls)
        pos, neg = _split_whitelist_ids(wl)
        if not neg and "*" in pos:
            return [], list(calls)
        if not pos:
            pos = {"*"}
        ids = pos
        catalog = {t.get("name"): t for t in ctx.tasks.catalog()}
        denied: list[ToolResult] = []
        allowed: list = []
        for c in calls:
            t = catalog.get(c.name) or {}
            dom = _domain_of(t)
            if c.name in neg or dom in neg:
                denied.append(ToolResult(
                    c.name,
                    f"工具 {c.name!r} 已被本次委派**显式排除**（子 Agent 是叶子节点，"
                    f"不得再委派/使用该工具）。请改用其它工具完成子任务，"
                    f"或把这一步交回父 Agent。",
                    is_error=True, denied=True, denied_by="whitelist",
                    tool_call_id=getattr(c, "id", "") or ""))
            elif "*" in ids or c.name in ids or dom in ids:
                allowed.append(c)
            else:
                denied.append(ToolResult(
                    c.name,
                    f"工具 {c.name!r} 不在本次委派的允许范围内"
                    f"（允许：{sorted(ids)}）。请改用允许范围内的工具，"
                    f"或换个思路完成子任务。",
                    is_error=True, denied=True, denied_by="whitelist",
                    tool_call_id=getattr(c, "id", "") or ""))
        return denied, allowed

    @staticmethod
    def _tools_for_step(tools: list[dict], task: TaskContext,
                        allowed: set[str] | None = None, ctx: object = None) -> list[dict]:
        """本步 native tools：元工具 + 已加载完整定义 + **放行域**的目录卡。

        放行条件（《治理方案》§3.3）：``name in task.loaded``（已加载 ⇒ 完整定义，
        **跨域放行**——``tool_load`` 成功即自动解禁，无需另写「域解禁」逻辑）
        **或** ``域 ∈ allowed``（放行域 ⇒ 目录卡）。二者之外的工具**不出现**。

        ``allowed is None`` ⇒ 全放行（与改造前**逐字节等价**）。
        隐藏域的工具虽然不在此处出现，但 ``_partition_meta`` 仍用**全量** ``tools``
        做 ``tool_search`` / ``tool_load`` ⇒ 「对模型不可见、对检索可见」（G3）。

        ``ctx``（可选）：传入时把技能常驻元工具（``Services.SKILLS.meta_tools``）
        前置进本步工具集——确保 skill 工具在懒加载模式下同样默认可见。
        """
        out: list[dict] = [dict(m) for m in LAZY_META_TOOLS]
        # 技能常驻元工具（独立于 lazy_tools，常驻可见）
        if ctx is not None:
            _cat = ctx.get(Services.SKILLS, None)
            if _cat is not None:
                out = [dict(m) for m in _cat.meta_tools()] + out
        for t in tools:
            if t.get("name") in task.loaded:
                out.append(t)
            elif allowed is None or _domain_of(t) in allowed:
                out.append(_card(t))
        return out

    async def _partition_meta(self, wm, task: TaskContext, calls, tools: list[dict]
                              ) -> tuple[list[ToolResult], list]:
        """拆分本轮调用：元工具（内联处理）+ 未加载（错误观察可自纠）+ 真实调用。

        ``wm_read``（P2-4）需要 ``wm`` 接缝做异步读回，故本方法为 async；``tool_search`` /
        ``tool_load`` 仍是纯内联（不进 ``ctx.tasks`` 注册表，``TaskContext.loaded`` 是唯一真相）。
        """
        meta: list[ToolResult] = []
        real = []
        catalog = {t.get("name"): t for t in tools}
        for c in calls:
            if c.name == "tool_search":
                meta.append(self._tool_search(c, tools))
            elif c.name == "tool_load":
                meta.append(self._tool_load(task, c, catalog))
            elif c.name == "wm_read" and wm is not None:
                meta.append(await self._wm_read(wm, task, c))
            elif c.name not in task.loaded:
                meta.append(ToolResult(
                    c.name,
                    f"工具 {c.name!r} 尚未加载完整定义（懒加载模式）。"
                    f"请先调用 tool_load(names=...) 加载后再调用，"
                    f"或用 tool_search(keyword=...) 查看可用工具目录。",
                    is_error=True, tool_call_id=c.id))
            else:
                real.append(c)
        return meta, real

    @staticmethod
    def _tool_search(c, tools: list[dict]) -> ToolResult:
        """tool_search：按关键词过滤目录，返回目录卡文本（摘要，非 schema）。"""
        kw = str((c.arguments or {}).get("keyword") or "").strip().lower()
        hits = [t for t in tools
                if not kw or kw in str(t.get("name") or "").lower()
                or kw in str(t.get("description") or "").lower()]
        lines = [f"- {t.get('name')}（{t.get('risk', 'write')}）："
                 f"{_slim_description(t.get('description'))}；参数 "
                 f"{render_params(t.get('params'))}" for t in hits[:20]]
        body = "\n".join(lines) if lines else "（无匹配工具）"
        return ToolResult("tool_search", f"可用工具 {len(hits)} 个：\n{body}",
                          tool_call_id=c.id)

    def _tool_load(self, task: TaskContext, c, catalog: dict) -> ToolResult:
        """tool_load：把工具完整定义加入本 run 的 loaded 集（上限 lazy_tools_max，诚实拒绝）。"""
        raw = (c.arguments or {}).get("names") or ""
        names = ([str(x).strip() for x in raw.split(",")]
                 if isinstance(raw, str) else [str(x) for x in raw])
        names = [n for n in names if n]
        unknown = [n for n in names if n not in catalog]
        wanted = [n for n in names if n in catalog]
        cap = max(0, int(self._cfg.lazy_tools_max))
        room = cap - len(task.loaded)
        loaded, deferred = wanted[:max(0, room)], wanted[max(0, room):]
        task.loaded.update(loaded)
        parts = [f"已加载 {len(loaded)} 个：{', '.join(loaded) or '无'}"]
        if deferred:
            parts.append(f"超出加载上限（{cap}）暂缓：{', '.join(deferred)}"
                         f"——请先 tool_load 卸载优先级低者或分批调用")
        if unknown:
            parts.append(f"未知工具名忽略：{', '.join(unknown)}")
        return ToolResult("tool_load", "；".join(parts), tool_call_id=c.id)

    @staticmethod
    async def _wm_read(wm, task: TaskContext, c) -> ToolResult:
        """``wm_read``：按检查点名读回本任务工作记忆里的**完整原文**（渐进披露读回闭环）。

        找不到 → 错误观察并列出可用检查点（模型据此自纠，与 ``tool_load`` 同款诚实拒绝）；
        超大内容做**源头护栏式**上限（超限如实标注，不静默截断语义）。
        """
        key = str((c.arguments or {}).get("key") or "").strip()
        try:
            data = await wm.get(task.task_id)
            cps = json.loads((data or {}).get("checkpoints") or "[]")
        except Exception as exc:                         # noqa: BLE001 —— 读回失败诚实返回
            return ToolResult("wm_read", f"读取工作记忆失败：{type(exc).__name__}",
                              is_error=True, tool_call_id=c.id)
        hit = next((x for x in reversed(cps)
                    if isinstance(x, dict) and str(x.get("name")) == key), None)
        if hit is None:
            names = "、".join(str(x.get("name")) for x in cps if isinstance(x, dict)) or "（无）"
            return ToolResult("wm_read", f"未找到检查点 {key!r}；当前可用：{names}",
                              is_error=True, tool_call_id=c.id)
        text = json.dumps(hit.get("data"), ensure_ascii=False)
        return ToolResult("wm_read", text[:20000], tool_call_id=c.id)

    # ---------- P1：链存档（「继续」低成本续接的依据） ----------

    @staticmethod
    async def _archive_chain(ctx, wm, session_id: str, task: TaskContext,
                             steps: int, tokens_in: int, tokens_out: int) -> None:
        """run 收尾把已治理折叠链存工作记忆（键 react_chain:{session}，不 finish、靠 TTL）。

        与一期 progress 摘要互补：摘要是给长期记忆的**结论**，链存档是给工作记忆的
        **过程**。存档本身已过 `_prepare_chain` 折叠治理（天然有界）。

        G2.0（P0/F-1）：连同**头部快照** ``task.head`` 一起存档——下次「继续」原样复用
        这三块字节，续接请求的第一个差异点因此落在链尾（追加）而非头部（改写）。
        """
        if wm is None or not task.chain:
            return
        try:
            arch_id = f"react_chain:{session_id}"
            await wm.begin(arch_id, {"session_id": session_id, "run_id": "chain",
                                     "max_steps": 0})
            await wm.checkpoint(arch_id, "chain", {
                "chain": [dict(m) for m in task.chain], "steps": steps,
                "head": dict(task.head) if task.head else None,
                "tokens_in": tokens_in, "tokens_out": tokens_out, "at": now_iso()})
            await durable(ctx, Events.REACT_ARCHIVED, {
                "session_id": session_id, "entries": len(task.chain), "steps": steps,
                "at": now_iso()})
        except Exception as exc:                 # noqa: BLE001 —— 存档失败不阻断收尾
            logger.warning("react chain archive failed: %s", exc)

    # ---------- 诊断（status() 钩子取用） ----------

    def _record(self, ctx, steps: int, tools: list[str], *, exhausted: bool,
                reason: str | None = None) -> None:
        try:
            ctx.state[StateKeys.REACT_LAST] = {"steps": steps, "tools": list(tools[-10:]),
                                               "exhausted": exhausted, "reason": reason,
                                               "at": now_iso()}
        except Exception:                                # noqa: BLE001 —— 诊断不阻断主链路
            pass


# ---- 阶段 2（2.2）：跨进程 run 快照辅助 -----------------------------------------
# 身份的**退化**逻辑在内核（``core.session.coerce_identity``），本模块只消费。


def _snapshot_chain(chain: list[dict], keep: int) -> list[dict]:
    """D19：快照内 chain 只保留**最近 keep 条**（防单 key 膨胀）。

    头部上下文由快照的 ``head`` 字段单独保存，续接时二者合并恢复；中间老条可截（取舍见方案 §3 D19）。
    """
    if keep <= 0 or len(chain) <= keep:
        return [dict(m) for m in chain]
    return [dict(m) for m in chain[-keep:]]


async def _snapshot_call(wm, method: str, *args) -> None:
    """容错调用 ``wm`` 的快照方法（阶段 2）。

    wm 可能是**不支持快照的旧/裸实现**（无该方法）→ 跳过，不拖累 run（快照是可选增强能力）；
    生产门面 ``WorkMemory`` 自带"吞异常仅告警"，故此处仅做方法存在性守卫，不重复 try/except。
    """
    fn = getattr(wm, method, None)
    if fn is not None:
        await fn(*args)


__all__ = ["ReactConfig", "ReactResult", "ReactLoop", "TaskContext", "visible_tools"]
