# 选品 Agent · 架构方案

> 归属：Meridian RPA 引擎扩展设计。
> 日期：2026-09-21。
> 状态：设计定稿（未实现，无代码）。
> 结论先行：**AI 当「能力插件 + Provider 可替换接缝」装进引擎，选品逻辑做成一个业务插件；MCP 只作为交互问答的薄挂件。**

---

## 1. 结论

**主架构（方案 A）：把 AI 作为可替换的 Provider 接入引擎，选品作为业务插件。**
**可选挂件：把引擎/选品工具薄薄包一层 MCP，供人肉交互问答。**

判断的唯一标尺是：**谁在驱动循环（who owns the loop）。**

- 你的引擎是为「长期稳定、自动运转、可观测」建的（调度器/心跳/看门狗/滚动重启/控制台/soak），选品是它天然该驱动的一类节奏活（每日批量打分、落库、出榜单）。
- 让外部 AI 持有循环 = 把引擎最值钱的「稳定性机制」全部缴出去。

---

## 2. 两种方案对照

| 维度 | A. AI 当插件进引擎 | B. 引擎包成 MCP 给 AI |
|---|---|---|
| 循环持有者 | 引擎调度器（确定、可审计） | 外部 LLM（每次往返驱动） |
| 数据访问 | `ctx.db` 一次批量读，本地 SQLite | 每轮推理经工具往返捞数据 |
| 节拍模型 | 3 分钟快照/每日批量，正确形状 | MCP「工具调用要快」，3 分钟节拍是错形状 |
| 离线可测 | `OfflineFixture` 下确定性全绿 | 依赖外部大脑，失去确定性 |
| 成本/token | 一次批量推理，落库核算 | 重复捞取 + 推理，token 双烧 |
| 合规边界 | 引擎侧守住「只读可见 DOM」 | 外部 LLM 直摸浏览器原语，双失控 |
| 可用性 | 本地引擎随时在跑 | 外部大脑不在线即停摆 |
| 哲学自洽 | 「无特权内核，一切皆插件」 | AI 成为特权内核，反哲学 |

**A 是运行时核心，B 永远是薄挂件 —— 不与 MCP 二选一，而是 A 为主、B 可选。**

---

## 3. 为什么是 A（对应你代码里的证据）

你的代码里接缝已经画好，这不是新机制，是**填空**：

1. **AI Provider 接缝已存在。**
   `tiktok/analysis.py` 已有 `AnalysisProvider` 协议、`ClaudeLlmProvider`（`ENGINE_ANALYSIS_KEY` 诚实边界）、`OfflineFixtureLlm`（离线确定性夹具）—— 正是 `engine/seams/browser/definition.py` 那套 `interface_version` + Provider 可替换 + 离线可测的模式。选品推理接口照抄这个形状，零新机制。

2. **数据归属本地。**
   选品要的是历史累进（销量变化量、观察天数）—— 全在 `tt_ticks`/`tt_products`/category 表中。AI 在引擎内直接 `ctx.db` 批量读，落库时复刻 `tokens_used` 核算；包成 MCP 后每轮工具往返白烧。

3. **调度与审计就为它而建。**
   选品是节奏活（每日 shortlist、每周报告），`scheduler`（interval 节拍）、`ctx.command`（创建任务即触发）、run 审计表是现成的承载。

4. **合规边界须由引擎侧守住。**
   `impl.py` 的浏览器 seam 全部是「只读可见 DOM、模拟真人」。AI 作为插件只能经你筛选过的工具出手 —— 不能让外部 LLM 直摸浏览器原语乱逛（成本 + 合规双失控）。

5. **离线确定性测试是地基。**
   你 125 项测试全绿依赖 `OfflineFixtureLlm` 这类夹具。B 主架构下这个属性基本丢失。

6. **哲学自洽。**
   README：无特权内核，一切具体能力都是插件。A 就是把 AI 做成同级插件/Provider；B 等于立起你一直避免的特权内核。

---

## 4. 目标架构（三层）

```
选品agent 全景
│
├─ engine/seams/llm/              ← 机制 seam（照抄 browser seam，零业务名词）
│    definition.py   LlmProvider Protocol（interface_version + 重试/schema/token 核算）
│    impl.py         离线夹具 OfflineFixtureLlm · 真实 ClaudeLlmProvider（ENGINE_ANALYSIS_KEY）
│
├─ plugins/product_selection/     ← 业务插件（一个业务 = 一个插件，自带给建表/领域方法）
│    __init__.py      @plugin ProductSelection  → ctx.selection
│    migrations.py    自带表 ps_candidates / ps_runs / ps_scores / ps_picks
│    scoring.py       评分管线（读候选 → batch 打分 → 校验 → 落库 → token 核算）
│    ranking.py       榜单/tool 面（shortlist / weekly xlsx 导出）
│
└─ plugins/mcp_bridge/            ← 可选薄挂件（交互问答，不做运行时核心）
     __init__.py     把 ctx.selection 的工具面映射成 MCP tools 挂给 Claude Code/Claude.ai
```

### 4.1 关键机制（seam 层）—— 与现有模式对齐

- `LlmProvider` Protocol 带 `interface_version`，Provider 可按 profile 替换，业务零改动（对齐 `BrowserProvider`）。
- 推理统一走 `dispatch(ctx, provider, prompt_bundle, schema)`：重试 → schema 校验 → token 核算 → 落库；非法×2 跳过不落脏（对齐 `tiktok/analysis.py` 的 `validate_dimension` + 重试逻辑）。
- 未配 `ENGINE_ANALYSIS_KEY` → `CapabilityNotImplemented`（诚实边界，不虚标），离线跑 fixture。
- Entry 可以是 tiktok/analysis 的**通用化**，也可以是新 seam —— 建议把已有 domain 侧 Provider 提升为 mechanism seam 让两者复用同一套核算/重试。

### 4.2 选品插件（业务层）

**provides：** `selection`
**inject：** `llm`(或 analysis) · `db` · `migrations` · `scheduler` · `browser`(选配，抓新候选用)

**评分维度（四维 + 总分，延续现有风格）：**

| 维度 | 输入信号 | 说明 |
|---|---|---|
| demand 需求 | 销量增速（tt_ticks 差分）、评论增量、类目热度 | 起点是「真实增长」，不是猜测 |
| margin 利润 | 前台价 vs 估进价（成本带）、价格带分布 | 决定能不能做 |
| trend 趋势 | 生命周期阶段、动量、观察天数 | 别进衰退期的局 |
| fit 契合 | 目标受众、定位、卖点、与自身供应链匹配 | 业务 profile 可注入偏好 |

**数据模型（表设计草案）：**

```
ps_candidates   候选池：product_id / category / title / price / currency /
                signals_snapshot(json) / source / captured_at
                （从 tt_products / tt_ticks 差分 + category 扫描聚合而来）
ps_runs         评分审计：run_id / batch / params(json) / 状态 / 起止 / 条数
ps_scores       逐候选逐维打分：run_id / product_id / dimension /
                result_json / total / rank / model / tokens_used / scored_at
ps_picks        最终短名单：product_id / rank / score / decision / notch(json) /
                备注 / picked_at
```

**工作流（两个节奏，均由调度器驱动）：**

```
每日：跃迁候选（今日 changed 的 tt/类目行）→ batch 打分 → 落 ps_scores → 排榜 → ps_picks
每周：榜单合并 → weekly 报告导出（openpyxl 已就位）→ 落 report/
```

触发方式两种并存：`ctx.command`（控制台「创建任务 → 运行插件」）与 `scheduler.interval`。run 审计保证可回溯（`ps_runs`）。

### 4.3 工具面（tool face）——为交互问答预留的受控接口

> 这批操作是**有界的读/触发**，不暴露浏览器原语。MCP bridge 只映射这里，LLM 永远摸不到 `browser.open/read` 之外的能力。

- `selection.candidates_query`   按类目/时间窗/价格带查候选（只读）
- `selection.candidate_report`   取单候选的信号历史（只读）
- `selection.run_score`          触发一轮评分（受控写，走审计）
- `selection.pick_list`          当前短名单（只读）
- `report.weekly`                导出周报 xlsx（只读）

---

## 5. MCP 什么时候出现（薄挂件，不是核心）

`engine/core/context.py` 的 `ctx.command(name, schema, handler)` 已经是 MCP tool 的形状（name/description/schema/handler）。
做交互式场景时，加 `plugins/mcp_bridge/` 薄适配器，把 `ctx.selection` 的**工具面**映射成 MCP tools，挂给 Claude Code / Claude.ai —— 让你人肉也能「把引擎当外设」问答选品。

**这是交互答疑入口，不是 autopilot 核心，别让它反客为主。**

---

## 6. 两个产品定义的提醒

「选品 agent」可指两种东西，别混：

| 形态 | 驱动 | 对应方案 |
|---|---|---|
| **自动选品机** | 引擎定节奏跑，你只看结果 | A（核心，先做） |
| **选品问答外设** | 你在 Claude 里问、它查数据论证 | B（MCP 薄挂件，有需要再做） |

引擎是为前者建的，**先做 A**。

---

## 7. 诚实边界（延续项目精神）

- 真实 LLM 依赖 `ENGINE_ANALYSIS_KEY`；未配 → `CapabilityNotImplemented`，离线走 fixture，不做虚标。
- 评分质量依赖采集数据质量；`signals_snapshot` 记录当时口径，历史回溯用 `ps_scores.result_json` 而非重算。
- 采集仍守「只读可见 DOM、模拟真人」合规线；AI 打分不吃站点内部接口数据。

---

## 8. 里程碑拆分（建议，未动工）

- **M1** engine/seams/llm 机制 seam + fixture/Claude Provider（对齐 analysis.py）
- **M2** plugins/product_selection 骨架：建表 + 候选聚合 + 批量打分落库（fixture 全绿）
- **M3** 排榜 / shortlist / 周报 xlsx；控制台观测挂 ps_runs 进度
- **M4（可选）** plugins/mcp_bridge：工具面映射 MCP，交互问答

> 本方案只到设计。实现时从 M1 开始逐步填，每个里程碑保持离线确定性测试全绿。