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
"""

from __future__ import annotations

from typing import Any

from agent.api import clip_text, clip_text_hard, estimate_tokens

from .protocols import Assembly, AssemblyReport, BlockReport, BlockSpec

# 可裁块默认权重（``BlockSpec.weight <= 0`` 时按 key 取；治理器自有口径，不依赖任何插件）。
_DEFAULT_WEIGHTS = {"memory": 0.10, "tools": 0.20, "history": 0.60, "summary": 0.10}
_DEFAULT_WEIGHT = 0.10
_SEP = "\n\n"          # 非空 text 块连接符（与原 system 拼接语义一致 → 对拍逐字节等价）


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
        )

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
                 cost_factor: float = 1.0) -> Assembly:
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
        """
        factor = min(3.0, max(0.5, float(cost_factor or 1.0)))
        ib = max(0, int((self.input_budget() - max(0, int(reserve_tokens))) / factor))
        blocks = [self._coerce(b) for b in blocks]        # 兼容 dict 入参（消费层零 import）
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
            rigid_req[b.key] = need
            give = min(need, max(0, remaining))
            rigid_alloc[b.key] = give
            remaining -= give
            if give < need:
                overflow_keys.append(b.key)

        # 2) 可裁块分配（water-filling：未用满余量回吐公共池再分配）
        pool = max(0, remaining)
        demand = {b.key: self._block_tokens(b.kind, b.payload) for b in flex}
        cap: dict[str, int] = {b.key: 0 for b in flex}
        work = [b for b in flex if demand[b.key] > 0]
        total_w0 = sum(self._weight_of(b) for b in work) or 1.0
        init_share = {b.key: pool * self._weight_of(b) / total_w0 for b in work}
        while work:
            total_w = sum(self._weight_of(b) for b in work) or 1.0
            share = {b.key: pool * self._weight_of(b) / total_w for b in work}
            fit = [b for b in work if demand[b.key] <= share[b.key]]
            if not fit:                       # 都超份额 → 按份额截断，收敛
                for b in work:
                    cap[b.key] = max(0, int(share[b.key]))
                break
            for b in fit:                     # 落定；余量自然回吐（pool 减少）
                cap[b.key] = demand[b.key]
                pool -= demand[b.key]
                work.remove(b)
            if pool <= 0:
                for b in work:
                    cap[b.key] = 0
                break
        redistributed = sum(max(0, int(init_share[b.key] - demand[b.key]))
                            for b in flex if cap[b.key] >= demand[b.key])

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
                                       pinned_downgraded=list(pin_down.get(b.key, []))))
        total_used = estimate_tokens(system_text) + self._block_tokens("messages", prior)
        rigid_overflow = [r for r in reports if r.reason == "downgraded"]
        report = AssemblyReport(blocks=reports, total_used=total_used, input_budget=ib,
                                rigid_overflow=rigid_overflow, redistributed=redistributed)

        # 7) 刚性超限双通道之二：文本通道（显式标注段，绝不静默截断）
        if rigid_overflow:
            marks = "、".join(f"{r.key} 缺 {r.requested - r.used} tok" for r in rigid_overflow)
            mark = f"⚠️ 上下文预算超限：以下刚性内容已降级 [{marks}]"
            system_text = mark if not system_text else mark + _SEP + system_text

        return Assembly(system_text=system_text, prior=prior, report=report)


__all__ = ["ContextGovernor"]
