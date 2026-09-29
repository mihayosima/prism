"""agent.plugins.context.protocols —— 治理器接缝契约（G0 固化，G1 扩面）。

``ctx.get("context.governor")`` 注入的实现须满足本协议；契约留在插件内
（与 memory 三组件同构），Driver/消费方只依赖协议面，不依赖默认实现。

G1 新增**分块装配契约**（纯数据，chat 构造、governor 消费）：
``BlockSpec`` / ``BlockReport`` / ``AssemblyReport`` / ``Assembly``。
消费方不触插件内部实现。

G2.0（P0 只读观测 + 前缀对齐）扩面：
- ``BlockSpec.volatile``：该块内容**跨请求可能变化**的标记——用于算
  ``AssemblyReport.stable_prefix_tokens``（缓存友好度的直接度量），不参与任何裁决；
- ``AttachmentSpec`` / ``AttachmentReport``：**请求附加项**（native tools 数组、
  response_format 等）的登记面——此前它们「扣预算但不登记」（记账盲区），
  现予以显式入账；**登记不改变预算裁决**（额度仍由 ``reserve_tokens`` 表达）；
- ``AssemblyReport.unaccounted_tokens``：预算黑洞的唯一显式出口；
- ``BlockDecl`` + ``GovernorLike.declare``：补上**声明方向**（消费方宣告「本阶段
  应该有哪些块」），使漏报可被结构性发现（单向 pull 无此能力）。
  声明属**配置期**行为（与 ``from_config`` 同级，只读常量），不违反运行期零状态纪律。

G2.0（P1 契约扩展：字段与水位）：
- ``BlockSpec`` 增**层与治理标记**六个字段（``layer`` / ``cap_tokens`` / ``compact_at``
  / ``compact_floor_ratio`` / ``on_compact`` / ``regenerable``），全部有缺省 → 旧调用逐字节等价；
- ``resolve_layer``：``"" → "resident" if rigid else "runtime"``（缺省推导，硬约束）；
- ``WaterlineReport`` + ``GovernorLike.assess``：**纯函数水位判定**（同样入参恒得同样结果）；
- 治理组（C 组：declare / assess …）与装配组（B 组：assemble）统一带 ``scope`` 留位。

G2.0（P2 治理动词：**首个改变装配行为的批次**）：
- ``Action`` / ``ApplyResult``：把「该不该动、动哪块、动了多少、是否回滚」变成**结构化事实**；
- ``GovernorLike.plan``：**纯函数决策表**（keep → clear → evict → compact → clip，代价递增）；
- ``GovernorLike.execute``：把决策落到块上，守住四条不变量（执行前先校验、执行后复校，
  任一失败 → 回滚该动作并记 ``degraded``）；``on_compact`` 回调由消费方注入——治理器
  只调用、不理解其内部（纯度纪律）；
- **压缩粒度 = 原子单元（硬约束）**：``react_chain`` 这类 messages 块**不得**在消息序列上
  任意切分，切分单位固定为 wire 原子单元 ``[assistant{reasoning_content}] + [tool_calls]
  + [tool results]``，唯一合法动作是「整单元 → 过程摘要」；切点落在单元内部一律判 I-2
  违规并回滚。
- ``mode`` ∈ ``off`` / ``shadow`` / ``on``（缺省读配置 ``compact_mode``）：``shadow`` 下
  ``plan`` 照常计算、``execute`` **不执行**（只把决策与理由写遥测）——这是让阈值用真实
  数据校准的唯一安全方式。

G2.0（P3 视图与不变量）：
- ``AssemblyReport.budget_raw``：``unaccounted_tokens`` 的**对照基数**（原始输入预算）——
  使 **I-4 预算守恒**（``Σ块used + Σ附件used + unaccounted = budget_raw``）可在载荷内**自证**；
  ``input_budget`` 仍是**生效**预算（已扣预留并作因子校准，用于实际分配）。
- ``GovernorLike.ledger`` / ``render_ledger``：**Inspector 门面**（``/context`` 分类视图 +
  轮次全景账本 ``RoundLedger``）——纯函数、零状态；消费方经服务**软调用**、不 import 插件内部。
  四条不变量中，I-1/I-2 在 ``execute`` 的**装配对象面**守护（回滚）；I-3/I-4 在**记账面**由
  ``check_invariants`` 提供，供每次装配后「记录 + 告警（不阻断）」。

S3 扩面（循环内上下文治理）：
- ``BlockSpec.rigid`` 语义钉死为「**不参与可裁池**」，**不等于「绝对不裁」**——刚性块自身
  超预算时仍按 priority 降序降级，并双通道标注（见 ``AssemblyReport.rigid_overflow``）；
- messages 块条目新增**通用元数据** ``pinned``（任何 messages 块可携带）：
  块内**先占额度**，额度不足者降级为普通条目，记入 ``BlockReport.pinned_downgraded``。
  —— 注意：``pinned`` 是**通用**词义，治理器不感知任何业务语义（既不认识「任务进度」，
  也不认识任何循环步骤类型；条目 ``type`` 属消费方私有，治理器只认 ``pinned``）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

_LAYER_RESIDENT = "resident"
_LAYER_RUNTIME = "runtime"

# G2.0（P2）：治理模式取值域（``compact_mode`` 配置的合法枚举；三处同步校验同源）。
COMPACT_MODES = ("off", "shadow", "on")

# G2.0（P2）：``Action.kind`` 取值域（与 4.2.4 决策表同源；代价从低到高）。
ACTION_KINDS = ("keep", "clear", "evict", "compact", "clip")


def resolve_layer(layer: str, rigid: bool) -> str:
    """**层缺省推导**（G2.0 P1 硬约束）：``"" → "resident" if rigid else "runtime"``。

    `layer` 是「策略归属」的显式声明（resident / on_demand / memory / runtime / input）。
    未标注时必须**可无损推导**，使旧调用方零改动即保持既有语义（刚性=常驻、可裁=运行时）。
    """
    if layer:
        return str(layer)
    return _LAYER_RESIDENT if rigid else _LAYER_RUNTIME


@dataclass(frozen=True)
class BlockSpec:
    """一个可装配块（注册制：块自声明存在性，治理器动态组装）。

    ``coverage`` 语义（**一律「左开右闭」`(lo, hi]`**，全链统一）：
    - `messages` 块专用；**含 `hi`、不含 `lo`**；
    - 现行实例：`summary=(0, covers]`、`history=(covers, N]`，`covers` 取自
      `covers_up_to_seq`（SSOT）；任意两块区间两两不相交；
    - 该边界由 **chat 读取后作入参传入**（非 governor 反向查询），避免
      governor→chat/short_term 循环依赖。
    """

    key: str
    kind: str                                   # "text" | "messages"
    present: bool
    payload: str | list[dict] = ""
    weight: float = 0.0                         # 可裁块预算权重；<=0 → 用治理器按 key 的默认
    priority: int = 100                         # 越小越先保留（装配顺序 / 超限裁剪逆序）
    rigid: bool = False                         # True = 刚性（先占预算）；自身超限仍会降级并标注
    coverage: tuple[int, int] | None = None     # messages 专用（左开右闭）
    volatile: bool = False                      # G2.0：内容跨请求可能变 → 稳定前缀体检用（不参与裁决）
    # ---- G2.0（P1）：块分层与治理标记（全部有缺省 → 旧调用方逐字节等价）----
    layer: str = ""                             # "" = 由 rigid 推导（见 resolve_layer）
    cap_tokens: int = 0                         # 显式 cap；0 = 无显式 cap（缺省按 layer 取配置 cap）
    compact_at: int = 0                         # 压缩触发阈值；0 = 不参与自动压缩触发
    compact_floor_ratio: float = 0.4            # 压缩后目标残留比 r（回本公式用）
    on_compact: Callable[[Any, int], Any] | None = None   # 压缩回调（消费方注入；治理器只调不理解）
    regenerable: bool = False                   # True = 可清除后重建 → clear 候选（零推理成本）

    def effective_layer(self) -> str:
        """本块生效层：显式 ``layer`` 优先，否则按 ``rigid`` 推导（见 ``resolve_layer``）。"""
        return resolve_layer(self.layer, self.rigid)


@dataclass(frozen=True)
class AttachmentSpec:
    """一个**请求附加项**（不在 messages 预算内，但计入真实 prompt_tokens）。

    典型：``react`` 步的 native tools 数组（本次事故的记账黑洞）、``response_format``。
    消费方用 ``core.request_cost.estimate_request_cost`` 算好 ``claimed_tokens`` 后传入——
    治理器**不重复实现**该估算（零特权纪律），只登记与对账。
    ``payload`` 可选：供 Inspector 用另一把尺子复算、交叉校验。

    P0 语义（**只读观测**）：登记**不改变预算裁决**——可分配额度仍由 ``reserve_tokens``
    表达（贵在兼容：既有调用方行为逐字节不变）。登记的作用是把「扣了但没登记」的部分
    显式化，使 ``AssemblyReport.unaccounted_tokens`` 具备可归因的意义。
    """

    key: str
    claimed_tokens: int = 0
    present: bool = True
    payload: Any = None


@dataclass(frozen=True)
class AttachmentReport:
    """单个附加项的登记结果（可观测）。"""

    key: str
    present: bool = True
    claimed: int = 0                            # 消费方自报占用
    used: int = 0                               # 实际计入预算的占用（P0：present 时等于 claimed）


@dataclass(frozen=True)
class BlockReport:
    """单块装配结果（可观测）。"""

    key: str
    present: bool
    kind: str
    requested: int = 0                          # 块自报需求 token
    allocated: int = 0                          # 分配上限 token
    used: int = 0                               # 实占 token
    trimmed: bool = False                       # 是否被裁剪
    reason: str = ""                            # absent / empty / ok / trimmed / downgraded
    pinned_downgraded: list[str] = field(default_factory=list)
    # ---- G2.0（P1）：分层与 cap 可观测（缺省空/0 → 旧消费方零影响）----
    layer: str = ""                             # 生效层（resolve_layer 结果）
    cap: int = 0                                # 生效 cap（0 = 无 cap）
    # ↑ 刚性失效的 pinned 条目 id（块内额度不足者；可能被丢弃或截短）。
    #   无降级时为空 list（字段不省略，便于消费方直接读）。


@dataclass(frozen=True)
class AssemblyReport:
    """装配总报告（结构化 dataclass，非裸 dict）。"""

    blocks: list[BlockReport] = field(default_factory=list)
    total_used: int = 0
    input_budget: int = 0
    # ---- G2.0（P3）：``unaccounted_tokens`` 的**对照基数**（I-4 预算守恒自证）----
    # ``input_budget`` 是**生效**预算（已扣 ``reserve`` 并 ÷ ``cost_factor``，用于实际分配）；
    # 而黑洞 ``unaccounted_tokens`` 的语义是「原始输入预算未被任何块/附件占用者」，故其
    # 对照基数是**原始**输入预算 ``budget_raw``。恒等式：``Σ块used + Σ附件used + unaccounted
    # == budget_raw``（accounted ≤ budget_raw 时精确成立；否则记 I-4 违规）。
    budget_raw: int = 0
    rigid_overflow: list[BlockReport] = field(default_factory=list)  # 刚性超限被降级者
    redistributed: int = 0                      # 回吐再分配的总 token
    # ---- G2.0（P0）新增：只读观测字段（全部有缺省 → 旧消费方零影响）----
    attachments: list[AttachmentReport] = field(default_factory=list)
    # ↑ 已登记的请求附加项（空 list 表示消费方未登记 → 其占用将落进 unaccounted）
    unaccounted_tokens: int = 0
    # ↑ 预算黑洞的唯一显式出口 = input_budget − Σ块used − Σ附件used。
    #   注：该差值同时含「未用满余量」与「未登记预留」，**关注其变化量**：
    #   消费方登记附件后本值下降的数额，即该附件此前的隐形占用（现存：native tools）。
    stable_prefix_tokens: int = 0
    # ↑ 首个 volatile 块之前的已装配 token 数——缓存友好度直接度量（F-1 体检指标）
    volatile_blocks: list[str] = field(default_factory=list)
    # ↑ 标记了 volatile 的块 key（装配顺序）
    missing_blocks: list[str] = field(default_factory=list)
    # ↑ 已被 declare 声明为本阶段必需、但本次未提交（或提交为空）的块 key——漏报红灯


@dataclass(frozen=True)
class BlockDecl:
    """一个块的存在性声明（**配置期**只读常量，非运行期状态）。

    用途（补上契约缺失的「声明方向」）：
      1. ``AssemblyReport.missing_blocks`` 完整性校验（漏报变红灯）；
      2. ``unaccounted_tokens`` 的归属参考；
      3. 后续 ``/context`` 视图的「应有 vs 实有」对照。
    """

    key: str
    kind: str = "text"                          # "text" | "messages"
    owner: str = ""                             # 消费方自报标识（仅作归集与告警归属）
    required: bool = True                       # True = 本阶段必须有；缺失进 missing_blocks
    layer: str = ""                             # 预留（G2.0 P1 的块分层；空 = 未标注）
    cap_tokens: int = 0                         # 预留（P1；0 = 无 cap）


@dataclass(frozen=True)
class Assembly:
    """装配产物：system 文本 + prior 消息 + 报告。"""

    system_text: str
    prior: list[dict]
    report: AssemblyReport


@dataclass(frozen=True)
class WaterlineReport:
    """**水位判定报告**（``assess`` 的纯函数产物：同样入参恒得同样结果，无状态）。

    - ``working_window``：**经济基数**（水位分母），与 ``capability_window``（1M 能力上限）解耦；
    - ``level``：``"normal" | "high"``——只由 ``load_ratio`` 与 ``window_high_ratio`` 决定；
    - ``suggested``：粗粒度建议（``none`` / ``clear`` / ``compact`` / ``evict`` / ``clip``）。
      P1 下 assess 不见块清单，故 high 时回落 ``"clip"``（保底、不做结构性改动）；
      P2 的 ``plan`` 才据块清单给出精确动作；
    - ``gates``：三重闸判定明细（可审计、可回放）——threshold / timing / payback / release。
    """

    used: int
    working_window: int
    capability_window: int
    load_ratio: float
    level: str
    suggested: str = "none"
    gates: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Action:
    """一条治理动作（``plan`` 的产物；纯数据，交给 ``execute`` 落位）。

    ``kind`` ∈ ``{"keep","clear","evict","compact","clip"}``（见 ``ACTION_KINDS``）。
    ``expected_release`` 是**预计**释放量（可审计）；``payload`` 是交给 ``execute`` 的
    透传参数（如目标残留比），治理器不解释其内部语义。
    """

    kind: str
    target: str = ""                    # 块 key；``keep`` / ``clip`` 等无目标动作为空串
    scope: str | None = None
    expected_release: int = 0
    reason: str = ""
    payload: Any = None


@dataclass(frozen=True)
class ApplyResult:
    """``execute`` 的结果（结构化事实；供遥测与消费方判断）。

    - ``applied``：已执行动作的描述串（如 ``"clear:tools"``）；
    - ``skipped``：``(target, 跳过原因)``——含 ``keep``/``clip``（非结构性动作）、
      不满足前置条件者、以及不变量校验失败被回滚者；
    - ``released``：实际释放 token；
    - ``invariants``：四条不变量的校验结果（``{"I-1": True, ...}``）；
    - ``degraded``：是否有失败降级（任一动作被回滚即 True）。
    """

    applied: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    released: int = 0
    invariants: dict[str, bool] = field(default_factory=dict)
    degraded: bool = False


class GovernorLike(Protocol):
    """上下文治理器契约：预算查询 + 历史收窗 + 分块装配（G1）+ 循环内预算（S3）
    + 声明（P0）+ 水位判定（P1）。

    治理组（C 组）方法统一带 ``scope: str | None = None``——**不透明归集键**（子 agent
    拓展性留位，成本近零；治理器只把它当字典键，绝不据此做控制流分支）。
    """

    def input_budget(self) -> int: ...

    def memory_budget(self) -> int: ...

    def history_budget(self) -> int: ...

    def source_clip_budget(self) -> int: ...

    # ---- S3：循环内（react）预算口径 ----
    def react_budget(self, prior_tokens: int) -> int: ...
    def react_keep_recent(self) -> int: ...
    def react_min_chain_tokens(self) -> int: ...
    def clip_max_tokens(self) -> int: ...
    def clip_head_tokens(self) -> int: ...
    def clip_tail_tokens(self) -> int: ...
    def clip_head_tail(self, text: str, *,
                       head: int | None = None,
                       tail: int | None = None) -> tuple[str, bool]: ...

    def pick_history(
        self,
        messages: list[dict],
        budget: int | None = None,
        *,
        keep_first: bool = False,
    ) -> tuple[list[dict], int]: ...

    def assemble(self, blocks: list[BlockSpec], *, reserve_tokens: int = 0,
                 cost_factor: float = 1.0,
                 attachments: list[AttachmentSpec] | None = None,
                 phase: str = "default", scope: str | None = None) -> Assembly: ...

    # ---- G2.0（P0）：声明式注册（配置期，幂等；非运行期状态）----
    def declare(self, decls: list[BlockDecl], *, phase: str = "default",
                scope: str | None = None) -> None: ...

    # ---- G2.0（P1）：水位判定（纯函数：同样入参恒得同样结果，无状态）----
    def assess(self, used: int, *,
               working_window: int | None = None,
               remaining_steps: int | None = None,
               phase: str | None = None,
               scope: str | None = None,
               blocks: list[BlockSpec] | None = None) -> WaterlineReport: ...

    # ---- G2.0（P2）：治理动词（plan 纯函数决策 / execute 落位并守不变量）----
    def plan(self, report: WaterlineReport, *,
             blocks: list[BlockSpec] | None = None,
             mode: str | None = None) -> list[Action]: ...

    def execute(self, actions: list[Action], blocks: list[BlockSpec], *,
                mode: str | None = None) -> tuple[list[BlockSpec], ApplyResult]: ...

    # ---- G2.0（P3）：Inspector 门面（``/context`` 视图 + 轮次账本；纯函数、零状态）----
    # 消费方经服务**软调用**（``getattr(gov, "ledger", None)``），不 import 插件内部；
    # 注入实现不提供即降级跳过（与 ``assess`` 同纪律）。
    def ledger(self, payloads: Any = None) -> Any: ...

    def render_ledger(self, payloads: Any = None, *, tolerance: int = 0) -> str: ...


__all__ = [
    "BlockSpec",
    "BlockReport",
    "AssemblyReport",
    "Assembly",
    "AttachmentSpec",
    "AttachmentReport",
    "BlockDecl",
    "WaterlineReport",
    "Action",
    "ApplyResult",
    "COMPACT_MODES",
    "ACTION_KINDS",
    "GovernorLike",
    "resolve_layer",
]
