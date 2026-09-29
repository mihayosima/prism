"""agent.plugins.context.governor —— 上下文治理器（G0 骨架 + G1 装配 + S3 循环内）。

预算模型：输入窗口总预算（``max_context_tokens - reserve_output_tokens``）按块分配——
  记忆块 ``memory_ratio`` · 历史窗口 ``history_ratio`` · 其余归 system/工具块/本轮消息。
裁剪一律按 token（``core.tokens.estimate_tokens``），不再按条数。

G0 提供预算查询 + 历史收窗（``pick_history``，保留供装配复用）。
G1 提供**分块装配** ``assemble(blocks)`` —— 注册制动态组装 + 统一互斥（左开右闭）+
刚性超限双通道标注 + 剩余回吐（water-filling）；消费面软依赖
``ctx.has(Services.CONTEXT)``——插件停用即退回各消费方现状，不劣化不裸崩。
S3 提供**循环内（react）预算口径**（``react_budget`` / ``clip_*`` / ``react_keep_recent``
/ ``react_min_chain_tokens``）+ 块内准备原语 ``clip_head_tail`` + messages 块**通用** ``pinned``
条目支持（块内先占、额度不足者降级）。

职责边界（S3 钉死，勿越界）：
- 本治理器**只**回答「内容怎么裁、占多少 token」，是**全仓唯一持有 token 语义**的组件；
- **不感知任何业务语义**——不认识「任务进度」，也不认识任何**循环步骤类型**：
  条目上的 `type` 字段属消费方私有语义，治理器**只认通用元数据 `pinned`**（块内先占额度）；
  ``pinned`` 是**通用**元数据，任何 messages 块都可携带。

G2.0（P0：只读观测 + 声明方向，**不改任何装配裁决行为**）：
- ``assemble(attachments=...)``：登记请求附加项（如 native tools 数组），使
  「扣了预算但未入账」的部分可归因——**登记本身不改变预算裁决**（额度仍由
  ``reserve_tokens`` 表达，两者取较大值作有效预留，绝不重复扣减）；
- ``AssemblyReport.unaccounted_tokens``：预算黑洞的唯一显式出口；
- ``AssemblyReport.stable_prefix_tokens`` / ``volatile_blocks``：缓存友好度体检
  （首个 volatile 块之前的装配量；纯只读，不参与裁决）；
- ``declare(decls, phase=, scope=)``：补上契约缺失的**声明方向**，使漏报可被结构性发现。
  声明是**配置期**只读常量（与 ``from_config`` 同级）——零状态纪律约束的是运行期不记历史。

G2.0（P2：治理动词，**首个改变装配行为的批次**）：
- ``plan(report, blocks=, mode=)``：**纯函数决策表**（§4.2.4）——``keep → clear → evict
  → compact → clip``（代价从低到高、可逆性从高到低）；``mode`` 缺省读配置 ``compact_mode``；
- ``execute(actions, blocks, mode=)``：把决策落到块上，三条执行纪律（§4.2.5）——
  ①失败即降级为 ``keep``（绝不半途破坏结构）；②执行前先校验、执行后复校，四条不变量
  任一失败 → 回滚该动作并记 ``degraded``；③``on_compact`` 由消费方提供，治理器只调用、
  不理解其内部（回调抛异常 → 该动作跳过，其余继续）；
- **压缩粒度 = 原子单元（硬约束）**：``compact`` 作用于 messages 块时，切分单位固定为
  wire 原子单元 ``_pair_units`` 的产物，切点**只在单元边界**；唯一合法形态是「整单元 →
  过程摘要」，任何半改（切点落在单元内部）判 **I-2 违规并回滚**。
- **执行点属消费方语义**：链内替换的**时机**（run 收尾存档点）由 ``react`` 决定，
  本治理器只提供 ``plan`` / ``execute`` 原语与不变量校验——零业务语义纪律。
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from agent.api import clip_text, clip_text_hard, estimate_tokens

from .protocols import (ACTION_KINDS, COMPACT_MODES, Action, ApplyResult, Assembly,
                        AssemblyReport, AttachmentReport, AttachmentSpec, BlockDecl,
                        BlockReport, BlockSpec, WaterlineReport, resolve_layer)
from .inspector import (LedgerEntry, RoundLedger, entry_from_payload,
                        ledger_from_payloads, render_entry)

# 可裁块默认权重（``BlockSpec.weight <= 0`` 时按 key 取；治理器自有口径，不依赖任何插件）。
_DEFAULT_WEIGHTS = {"memory": 0.10, "tools": 0.20, "history": 0.60, "summary": 0.10}
_DEFAULT_WEIGHT = 0.10
_SEP = "\n\n"          # 非空 text 块连接符（与原 system 拼接语义一致 → 对拍逐字节等价）

# ---- 层 → cap 配置键映射（G2.0 P1-5）----
# §4.4：cap 是**绝对量级**概念（「系统提示不该超过 8k」），与窗口大小无关——本次事故的
# 机械成因正是「所有阈值随窗口按比例放大 42 倍后全部失灵」。故 cap 用绝对值表达。
_LAYER_CAP_KEYS = {
    "resident": "resident_cap_tokens",
    "on_demand": "on_demand_cap_tokens",
    "input": "prompt_cap_tokens",
}

# ---- 回本公式常量（§3.3）----
# n* = ρ · r / (1 − r)，ρ = (miss − hit)/hit。三价模型 h : m : o = 1 : 30 : 90
# ⇒ ρ = 29。**只做除法**：治理器不感知价目表，故 ρ 以常量形式内联（可经构造参数覆盖）。
_PAYBACK_RHO = 29.0
# 单次清除的最小释放量（对标 Anthropic ``clear_at_least``）。P2 起为**配置键**
# ``clear_at_least_tokens`` 的缺省值（本模块常量仅作缺省，不再被硬编码引用）。
_CLEAR_AT_LEAST_TOKENS = 4000
# 治理模式缺省（§4.2.4：``shadow`` = 照常计算但**不执行**，只写遥测）。
_DEFAULT_COMPACT_MODE = "shadow"


class ContextGovernor:
    """上下文治理器：预算模型 + token 化收窗 + 分块装配（接缝默认实现，可注入替换）。"""

    def __init__(
        self,
        *,
        max_context_tokens: int = 24000,
        reserve_output_tokens: int = 4000,
        memory_ratio: float = 0.10,
        history_ratio: float = 0.60,
        source_clip_tokens: int = 8192,
        clip_max_tokens: int = 1000,
        clip_head_tokens: int = 500,
        clip_tail_tokens: int = 500,
        react_keep_recent: int = 6,
        react_min_chain_tokens: int = 2048,
        # ---- G2.0（P1）：双窗口解耦 + 水位 + 层 cap ----
        working_window_tokens: int = 96000,
        window_high_ratio: float = 0.70,
        window_low_ratio: float = 0.40,
        resident_cap_tokens: int = 8000,
        on_demand_cap_tokens: int = 8000,
        prompt_cap_tokens: int = 2000,
        payback_rho: float = _PAYBACK_RHO,
        # ---- G2.0（P2）：治理动词（模式 + clear 最小释放量）----
        compact_mode: str = _DEFAULT_COMPACT_MODE,
        clear_at_least_tokens: int = _CLEAR_AT_LEAST_TOKENS,
    ):
        if max_context_tokens <= 0 or reserve_output_tokens < 0:
            raise ValueError("max_context_tokens 须 > 0 且 reserve_output_tokens 须 >= 0")
        if reserve_output_tokens >= max_context_tokens:
            raise ValueError("reserve_output_tokens 须小于 max_context_tokens")
        if not (0.0 < memory_ratio < 1.0 and 0.0 < history_ratio < 1.0):
            raise ValueError("memory_ratio / history_ratio 须在 (0,1) 区间")
        if min(clip_max_tokens, clip_head_tokens, clip_tail_tokens,
               react_keep_recent, react_min_chain_tokens) < 0:
            raise ValueError("S3 循环内预算参数（clip_* / react_*）须 >= 0")
        if working_window_tokens <= 0:
            raise ValueError("working_window_tokens 须 > 0（经济窗口基数）")
        if not (0.0 < window_low_ratio <= window_high_ratio <= 1.0):
            raise ValueError("须 0 < window_low_ratio <= window_high_ratio <= 1")
        if min(resident_cap_tokens, on_demand_cap_tokens, prompt_cap_tokens) < 0:
            raise ValueError("层 cap（resident/on_demand/prompt_cap_tokens）须 >= 0")
        if payback_rho < 0:
            raise ValueError("payback_rho 须 >= 0")
        if str(compact_mode) not in COMPACT_MODES:
            raise ValueError(f"compact_mode 须 ∈ {COMPACT_MODES}，得到 {compact_mode!r}")
        if int(clear_at_least_tokens) < 0:
            raise ValueError("clear_at_least_tokens 须 >= 0")
        self.max_context_tokens = int(max_context_tokens)
        self.reserve_output_tokens = int(reserve_output_tokens)
        self.memory_ratio = float(memory_ratio)
        self.history_ratio = float(history_ratio)
        self.source_clip_tokens = int(source_clip_tokens)
        self._clip_max_tokens = int(clip_max_tokens)
        self._clip_head_tokens = int(clip_head_tokens)
        self._clip_tail_tokens = int(clip_tail_tokens)
        self._react_keep_recent = int(react_keep_recent)
        self._react_min_chain_tokens = int(react_min_chain_tokens)
        # G2.0（P1）：双窗口解耦——working_window 是**经济基数**（水位分母），
        # max_context_tokens 是**能力上限**（总闸）；两者量纲不同，绝不可互相顶替。
        self.working_window_tokens = int(working_window_tokens)
        self.window_high_ratio = float(window_high_ratio)
        self.window_low_ratio = float(window_low_ratio)
        self._resident_cap_tokens = int(resident_cap_tokens)
        self._on_demand_cap_tokens = int(on_demand_cap_tokens)
        self._prompt_cap_tokens = int(prompt_cap_tokens)
        self._payback_rho = float(payback_rho)
        # G2.0（P2）：治理模式与 clear 阈值——**只读配置**（非运行期状态）。
        self._compact_mode = str(compact_mode)
        self._clear_at_least_tokens = int(clear_at_least_tokens)
        # G2.0（P0）：声明表——**配置期**只读常量（与 from_config 同级），非运行期状态。
        # 键 ``(scope, phase)`` → ``{块 key: BlockDecl}``；同键后声明覆盖前声明（幂等）。
        self._decls: dict[tuple[str | None, str], dict[str, BlockDecl]] = {}

    # ---- 构造 ----

    @classmethod
    def from_config(cls, cfg: dict | None) -> "ContextGovernor":
        """从 ``agent.context`` 配置节构造（缺省键落默认值，诚实显式）。"""
        cfg = cfg or {}
        return cls(
            max_context_tokens=int(cfg.get("max_context_tokens", 24000)),
            reserve_output_tokens=int(cfg.get("reserve_output_tokens", 4000)),
            memory_ratio=float(cfg.get("memory_ratio", 0.10)),
            history_ratio=float(cfg.get("history_ratio", 0.60)),
            source_clip_tokens=int(cfg.get("source_clip_tokens", 8192)),
            clip_max_tokens=int(cfg.get("clip_max_tokens", 1000)),
            clip_head_tokens=int(cfg.get("clip_head_tokens", 500)),
            clip_tail_tokens=int(cfg.get("clip_tail_tokens", 500)),
            react_keep_recent=int(cfg.get("react_keep_recent", 6)),
            react_min_chain_tokens=int(cfg.get("react_min_chain_tokens", 2048)),
            working_window_tokens=int(cfg.get("working_window_tokens", 96000)),
            window_high_ratio=float(cfg.get("window_high_ratio", 0.70)),
            window_low_ratio=float(cfg.get("window_low_ratio", 0.40)),
            resident_cap_tokens=int(cfg.get("resident_cap_tokens", 8000)),
            on_demand_cap_tokens=int(cfg.get("on_demand_cap_tokens", 8000)),
            prompt_cap_tokens=int(cfg.get("prompt_cap_tokens", 2000)),
            payback_rho=float(cfg.get("payback_rho", _PAYBACK_RHO)),
            compact_mode=str(cfg.get("compact_mode", _DEFAULT_COMPACT_MODE)),
            clear_at_least_tokens=int(cfg.get("clear_at_least_tokens", _CLEAR_AT_LEAST_TOKENS)),
        )

    # ---- 预算查询 ----

    def input_budget(self) -> int:
        """输入窗口总预算 = 总窗口 - 输出余量。"""
        return self.max_context_tokens - self.reserve_output_tokens

    def memory_budget(self) -> int:
        """记忆块配额（token）——消费方据此反推 memory.context 实参（G4）。"""
        return int(self.input_budget() * self.memory_ratio)

    def history_budget(self) -> int:
        """历史窗口配额（token）——本预算内从新到旧贪心收取。"""
        return int(self.input_budget() * self.history_ratio)

    def source_clip_budget(self) -> int:
        """MCP 源头护栏上限（G3 接线用；G0 先固化口径）。"""
        return self.source_clip_tokens

    # ---- S3：循环内（react）预算口径 ----
    #
    # 口径统一：``react_budget`` 由 ``input_budget()`` 派生，**不另立窗口口径**
    # （否则「会话级」与「循环级」两套窗口会在配置上打架）。
    # 注意：结果**可能为负**（prior 已超输入预算）——调用方须显式处置，不可当 0 静默吞掉。

    def react_budget(self, prior_tokens: int) -> int:
        """循环内可用额度 = 输入预算 - 入口 prior 实占（**可为负**）。"""
        return self.input_budget() - max(0, int(prior_tokens))

    def react_keep_recent(self) -> int:
        """循环内最近 N 条链原文保留（更老者才参与首尾折叠）。"""
        return self._react_keep_recent

    def react_min_chain_tokens(self) -> int:
        """循环内 chain 保底额度——会话级装配 prior 时为循环预留（防循环空转）。"""
        return self._react_min_chain_tokens

    def clip_max_tokens(self) -> int:
        """单条观察结果超此值即触发首尾折叠。"""
        return self._clip_max_tokens

    def clip_head_tokens(self) -> int:
        """折叠后头部保留量。"""
        return self._clip_head_tokens

    def clip_tail_tokens(self) -> int:
        """折叠后尾部保留量（错误常在尾部）。"""
        return self._clip_tail_tokens

    # ---- 历史收窗（收窗机制已从 chat/compactor 移除调用；保留供装配/未来复用） ----

    def pick_history(
        self,
        messages: list[dict],
        budget: int | None = None,
        *,
        keep_first: bool = False,
    ) -> tuple[list[dict], int]:
        """token 口径历史收窗：从最新往回贪心收取，超出预算即止。

        刚性保障（可超预算，保对话不残）：
        - **最新一条**永远保留（当前话题不可丢）；
        - ``keep_first=True`` 时**首条 user 消息**额外保留（多步循环"首问"语义，
          与 react 既有 ``_trim`` 行为对齐）。

        返回 ``(按原顺序保留的消息, 丢弃条数)``。只裁喂给模型的窗口，
        不动调用方持有的消息全集。
        """
        if budget is None:
            budget = self.history_budget()
        n = len(messages)
        if n == 0:
            return [], 0
        toks = [estimate_tokens(m) for m in messages]
        keep: set[int] = set()
        used = 0
        # 刚性：最新一条
        keep.add(n - 1)
        used += toks[n - 1]
        # 贪心：从新到旧
        for i in range(n - 2, -1, -1):
            if used + toks[i] > budget:
                break
            keep.add(i)
            used += toks[i]
        # 刚性：首条 user（keep_first）
        if keep_first:
            fu = next((i for i, m in enumerate(messages)
                       if isinstance(m, dict) and m.get("role") == "user"), None)
            if fu is not None:
                keep.add(fu)
        dropped = n - len(keep)
        kept = [m for i, m in enumerate(messages) if i in keep]
        return kept, dropped

    # ---- 块内准备（S3：只折叠、不丢弃） ----

    def clip_head_tail(self, text: str, *, head: int | None = None,
                       tail: int | None = None) -> tuple[str, bool]:
        """首尾保留折叠（**只折叠、不丢弃**）：返回 ``(新文本, 是否发生折叠)``。

        - 未超 ``clip_max_tokens`` → 原样返回 ``(text, False)``；
        - 超过 → 保头 ``head`` + 保尾 ``tail``，中段折叠为省略标注，并保证实占 <= ``head+tail``；
        - 头尾占比按 ``head:tail`` 换算（错误通常落在尾部，故尾部不缺席）；
        - 与 ``_trim_messages``（整块丢弃 + 末条保底）**职责不重叠**：本方法绝不丢条目。
        """
        h = self._clip_head_tokens if head is None else max(0, int(head))
        t = self._clip_tail_tokens if tail is None else max(0, int(tail))
        target = max(1, h + t)
        if estimate_tokens(text) <= self._clip_max_tokens:
            return text, False
        out, omitted = clip_text(text, target, head_ratio=(h / target) or 0.6)
        if omitted <= 0:
            return text, False
        return self._clip_hard(out, target), True

    # ---- 分块装配（G1） ----

    def _block_tokens(self, kind: str, payload: Any) -> int:
        """块内容 token 估算（text 串 / messages 列表）。"""
        if kind == "messages":
            return sum(estimate_tokens(m) for m in (payload or []))
        return estimate_tokens(payload or "")

    def _weight_of(self, b: BlockSpec) -> float:
        if b.weight > 0:
            return float(b.weight)
        return _DEFAULT_WEIGHTS.get(b.key, _DEFAULT_WEIGHT)

    def _layer_cap(self, layer: str) -> int:
        """层缺省 cap（§4.4：resident / on_demand / input 三处；其余层无 cap）。"""
        key = _LAYER_CAP_KEYS.get(layer)
        return int(getattr(self, "_" + key)) if key else 0

    def _cap_of(self, b: BlockSpec) -> int:
        """本块生效 cap：**显式 ``cap_tokens`` 优先**，否则按生效层取配置 cap；0 = 无 cap。"""
        if b.cap_tokens > 0:
            return int(b.cap_tokens)
        return self._layer_cap(b.effective_layer())

    @staticmethod
    def _present_nonempty(b: BlockSpec) -> bool:
        """注册制核心：`present=False` 或内容为空 → 不参与预算/装配。"""
        if not b.present:
            return False
        if b.kind == "messages":
            return bool(b.payload)
        return bool(str(b.payload or "").strip())

    @staticmethod
    def _coerce(b: BlockSpec | dict) -> BlockSpec:
        """兼容消费层传入 dict（chat 不 import 契约类型；解耦纪律）。"""
        if isinstance(b, BlockSpec):
            return b
        cov = b.get("coverage")
        return BlockSpec(
            key=str(b.get("key", "")),
            kind=str(b.get("kind", "text")),
            present=bool(b.get("present", False)),
            payload=b.get("payload", ""),
            weight=float(b.get("weight", 0.0) or 0.0),
            priority=int(b.get("priority", 100)),
            rigid=bool(b.get("rigid", False)),
            coverage=(int(cov[0]), int(cov[1])) if cov else None,
            volatile=bool(b.get("volatile", False)),
            layer=str(b.get("layer", "") or ""),
            cap_tokens=int(b.get("cap_tokens", 0) or 0),
            compact_at=int(b.get("compact_at", 0) or 0),
            compact_floor_ratio=float(b.get("compact_floor_ratio", 0.4) or 0.4),
            on_compact=b.get("on_compact"),
            regenerable=bool(b.get("regenerable", False)),
        )

    @staticmethod
    def _coerce_attachment(a: AttachmentSpec | dict) -> AttachmentSpec:
        """兼容消费层传入 dict（与 ``_coerce`` 同纪律：消费层零 import）。"""
        if isinstance(a, AttachmentSpec):
            return a
        return AttachmentSpec(
            key=str(a.get("key", "")),
            claimed_tokens=int(a.get("claimed_tokens", 0) or 0),
            present=bool(a.get("present", True)),
            payload=a.get("payload"),
        )

    # ---- 声明式注册（G2.0 P0：补上契约缺失的「声明方向」） ----

    def declare(self, decls: list[BlockDecl], *, phase: str = "default",
                scope: str | None = None) -> None:
        """声明本阶段**应该有哪些块**（配置期注入；幂等；非运行期状态）。

        用途：``AssemblyReport.missing_blocks`` —— 消费方**根本没提交**（连
        ``present=False`` 都没给）声明的必需块时变红灯。这是「单向 pull 无漏报发现能力」
        这一结构性缺陷的修法：块从管线里悄悄消失，从此会被结构性发现而非靠人排查。

        纪律（勿误判）：本方法写入的是**启动期一次性注入的只读常量**（与 ``from_config``
        同级），不随请求变化、不记录历史——零状态纪律约束的是**运行期不得记忆历史**。
        同一 ``(scope, phase, key)`` 重复声明以**最后一次**为准（启动幂等）。
        """
        node = self._decls.setdefault((scope, phase), {})
        for d in decls:
            if isinstance(d, BlockDecl):
                node[d.key] = d
            else:                              # 兼容 dict 入参（消费层零 import）
                key = str(d.get("key", ""))
                node[key] = BlockDecl(
                    key=key, kind=str(d.get("kind", "text")),
                    owner=str(d.get("owner", "")),
                    required=bool(d.get("required", True)),
                    layer=str(d.get("layer", "")),
                    cap_tokens=int(d.get("cap_tokens", 0) or 0),
                )

    def _missing_blocks(self, submitted: set[str], phase: str,
                        scope: str | None) -> list[str]:
        """声明的必需块中，**本次连提交都没有**的 key（如实上报，不猜业务）。"""
        decls = self._decls.get((scope, phase)) or {}
        return sorted(d.key for d in decls.values() if d.required and d.key not in submitted)

    # ---- 水位判定（G2.0 P1-3：纯函数，无状态） ----

    def assess(self, used: int, *,
               working_window: int | None = None,
               remaining_steps: int | None = None,
               phase: str | None = None,
               scope: str | None = None,
               blocks: list[BlockSpec] | None = None) -> WaterlineReport:
        """**水位判定**：把「已用多少」换算成 ``normal / high`` 与三重闸明细。

        纪律（零状态）：本方法是**纯函数**——结果只依赖入参与本实例的**只读配置**，
        不读取任何历史、不写入任何字段；连续两次同样入参必得**逐字段相同**的结果
        （见 ``test_assess_pure_function_no_state``）。

        - **阈值闸**：``load_ratio = used / working_window >= window_high_ratio``；
          ``working_window`` 缺省取配置 ``working_window_tokens``（**经济基数**，
          与能力上限 ``max_context_tokens`` 解耦）。
        - **时机闸**：仅 ``phase == "run_teardown"``（run 收尾存档点）——这是「冷却」的
          **无状态实现**（一 run 一次），不是靠记时间戳。
        - **回本闸**：``n* = ρ · r/(1−r)``（ρ 由三价模型内联；r 取候选块 ``compact_floor_ratio``
          的最小值，缺省 0.4）。``remaining_steps`` 缺省 ``None`` ⇒ 判 ``passed=False``
          （**保守不压**）。``remaining_steps`` 语义＝「**下一次 run 的预计剩余步数**」。
        - **释放闸**：``expected_release``（P1 仅当传入 ``blocks`` 时可算）≥ ``clear_at_least``。

        ``scope`` 是**不透明归集键**（当前仅回填进报告，不作控制流分支）。
        """
        w = max(1, int(working_window) if working_window else self.working_window_tokens)
        u = max(0, int(used))
        ratio = u / w
        level = "high" if ratio >= self.window_high_ratio else "normal"

        # 回本闸：r 取「候选块」目标残留比的最小值（更激进者决定回本线）；无候选则用缺省 0.4。
        blocks_c = [self._coerce(b) for b in blocks] if blocks else []
        floors = [b.compact_floor_ratio for b in blocks_c if b.compact_at > 0]
        r = min(floors) if floors else 0.4
        r = min(0.999, max(0.0, float(r)))
        n_star = self._payback_rho * r / (1.0 - r)
        payback_passed = (remaining_steps is not None
                          and float(remaining_steps) >= n_star)

        # 释放闸：可清除（regenerable）且属按需/运行时层的块，其体量之和即预期释放。
        expected_release = sum(
            self._block_tokens(b.kind, b.payload) for b in blocks_c
            if b.regenerable and b.effective_layer() in ("on_demand", "runtime"))
        clear_at_least = self._clear_at_least_tokens

        # 粗粒度建议：P1 不见块清单时回落 "clip"（保底、不做结构性改动）；
        # 传块时可给出 "clear"（有可清除候选且释放量达标），精确动作归 P2 的 plan。
        if level == "normal":
            suggest = "none"
        elif expected_release >= clear_at_least and clear_at_least > 0:
            suggest = "clear"
        else:
            suggest = "clip"

        gates: dict[str, Any] = {
            "threshold": {"passed": level == "high", "ratio": ratio,
                          "high": self.window_high_ratio, "low": self.window_low_ratio},
            "timing": {"at_run_teardown": phase == "run_teardown", "phase": phase},
            "payback": {"required_steps": n_star, "floor_ratio": r,
                        "remaining_steps": remaining_steps, "passed": payback_passed},
            "release": {"clear_at_least": clear_at_least,
                        "expected_release": expected_release,
                        "passed": expected_release >= clear_at_least},
        }
        return WaterlineReport(used=u, working_window=w,
                               capability_window=self.max_context_tokens,
                               load_ratio=ratio, level=level,
                               suggested=suggest, gates=gates)

    # ---- 配置读取（消费方只读；零状态） ----

    def compact_mode(self) -> str:
        """默认治理模式（``off`` / ``shadow`` / ``on``）——``plan``/``execute`` 缺省取此值。"""
        return self._compact_mode

    def clear_at_least_tokens(self) -> int:
        """单次清除的最小释放量（``clear`` 动作的准入门槛）。"""
        return self._clear_at_least_tokens

    def _resolve_mode(self, mode: str | None) -> str:
        m = self._compact_mode if mode is None else str(mode)
        if m not in COMPACT_MODES:
            raise ValueError(f"mode 须 ∈ {COMPACT_MODES}，得到 {m!r}")
        return m

    # ---- 治理动词（G2.0 P2-1：决策表，§4.2.4） ----

    def plan(self, report: WaterlineReport, *,
             blocks: list[BlockSpec] | None = None,
             mode: str | None = None) -> list[Action]:
        """**决策表**（自上而下，首个命中即返回；纯函数、无状态）。

        =====  =============================================================  ==========
        序      条件                                                             Action
        =====  =============================================================  ==========
        1      ``mode == "off"`` 或 ``level == "normal"``                       ``keep``
        2      有 ``regenerable`` 且层 ∈ {on_demand, runtime} 的块，              ``clear``
               预计释放 ≥ ``clear_at_least_tokens``（首选：零推理成本）
        3      有 ``layer == "on_demand"`` 且 ``regenerable`` 的块               ``evict``
        4      ``timing.at_run_teardown`` 且 ``payback.passed``                 ``compact``
               且有 ``on_compact`` 的块（仅 run 收尾存档点；须过回本闸）
        5      其余                                                              ``clip``
        =====  =============================================================  ==========

        顺序理由：``keep → clear → evict → compact → clip`` 是「代价从低到高、可逆性从高
        到低」的排序（与 Anthropic「清除优先于压缩」一致，且符合本项目实测压缩罚金 30 倍）。
        """
        m = self._resolve_mode(mode)
        if m == "off":
            return [Action(kind="keep", reason="mode_off")]
        if report is None or report.level != "high":
            return [Action(kind="keep", reason="below_waterline")]
        blocks_c = [self._coerce(b) for b in (blocks or [])]

        # 2) clear：零推理成本，首选（可清除后重建者）
        clear_cands = [b for b in blocks_c
                       if b.regenerable and b.effective_layer() in ("on_demand", "runtime")]
        clear_release = sum(self._block_tokens(b.kind, b.payload) for b in clear_cands)
        if clear_cands and clear_release >= self._clear_at_least_tokens:
            return [Action(kind="clear", target=b.key,
                           expected_release=self._block_tokens(b.kind, b.payload),
                           reason="regenerable_clear") for b in clear_cands]

        # 3) evict：卸载按需块（内容可重取）
        evict_cands = [b for b in blocks_c
                       if b.effective_layer() == "on_demand" and b.regenerable]
        if evict_cands:
            return [Action(kind="evict", target=b.key,
                           expected_release=self._block_tokens(b.kind, b.payload),
                           reason="on_demand_evict") for b in evict_cands]

        # 4) compact：**仅 run 收尾存档点**（时机闸）+ 过回本闸 + 有回调（否则无从摘要）
        gates = report.gates or {}
        timing_ok = bool((gates.get("timing") or {}).get("at_run_teardown"))
        payback_ok = bool((gates.get("payback") or {}).get("passed"))
        compact_cands = [b for b in blocks_c if callable(b.on_compact)]
        if timing_ok and payback_ok and compact_cands:
            return [Action(kind="compact", target=b.key, reason="payback_ok_at_teardown")
                    for b in compact_cands]

        # 5) clip：保底——不做结构性改动
        return [Action(kind="clip", reason="no_structural_action")]

    # ---- 治理动词（G2.0 P2-1：落位，§4.2.5） ----

    def execute(self, actions: list[Action], blocks: list[BlockSpec], *,
                mode: str | None = None) -> tuple[list[BlockSpec], ApplyResult]:
        """把 ``plan`` 的决策落到块上（三条执行纪律，§4.2.5）。

        1. **失败即降级为 keep**——绝不半途破坏结构（任何拒绝都记入 ``skipped``）；
        2. **执行前先校验、执行后复校**——I-1/I-2 任一失败 → 回滚该动作并记 ``degraded``；
        3. **``on_compact`` 由消费方提供**——治理器只调用、不理解其内部（纯度）；
           回调抛异常 → 该动作跳过，其余继续。

        ``shadow``（缺省）：**不执行**，如实回报（决策与理由由消费方写遥测）——这是让压缩
        阈值用真实数据校准的唯一安全方式。``off``：同样不执行。
        """
        m = self._resolve_mode(mode)
        blocks_c = [self._coerce(b) for b in blocks]
        acts = [self._coerce_action(a) for a in (actions or [])]
        if m != "on":
            why = "mode_off" if m == "off" else "shadow"
            return blocks_c, ApplyResult(applied=[],
                                         skipped=[(a.target or a.kind, why) for a in acts],
                                         invariants={}, degraded=False)

        out = list(blocks_c)
        applied: list[str] = []
        skipped: list[tuple[str, str]] = []
        released = 0
        degraded = False
        for a in acts:
            if a.kind not in ACTION_KINDS:
                skipped.append((a.target or a.kind, "unknown_kind"))
                degraded = True
                continue
            if a.kind in ("keep", "clip"):
                skipped.append((a.target or a.kind, a.kind))
                continue
            idx = next((i for i, b in enumerate(out) if b.key == a.target), None)
            if idx is None:
                skipped.append((a.target, "block_not_found"))
                degraded = True
                continue
            b = out[idx]
            try:
                new_b, rel, ok, why = self._apply_action(a, b)
            except Exception as exc:          # noqa: BLE001 —— 回调/实现异常一律降级，不裸崩
                new_b, rel, ok, why = b, 0, False, f"exception:{type(exc).__name__}"
            if not ok:
                skipped.append((a.target, why))
                degraded = True
                continue
            cand = list(out)
            cand[idx] = new_b
            if not self._invariants_ok(out, cand):     # 执行后复校（I-1 / I-2）
                skipped.append((a.target, "invariant_violation"))
                degraded = True
                continue
            out = cand
            released += max(0, int(rel))
            applied.append(f"{a.kind}:{a.target}")
        return out, ApplyResult(applied=applied, skipped=skipped, released=released,
                                invariants=self._invariants_report(blocks_c, out),
                                degraded=degraded)

    # ---- Inspector 门面（G2.0 P3：``/context`` 视图 + 轮次账本；纯函数、零状态）----
    # 消费方（run / react）**经服务软调用**这些方法，不 import 插件内部（解耦纪律）；
    # 注入实现若不提供 → 消费方 ``getattr(gov, "ledger", None)`` 取不到即降级跳过。

    def ledger(self, payloads: Any = None) -> RoundLedger:
        """把 ``CONTEXT_GOVERNED`` 遥测载荷（普通 dict 序列）重建为**轮次全景账本**（纯函数）。

        ``payloads`` 是**不透明**输入——本方法只做「载荷 → 记账」的机械重建，不猜测轮次边界、
        不读任何历史、不写状态（轮次归集由消费方决定）。空入参返回空账本。
        """
        if payloads is None:
            return RoundLedger(entries=())
        return ledger_from_payloads(payloads)

    def render_ledger(self, payloads: Any = None, *, tolerance: int = 0) -> str:
        """渲染**轮次全景账本**文本（``/context`` 分类视图 + 逐端点 I-4 对账）。"""
        return self.ledger(payloads).render(tolerance=tolerance)

    def entry(self, payload: Any) -> LedgerEntry:
        """把单个 governed 载荷重建为 ``LedgerEntry``（纯函数）。"""
        return entry_from_payload(payload or {})

    def render_entry(self, payload: Any, *, tolerance: int = 0) -> str:
        """渲染**单端点** ``/context`` 分类视图。"""
        return render_entry(entry_from_payload(payload or {}), tolerance=tolerance)

    def check_invariants(self, payload: Any, *, tolerance: int = 0) -> dict[str, bool]:
        """记账面可判定的不变量（**I-3** 前缀稳定性可测 / **I-4** 预算守恒）。

        返回 ``{"I-3": bool, "I-4": bool}``——供每次装配后「记录 + 告警（不阻断）」。
        I-1 / I-2 的判定需 messages 原始对象（装配对象面），由 ``execute`` 守护，不在此回报。
        """
        return self.entry(payload).invariants(tolerance=tolerance)


    @staticmethod
    def _coerce_action(a: Action | dict) -> Action:
        """兼容消费层传入 dict（与 ``_coerce`` 同纪律：消费层零 import）。"""
        if isinstance(a, Action):
            return a
        return Action(kind=str(a.get("kind", "keep")),
                      target=str(a.get("target", "") or ""),
                      scope=a.get("scope"),
                      expected_release=int(a.get("expected_release", 0) or 0),
                      reason=str(a.get("reason", "")),
                      payload=a.get("payload"))

    @staticmethod
    def _empty_payload(kind: str) -> Any:
        return [] if kind == "messages" else ""

    def _apply_action(self, a: Action, b: BlockSpec) -> tuple[BlockSpec, int, bool, str]:
        """执行单个动作；返回 ``(新块, 释放量, 是否成功, 原因)``。"""
        if a.kind == "clear":
            if not b.regenerable:                        # 只清「可重建」者（否则丢真）
                return b, 0, False, "not_regenerable"
            old = self._block_tokens(b.kind, b.payload)
            return replace(b, payload=self._empty_payload(b.kind)), old, True, "ok"
        if a.kind == "evict":
            if b.effective_layer() != "on_demand" or not b.regenerable:
                return b, 0, False, "not_evictable"
            old = self._block_tokens(b.kind, b.payload)
            return replace(b, payload=self._empty_payload(b.kind)), old, True, "ok"
        if a.kind == "compact":
            return self._compact_block(a, b)
        return b, 0, False, f"unknown_kind:{a.kind}"

    def _target_tokens(self, a: Action, b: BlockSpec, old: int) -> int:
        """目标残留量：``Action.payload`` 显式给 ``target_tokens`` 优先；否则按残留比 r 折算。"""
        if isinstance(a.payload, dict) and a.payload.get("target_tokens") is not None:
            return max(1, int(a.payload["target_tokens"]))
        r = min(0.999, max(0.0, float(b.compact_floor_ratio)))
        return max(1, int(old * r))

    def _compact_block(self, a: Action, b: BlockSpec) -> tuple[BlockSpec, int, bool, str]:
        """压缩原语（`Compactor`）：**唯一**与「上下文原语」相关的单元，可整体替换。

        messages 块走**整单元替换**（见 ``_compact_messages``）；text 块直接调回调。
        """
        cb = b.on_compact
        if not callable(cb):
            return b, 0, False, "no_on_compact"
        old = self._block_tokens(b.kind, b.payload)
        target = self._target_tokens(a, b, old)
        if target >= old:
            return b, 0, False, "already_within_target"
        if b.kind == "messages":
            return self._compact_messages(b, target, cb, old)
        new = cb(b.payload, target)
        if not isinstance(new, str):                     # 类型不符 → 视为失败，保留原 payload
            return b, 0, False, "on_compact_type"
        new_tokens = estimate_tokens(new)
        if new_tokens >= old:
            return b, 0, False, "no_reduction"
        return replace(b, payload=new), old - new_tokens, True, "ok"

    def _compact_messages(self, b: BlockSpec, target: int, cb, old: int
                          ) -> tuple[BlockSpec, int, bool, str]:
        """**整单元替换**（I-2 硬约束）：切点**只在原子单元边界**。

        选择规则：**``pinned`` 单元与最新单元永不丢弃**（块内先占语义的延续）；其余从最新
        往回按整单元纳入保留集，累计超 ``target`` 即停——被丢弃的因此是**最旧的一段整单元**。

        分工：本方法负责**切分**（纯 token 语义）与**不变量校验**；回调只负责「把被丢弃的
        那段消息压成过程摘要」（消费方语义）。回调产物若造成半改（切点落在单元内部）→
        判 I-2 违规并拒绝（回滚）。
        """
        msgs = list(b.payload or [])
        units = self._pair_units(msgs)
        if len(units) <= 1:                              # 单单元无可替换（保最新）
            return b, 0, False, "single_unit"
        toks = [sum(estimate_tokens(msgs[i]) for i in u) for u in units]
        keep_unit: set[int] = {len(units) - 1}           # 最新单元永不丢
        keep_unit.update(k for k, u in enumerate(units)
                         if any(msgs[i].get("pinned") for i in u))
        acc = sum(toks[k] for k in keep_unit)
        for k in range(len(units) - 1, -1, -1):          # 从最新往回按整单元纳入
            if k in keep_unit:
                continue
            if acc + toks[k] <= target:
                keep_unit.add(k)
                acc += toks[k]
            else:
                break                                    # 断点之外（更旧）一律丢弃
        drop_idx = [k for k in range(len(units)) if k not in keep_unit]
        if not drop_idx:
            return b, 0, False, "nothing_to_drop"
        drop_flat = [msgs[i] for k in drop_idx for i in units[k]]
        keep_flat = [msgs[i] for k in range(len(units)) if k in keep_unit
                     for i in units[k]]
        summary = cb(drop_flat, target)
        head = self._summary_head(summary)
        if head is None:
            return b, 0, False, "on_compact_type"
        if self._partial_in_head(msgs, head):            # I-2：切点不得落在单元内部
            return b, 0, False, "I-2_violation"
        new_payload = head + keep_flat
        if not self._pair_ok(new_payload):               # I-1：不产生孤儿回灌
            return b, 0, False, "I-1_violation"
        new_tokens = self._block_tokens("messages", new_payload)
        if new_tokens >= old:
            return b, 0, False, "no_reduction"
        return replace(b, payload=new_payload), old - new_tokens, True, "ok"

    @staticmethod
    def _summary_head(summary: Any) -> list[dict] | None:
        """把回调产物规整为「摘要消息（可多条）」；类型不符返回 ``None``。"""
        if isinstance(summary, dict):
            return [summary]
        if isinstance(summary, list) and all(isinstance(x, dict) for x in summary):
            return list(summary)
        if isinstance(summary, str):
            return [{"role": "user", "content": summary, "type": "summary"}]
        return None

    # ---- 不变量校验（§4.6：I-1 配对完整 · I-2 单元完整） ----

    @staticmethod
    def _is_result_msg(m: dict) -> bool:
        c = m.get("content")
        return isinstance(c, dict) and c.get("tool_results") is not None

    @staticmethod
    def _opens_unit(m: dict) -> bool:
        return m.get("role") == "assistant" and bool(m.get("tool_calls"))

    def _pair_ok(self, messages: list[dict]) -> bool:
        """**I-1 配对完整**：任何 ``tool_results`` 回灌都必须能与前置
        ``assistant.tool_calls`` 配对（9-28 实测 400 的根因）。"""
        for u in self._pair_units(messages):
            if any(self._is_result_msg(messages[i]) for i in u):
                if not self._opens_unit(messages[u[0]]):
                    return False
        return True

    @staticmethod
    def _msg_sig(m: dict) -> str:
        """条目结构指纹（忽略内部 ``seq``——输出前会被剥离，不算内容变更）。"""
        return json.dumps({k: v for k, v in m.items() if k != "seq"},
                          sort_keys=True, ensure_ascii=False, default=str)

    def _partial_in_head(self, before: list[dict], head: list[dict]) -> bool:
        """**I-2 判定（严格版，用于「回调产物」）**：切点是否落在某个原子单元内部？

        判据：把回调产物 ``head`` 按内容指纹与 ``before`` 的每个原子单元比对——若某单元
        **只有一部分成员**出现在 ``head`` 里（如只回显了 ``assistant.tool_calls`` 却丢了
        配对回灌），即为「半改」，返回 ``True``（应回滚）。

        为什么只与 ``head``（回调新产物）比对、而非与整段 ``after``：保留段由治理器**原对象**
        拼接，与 ``head`` 内容空间天然分离——这样既免去同形条目互相误判（两条字节相同但
        分属不同单元的消息会致假阳性），又能同时抓出「回显同一对象」与「深拷贝回显」两种半改。
        """
        head_sigs = {self._msg_sig(m) for m in head}
        for u in self._pair_units(before):
            sigs = [self._msg_sig(before[i]) for i in u]
            kept = sum(1 for s in sigs if s in head_sigs)
            if kept not in (0, len(sigs)):
                return True
        return False

    def _unit_atomic_ok(self, before: list[dict], after: list[dict]) -> bool:
        """**I-2 单元完整（原子性，通用安全网）**：``before`` 的每个原子单元在 ``after`` 中
        要么整体保留、要么整体消失——不得半改。

        判据用**对象身份**（``after`` 由治理器以原对象拼接，故保留段可被精确识别）：这只会
        **少报**（深拷贝式的半改可能被漏过，由 ``_partial_in_head`` 在 ``compact`` 路径上补足），
        绝不**误报**——避免同形条目导致的假阳性回滚。
        """
        after_ids = {id(m) for m in after}
        for u in self._pair_units(before):
            ids = [id(before[i]) for i in u]
            kept = sum(1 for x in ids if x in after_ids)
            if kept not in (0, len(ids)):
                return False
        return True

    def _invariants_ok(self, before: list[BlockSpec], after: list[BlockSpec]) -> bool:
        """逐 messages 块判定 I-1 / I-2（装配对象上可判定的两条）。"""
        bmap = {b.key: b for b in before}
        for b in after:
            if b.kind != "messages":
                continue
            after_msgs = list(b.payload or [])
            if not self._pair_ok(after_msgs):
                return False
            ob = bmap.get(b.key)
            before_msgs = list(ob.payload or []) if ob is not None else []
            if not self._unit_atomic_ok(before_msgs, after_msgs):
                return False
        return True

    def _invariants_report(self, before: list[BlockSpec],
                           after: list[BlockSpec]) -> dict[str, bool]:
        """四条不变量中可判定的两条之**最终**结果（供 ``ApplyResult.invariants``）。"""
        i1 = all(self._pair_ok(list(b.payload or []))
                 for b in after if b.kind == "messages")
        bmap = {b.key: b for b in before}
        i2 = True
        for b in after:
            if b.kind != "messages":
                continue
            ob = bmap.get(b.key)
            if ob is None:
                continue
            if not self._unit_atomic_ok(list(ob.payload or []), list(b.payload or [])):
                i2 = False
        return {"I-1": bool(i1), "I-2": bool(i2)}

    def _clip_hard(self, text: str, budget: int) -> str:
        """定点硬裁：保证 ``estimate_tokens(结果) <= budget``（省略标注开销已计入）。

        **复用 ``core.tokens.clip_text_hard``**（S4 起全仓唯一硬裁实现，本方法仅留薄封装，
        供块内既有调用点 ``clip_head_tail`` / ``_trim_text`` 不改动）。
        """
        return clip_text_hard(text, budget)[0]

    def _trim_text(self, text: str, budget: int) -> tuple[str, bool]:
        if budget <= 0:
            return "", bool(text.strip())
        if estimate_tokens(text) <= budget:
            return text, False
        return self._clip_hard(text, budget), True

    @staticmethod
    def _msg_id(m: dict, i: int) -> str:
        """条目 id：优先调用方给的 ``id``，缺省用块内序号（供 pinned 降级追溯）。"""
        return str(m.get("id") or f"#{i}")

    @staticmethod
    def _pair_units(messages: list[dict]) -> list[list[int]]:
        """按 wire 配对形状把消息分组成**原子单元**（400-fix A）。

        形状判据（与 ``request_cost.wire_messages`` 的内部约定同构，零业务感知）：
        - **工具调用条目**：``assistant`` 且 ``tool_calls`` 非空 → 开启新单元；
        - **工具结果回灌**：``content`` 为 dict 且含 ``tool_results`` → 并入当前开启单元
          （无开启单元则自成单元——孤儿回灌本就 wire 非法，不在此纠）；
        - 其余（system / user / 纯文本 assistant）→ 自成单元（闭合当前开启单元）。

        动机：预算裁剪的丢弃切点若落在工具调用与其结果回灌之间，请求即违反服务商
        「role=tool 必须紧跟带 tool_calls 的 assistant」配对校验（9-28 实测 400）。
        故**裁剪以单元为粒度**：调用与回灌同进同退，杜绝拆对。
        """
        units: list[list[int]] = []
        open_unit: list[int] | None = None
        for i, m in enumerate(messages):
            content = m.get("content")
            is_result = (isinstance(content, dict)
                         and content.get("tool_results") is not None)
            if m.get("role") == "assistant" and m.get("tool_calls"):
                open_unit = [i]
                units.append(open_unit)
            elif is_result and open_unit is not None:
                open_unit.append(i)
            else:
                open_unit = None
                units.append([i])
        return units

    @staticmethod
    def _clip_unit_hard(unit: list[dict], budget: int, clip_fn) -> list[dict]:
        """末条保底（单元版）：**整单元保留**，可裁文本按体量占比分摊预算硬裁。

        只裁文本载荷（工具调用条目的 content 字符串 / 结果回灌的 tool_results[].content），
        配对结构（tool_calls / tool_call_id）绝不破坏；外壳骨架开销先扣再裁
        （与旧单条保底同纪律）。``clip_fn`` 签名 ``(text, budget) -> str``。
        """
        texts: list[tuple[int, Any, str]] = []           # (成员序, tr容器(None=整content), 原文)
        for j, m in enumerate(unit):
            content = m.get("content")
            if isinstance(content, str):
                texts.append((j, None, content))
            elif isinstance(content, dict):
                for r in (content.get("tool_results") or []):
                    if isinstance(r, dict) and isinstance(r.get("content"), str):
                        texts.append((j, r, r["content"]))
        if not texts:
            return unit
        skeleton = sum(max(0, estimate_tokens(m)
                           - sum(estimate_tokens(t) for jj, rr, t in texts if jj == j))
                       for j, m in enumerate(unit))
        avail = max(0, budget - skeleton)
        sizes = [max(1, estimate_tokens(t)) for _j, _r, t in texts]
        total = sum(sizes) or 1
        for (j, r, text), alloc in zip(texts,
                                       [max(1, int(avail * s / total)) for s in sizes]):
            clipped = clip_fn(text, alloc)
            if r is None:
                unit[j] = {**unit[j], "content": clipped}
            else:
                unit[j] = {**unit[j], "content": {
                    **unit[j]["content"],
                    "tool_results": [{**tr, "content": clipped}
                                     if tr is r else tr
                                     for tr in unit[j]["content"].get("tool_results") or []]}}
        return unit

    def _trim_messages(self, messages: list[dict],
                       budget: int) -> tuple[list[dict], list[str], bool]:
        """预算裁剪（**只整块丢弃 + 末条保底折叠**；绝不逐条折叠——逐条折叠归块内准备）。

        与 ``clip_head_tail`` 职责**不重叠**，两者互不代劳（杜绝双重截断）。

        400-fix A：裁剪以 ``_pair_units`` 的**配对原子单元**为粒度——工具调用与其结果
        回灌同进同退，丢弃切点永不落在二者之间（杜绝 role=tool 失去前置 tool_calls 的 400）。

        顺序：
          1. **块内 pinned 先占**：按原顺序占额度（不参与 water-filling）；
          2. 额度用尽的剩余 pinned → **降级为普通条目**，其 id 记入返回值；
          3. 余额给非 pinned 单元：**从新到旧连续贪心**（保连续性，避免抽空中间）；
          4. 一个单元都没保留 → 末位单元 ``_clip_unit_hard`` 到预算（保对话不残）。

        返回 ``(保留条目[原顺序], 刚性失效的 pinned 条目 id, 是否发生末条保底折叠)``。
        """
        if not messages:
            return [], [], False
        if budget <= 0:                     # 无空间：pinned 全部刚性失效（诚实记录，不静默）
            return [], [self._msg_id(m, i) for i, m in enumerate(messages) if m.get("pinned")], False
        units = self._pair_units(messages)
        toks = [sum(estimate_tokens(messages[i]) for i in u) for u in units]
        pinned = [any(messages[i].get("pinned") for i in u) for u in units]
        keep_u: set[int] = set()
        downgraded: list[str] = []
        used = 0
        for k, u in enumerate(units):               # 1) + 2) pinned 单元先占（按原顺序）
            if not pinned[k]:
                continue
            if used + toks[k] <= budget:
                keep_u.add(k)
                used += toks[k]
            else:
                downgraded.extend(self._msg_id(messages[i], i)
                                  for i in u if messages[i].get("pinned"))
        for k in range(len(units) - 1, -1, -1):     # 3) 余额给非 pinned（连续贪心）
            if k in keep_u:
                continue
            if used + toks[k] <= budget:
                keep_u.add(k)
                used += toks[k]
            else:
                break
        clipped = False
        override: dict[int, dict] = {}              # 保底裁剪后的替换条目（按索引回填）
        if not keep_u:                              # 4) 末位单元保底（整单元保留，结构不破）
            u = units[-1]
            unit = [dict(messages[i]) for i in u]
            unit = self._clip_unit_hard(unit, budget, self._clip_hard)
            keep_u.add(len(units) - 1)
            override = {i: m for i, m in zip(u, unit)}
            clipped = True
        kept: list[dict] = []
        for k, u in enumerate(units):
            if k not in keep_u:
                continue
            kept.extend(override.get(i, messages[i]) for i in u)
        return kept, downgraded, clipped

    def assemble(self, blocks: list[BlockSpec], *, reserve_tokens: int = 0,
                 cost_factor: float = 1.0,
                 attachments: list[AttachmentSpec] | None = None,
                 phase: str = "default", scope: str | None = None) -> Assembly:
        """分块装配：注册制过滤 → 刚性先扣 → 可裁回吐分配 → 块内裁剪 → 互斥输出。

        - **注册制**：只对 `present` 且非空的块分配预算；权重在"存在的块"间重归一。
        - **刚性超限**：按 priority 升序（越重要越先满足）分配，次要块降级并**双通道标注**
          （`AssemblyReport.rigid_overflow` + `system_text` 内显式标注段），绝不静默截断。
        - **剩余回吐**：可裁块 water-filling——未用满余量回吐公共池再分配（`redistributed` 记账）。
        - **互斥**：`messages` 块按 `coverage`（左开右闭）排序，重叠时高阶块胜出，
          绝不双份注入；输出前剥离内部 `seq`。
        - **pinned（S3，通用元数据）**：`messages` 块内 `pinned` 条目**按原顺序先占额度**，
          额度不足者降级为普通条目并记入 `BlockReport.pinned_downgraded`（诚实、可追溯）。
        - **`reserve_tokens`（S3）**：为调用方**后续**块预留额度（如 react 循环的 chain 保底），
          从输入预算中先扣除后再分配——避免先装配的块吃满致后续块无空间。
        - **`cost_factor`（F1）**：估算→真实口径校准因子（``api.cost_factor`` 读取，
          调用方传入——治理器自身无 ctx，保持零状态）。预算整体 ÷ factor：因子 >1
          （真实计费高于估算）时窗口装得更少，使**真实**请求体积贴近窗口预算。
        - **`attachments`（G2.0 P0）**：请求附加项的**登记面**（如 native tools 数组）。
          **只记账、不改裁决**——有效预留取 ``max(reserve_tokens, Σ附件claimed)``，
          故只传 reserve 的既有调用方行为逐字节不变；两者都传（推荐）时不会重复扣减。
          登记的作用是让 ``unaccounted_tokens`` 具备可归因性（黑洞显影）。
        - **`phase` / `scope`（G2.0 P0）**：声明式注册的定位键——``missing_blocks``
          只针对 ``(scope, phase)`` 下已声明的必需块做完整性校验。缺省 ``"default"``，
          不声明过的 ``(scope, phase)`` 恒返回空表（零误报）。
        """
        factor = min(3.0, max(0.5, float(cost_factor or 1.0)))
        attach = [self._coerce_attachment(a) for a in (attachments or [])]
        attach_used = sum(max(0, int(a.claimed_tokens)) for a in attach if a.present)
        # 有效预留：reserve 与「已登记附件」同源占位 → 取较大值（绝不重复扣减）。
        # 不传 attachments 的既有调用方：reserve_eff == reserve_tokens，行为完全不变。
        reserve_eff = max(0, int(reserve_tokens or 0), attach_used)
        ib = max(0, int((self.input_budget() - reserve_eff) / factor))
        blocks = [self._coerce(b) for b in blocks]        # 兼容 dict 入参（消费层零 import）
        submitted = {b.key for b in blocks}
        active = [b for b in blocks if self._present_nonempty(b)]
        active_keys = {b.key for b in active}

        # 1) 刚性块先扣（priority 升序 = 越重要越先满足）
        rigid = sorted([b for b in active if b.rigid], key=lambda b: b.priority)
        flex = [b for b in active if not b.rigid]
        rigid_alloc: dict[str, int] = {}
        rigid_req: dict[str, int] = {}
        overflow_keys: list[str] = []
        remaining = ib
        for b in rigid:
            need = self._block_tokens(b.kind, b.payload)
            bcap = self._cap_of(b)                     # P1-5：层/显式 cap（0 = 无）
            want = min(need, bcap) if bcap > 0 else need
            rigid_req[b.key] = need
            give = min(want, max(0, remaining))
            rigid_alloc[b.key] = give
            remaining -= give
            if give < need:                            # 超 cap 或超预算 → 走刚性超限双通道
                overflow_keys.append(b.key)

        # 2) 可裁块分配（water-filling：未用满余量回吐公共池再分配）
        #    P1-5：块级 cap 作为**分配上限**参与 water-filling（want = min(demand, cap)）——
        #    未用满的 cap 余量自然回吐公共池（`pool` 不减满额），不浪费预算。
        pool = max(0, remaining)
        demand = {b.key: self._block_tokens(b.kind, b.payload) for b in flex}
        blk_cap = {b.key: self._cap_of(b) for b in flex}
        want = {k: (min(demand[k], blk_cap[k]) if blk_cap[k] > 0 else demand[k])
                for k in demand}
        cap: dict[str, int] = {b.key: 0 for b in flex}     # 可裁块最终分配（与 blk_cap 区分）
        work = [b for b in flex if want[b.key] > 0]
        total_w0 = sum(self._weight_of(b) for b in work) or 1.0
        init_share = {b.key: pool * self._weight_of(b) / total_w0 for b in work}
        while work:
            total_w = sum(self._weight_of(b) for b in work) or 1.0
            share = {b.key: pool * self._weight_of(b) / total_w for b in work}
            fit = [b for b in work if want[b.key] <= share[b.key]]
            if not fit:                       # 都超份额 → 按份额截断，收敛
                for b in work:
                    cap[b.key] = max(0, int(min(share[b.key], want[b.key])))
                break
            for b in fit:                     # 落定；余量自然回吐（pool 减少）
                cap[b.key] = want[b.key]
                pool -= want[b.key]
                work.remove(b)
            if pool <= 0:
                for b in work:
                    cap[b.key] = 0
                break
        redistributed = sum(max(0, int(init_share[b.key] - want[b.key]))
                            for b in flex if cap[b.key] >= want[b.key])

        # 3) 块内裁剪（text / messages）
        out_text: dict[str, str] = {}
        out_msgs: dict[str, list[dict]] = {}
        used: dict[str, int] = {}
        trimmed: dict[str, bool] = {}
        pin_down: dict[str, list[str]] = {}      # S3：刚性失效的 pinned 条目 id（可追溯）
        for b in active:
            alloc = rigid_alloc.get(b.key, 0) if b.rigid else cap.get(b.key, 0)
            if b.kind == "messages":
                msgs, down, clipped = self._trim_messages(list(b.payload), alloc)
                out_msgs[b.key] = msgs
                used[b.key] = self._block_tokens("messages", msgs)
                trimmed[b.key] = (len(msgs) < len(b.payload)) or bool(down) or clipped
                if down:
                    pin_down[b.key] = list(down)
            else:
                text, cut = self._trim_text(str(b.payload), alloc)
                out_text[b.key] = text
                used[b.key] = estimate_tokens(text)
                trimmed[b.key] = cut

        # 4) messages 互斥（左开右闭：相邻块边界相接；重叠则高阶块胜出，绝不双份注入）
        msg_blocks = sorted([b for b in active if b.kind == "messages"],
                            key=lambda b: (b.coverage[0] if b.coverage else 0))
        cover_hi = 0
        for b in msg_blocks:
            lo, hi = (b.coverage or (0, 0))
            if b.coverage is not None:
                kept: list[dict] = []
                for idx, m in enumerate(out_msgs[b.key]):
                    if "seq" not in m:        # 无 seq：信任调用方划分，不逐条过滤
                        kept.append(m)
                        continue
                    s = int(m["seq"])
                    # 左开右闭本块范围 (lo, hi] + 不与前块重叠（s > cover_hi）→ 越界/重叠即剔除
                    if s <= lo or s > hi or s <= cover_hi:
                        if m.get("pinned"):    # 互斥剔除亦可致刚性失效（诚实记录，不静默）
                            pin_down.setdefault(b.key, []).append(self._msg_id(m, idx))
                        continue
                    kept.append(m)
                if len(kept) != len(out_msgs[b.key]):
                    out_msgs[b.key] = kept
                    used[b.key] = self._block_tokens("messages", kept)
                    trimmed[b.key] = True
            cover_hi = max(cover_hi, hi)

        # 5) 输出（text 块按 priority 升序 _SEP 连接；messages 按 coverage.lo 排序、剥离 seq）
        text_parts = [out_text[b.key] for b in sorted(
            [b for b in active if b.kind == "text" and out_text.get(b.key)],
            key=lambda b: b.priority)]
        system_text = _SEP.join(text_parts)

        prior: list[dict] = []
        for b in msg_blocks:
            for m in out_msgs.get(b.key, []):
                prior.append({k: v for k, v in m.items() if k != "seq"})

        # 6) 报告（刚性超限双通道之一：report）
        reports: list[BlockReport] = []
        for b in blocks:
            if b.key not in active_keys:
                reports.append(BlockReport(b.key, b.present, b.kind,
                                           reason="absent" if not b.present else "empty"))
                continue
            alloc = rigid_alloc.get(b.key, 0) if b.rigid else cap.get(b.key, 0)
            req = rigid_req.get(b.key, demand.get(b.key, 0))
            u = used.get(b.key, 0)
            reason = ("downgraded" if b.key in overflow_keys
                      else "trimmed" if trimmed.get(b.key) else "ok")
            reports.append(BlockReport(b.key, b.present, b.kind, requested=req,
                                       allocated=alloc, used=u,
                                       trimmed=trimmed.get(b.key, False), reason=reason,
                                       pinned_downgraded=list(pin_down.get(b.key, [])),
                                       layer=b.effective_layer(),
                                       cap=self._cap_of(b)))
        total_used = estimate_tokens(system_text) + self._block_tokens("messages", prior)
        rigid_overflow = [r for r in reports if r.reason == "downgraded"]

        # 6.5) G2.0（P0）只读观测：记账 + 缓存友好度体检（**不参与任何裁决**）
        #   a) 稳定前缀 = 首个 volatile 块之前的装配量（窗口序：text 按 priority，messages
        #      按 coverage.lo）——F-1 类回归的直接体检指标；
        #   b) unaccounted = 预算 − Σ块used − Σ附件used（黑洞的唯一显式出口；含未用满余量，
        #      **关注其变化量**：登记附件后本值下降的数额即该附件此前的隐形占用）；
        #   c) missing_blocks = 已声明本阶段必需、但本次**连提交都没有**的块 key。
        order = ([b for b in sorted([b for b in active if b.kind == "text"],
                                    key=lambda b: b.priority)] + list(msg_blocks))
        volatile_blocks = [b.key for b in order if b.volatile]
        stable_prefix = 0
        for b in order:
            if b.volatile:
                break
            stable_prefix += used.get(b.key, 0)
        attachment_reports = [
            AttachmentReport(key=a.key, present=a.present,
                             claimed=max(0, int(a.claimed_tokens)),
                             used=max(0, int(a.claimed_tokens)) if a.present else 0)
            for a in attach
        ]
        accounted = sum(used.values()) + sum(r.used for r in attachment_reports)
        budget_raw = self.input_budget()               # P3：unaccounted 的对照基数（原始输入预算）
        unaccounted = max(0, budget_raw - accounted)

        report = AssemblyReport(blocks=reports, total_used=total_used, input_budget=ib,
                                budget_raw=budget_raw,
                                rigid_overflow=rigid_overflow, redistributed=redistributed,
                                attachments=attachment_reports,
                                unaccounted_tokens=unaccounted,
                                stable_prefix_tokens=stable_prefix,
                                volatile_blocks=volatile_blocks,
                                missing_blocks=self._missing_blocks(submitted, phase, scope))

        # 7) 刚性超限双通道之二：文本通道（显式标注段，绝不静默截断）
        if rigid_overflow:
            marks = "、".join(f"{r.key} 缺 {r.requested - r.used} tok" for r in rigid_overflow)
            mark = f"⚠️ 上下文预算超限：以下刚性内容已降级 [{marks}]"
            system_text = mark if not system_text else mark + _SEP + system_text

        return Assembly(system_text=system_text, prior=prior, report=report)


__all__ = ["ContextGovernor"]
