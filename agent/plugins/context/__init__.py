"""插件包：context —— 上下文治理（G0 骨架）。

目录插件（agent/plugins/context），语义上是**系统组件**（layer=system，
与 guard/budget 同级）；来源（目录）决定生命周期——经「插件」页可启用 / 停用 / 卸载。

职责（分阶段落地，见 docs/governance/上下文治理-设计方案.md）：
  G0（已完成）：预算模型 + token 化历史收窗；服务键 ``Services.CONTEXT``；
              接缝 ``ctx.get("context.governor") or ContextGovernor(...)`` 可注入替换。
  G1（已完成）：**分块装配统一编排** ``assemble(blocks)``——注册制（块自声明存在性）+
              统一互斥（左开右闭 coverage）+ 刚性超限双通道标注 + 剩余回吐（water-filling）；
              chat 的 plan / narrate / prior 均经此装配。
  S3/G2（已完成）：**循环内（react）上下文治理**——循环预算口径
              （``react_budget`` / ``observe_*`` / ``react_keep_recent`` / ``react_min_chain_tokens``）
              + 块内准备原语 ``clip_head_tail``（只折叠不丢弃）+ messages 块**通用** ``pinned``
              条目支持（块内先占、额度不足者降级并记入 ``BlockReport.pinned_downgraded``）。
  G3–G5：MCP 源头护栏 / 记忆注入配额反推 / 工具块降载。

职责边界（S3 钉死）：本插件是**全仓唯一持有 token 语义**组件——只回答「内容怎么裁、占多少 token」，
**不感知业务语义**（不认识"任务进度"，也不认识 ``observe`` 之类字眼；``pinned`` 是通用元数据）。
任务进度态归 ``plugins.work_memory``，循环编排归 ``plugins.react``。

解耦纪律：
  - 消费面一律 ``ctx.has(Services.CONTEXT)`` 软依赖——插件停用即退回消费方现状，
    不劣化、不裸崩；
  - 本插件 G0 无表、无 LLM 依赖（预算纯计算）；
  - 零特权：预算/裁剪一律走 ``core.tokens`` 公共原语，不碰内核内部。
"""

from __future__ import annotations

from agent.api import Services, plugin

from .governor import ContextGovernor
from .inspector import LedgerEntry, RoundLedger
from .protocols import Assembly, AssemblyReport, BlockReport, BlockSpec, GovernorLike


@plugin
class Context:
    """ctx.context —— 上下文治理插件（G0：预算模型 + token 化收窗接缝）。"""

    name = "plugins.context"
    version = "0.1.0"
    layer = "system"
    provides = [Services.CONTEXT]
    inject = []          # G0 零依赖；G1 摘要时增补 events/llm/db
    tables = ()          # G1：agent_session_summaries 随插件（M1 模式）
    description = "上下文治理：token 预算分块装配 + 循环内（react）窗口治理"

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("context") or {}
        # 接缝（与 memory 三组件同构）：ctx.get("context.governor") 存在即用注入实现。
        governor = ctx.get("context.governor") or ContextGovernor.from_config(cfg)
        ctx.provide(Services.CONTEXT, governor)


__all__ = [
    "Context",
    "ContextGovernor",
    "GovernorLike",
    "BlockSpec",
    "BlockReport",
    "AssemblyReport",
    "Assembly",
    "LedgerEntry",
    "RoundLedger",
]
