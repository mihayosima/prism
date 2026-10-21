"""插件包：context —— 上下文治理。

目录插件，语义上是系统组件；来源决定生命周期——经「插件」页可启用 / 停用 / 卸载。

职责：
 G0：预算模型 + token 化历史收窗；服务键 ``Services.CONTEXT``；
 接缝 ``ctx.get("context.governor") or ContextGovernor(...)`` 可注入替换。
 G1：分块装配统一编排 ``assemble(blocks)``——注册制+
 统一互斥+ 刚性超限双通道标注 + 剩余回吐；
 chat 的 plan / narrate / prior 均经此装配。
 S3/G2：循环内上下文治理——循环预算口径

 + 块内准备原语 ``clip_head_tail``+ messages 块通用 ``pinned``
 条目支持。
 G3–G5：MCP 源头护栏 / 记忆注入配额反推 / 工具块降载。

职责边界：本插件是全仓唯一持有 token 语义组件——只回答「内容怎么裁、占多少 token」，
不感知业务语义。
任务进度态归 ``plugins.work_memory``，循环编排归 ``plugins.react``。

解耦纪律：
 - 消费面一律 ``ctx.has(Services.CONTEXT)`` 软依赖——插件停用即退回消费方现状，
 不劣化、不裸崩；
 - 本插件 G0 无表、无 LLM 依赖；
 - 零特权：预算/裁剪一律走 ``core.tokens`` 公共原语，不碰内核内部。
"""

from __future__ import annotations

from agent.api import Services, plugin

from .governor import ContextGovernor
from .inspector import LedgerEntry, RoundLedger
from .protocols import Assembly, AssemblyReport, BlockReport, BlockSpec, GovernorLike
from .router import GovernorRouter


@plugin
class Context:
    """ctx.context —— 上下文治理插件。"""

    name = "plugins.context"
    version = "0.1.0"
    layer = "system"
    provides = [Services.CONTEXT]
    inject = []          # G0 零依赖；G1 摘要时增补 events/llm/db
    tables = ()          # G1：agent_session_summaries 随插件
    description = "上下文治理：token 预算分块装配 + 循环内（react）窗口治理"

    async def apply(self, ctx) -> None:
        cfg = ((ctx.config or {}).get("agent") or {}).get("context") or {}
        # 接缝：ctx.get("context.governor") 存在即用注入实现
        # ——注入形态保持「原样提供」，不套门面。
        injected = ctx.get("context.governor")
        if injected is not None:
            ctx.provide(Services.CONTEXT, injected)
            return
        # G2：提供门面——按当前会话选实例；无会话覆盖时恒返回容器实例，
        # 与改造前逐值等价。消费方仍 ctx.get(Services.CONTEXT) 现取，
        # 故零改动。详见 router.py 与实施方案 路线 B。
        ctx.provide(Services.CONTEXT,
                    GovernorRouter(ctx, ContextGovernor.from_config, cfg))


__all__ = [
    "Context",
    "ContextGovernor",
    "GovernorRouter",
    "GovernorLike",
    "BlockSpec",
    "BlockReport",
    "AssemblyReport",
    "Assembly",
    "LedgerEntry",
    "RoundLedger",
]
