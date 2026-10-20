# 工作流编排框架调研（LangGraph / AutoGen / Microsoft Agent Framework / LlamaIndex / Temporal / n8n）

> 编制时间：2026-10-15 23:07（GMT+8）
> 文档定位：**独立于《工作流编排方案-2026-10-15.md》的调研文档**。回答"如果要接入主流编排框架，要怎么改造"，并给出选型决策。
> 调研方法：WebSearch 检索 2026 年最新资料（框架均含 2026 年状态/版本），结合本仓 `ctx.tasks` 基座实情对比。
> 状态：**调研文档，未动代码**。

## 更新记录

| 时间 | 版本 | 变更摘要 |
|---|---|---|
| 2026-10-15 23:07 | v1.0 | 初版。逐一剖析 LangGraph / AutoGen（含继任 Microsoft Agent Framework）/ LlamaIndex Workflows / Temporal / n8n；横向对比矩阵；与本仓 `ctx.tasks` 基座对照；给出四种接入改造路径（自研 / LangGraph / MAF / Temporal）与决策建议；衔接既有工作流方案。 |

---

## 1. 调研目的与边界

用户诉求："不仅仅是单个工作流，需要所有工作流的编排方案，现有 MCP + Skill + 工作流编排才能做业务。"
本仓已有《工作流编排方案-2026-10-15.md》——基于 `ctx.tasks` 自研轻量 DAG 引擎。**本调研评估"直接采用现成编排框架"是否更优，以及接入要改造什么。**

核心判断前提（已核证）：本仓 `agent/sys/tasks.py` 把所有能力（MCP 工具、ASR、Skill 的 `run`、原生 tool）统一注册为 `Task`（`invoke`+schema+owner+risk+审计），是一个**天然的轻量编排基座**。任何外部框架接入，本质都是"把现有 `ctx.tasks` 能力适配成该框架的执行单元"。

---

## 2. 主流框架逐一剖析

### 2.1 LangGraph（LangChain 团队）
- **核心抽象**：`StateGraph` = 共享 `State`（TypedDict）+ `Node`（函数：收 state、返 state 增量）+ `Edge`（固定/条件路由，支持循环 back-edge）。
- **关键能力**：① 循环是一等公民（ReAct 循环、retry 循环）；② **Checkpoint 持久化**（InMemorySaver dev / PostgresSaver prod），每节点落快照 → 崩溃恢复、暂停/恢复、time-travel 调试；③ `interrupt()` 原生 human-in-the-loop 审批闸门；④ 编译期结构校验（孤立节点检测）。
- **成熟度/许可**：2026 年稳定 1.x，MIT，生产级，LangSmith 可追踪。
- **与本项目契合度**：强在"显式图 + 持久化 + 人机协同 + 可视化"。但它**要求把业务写成 `StateGraph`**，即要把现有 `ctx.tasks.invoke(...)` 包成 LangGraph 的 `Node`，把工作流变量换成 `State` 字段 + reducer 合并。引入重依赖（langgraph + langchain 生态，数十 MB）。

### 2.2 AutoGen（微软研究院）→ 已进维护模式
- **现状（关键）**：2026 年初 AutoGen **进入 maintenance mode**，官方新用户指向继任者 **Microsoft Agent Framework**。继续用仅适合维护既有部署。
- **核心抽象**：三层（Core 事件驱动 pub/sub runtime、AgentChat 高层 Agent/Team、Extensions）。编排模式：RoundRobin / Selector（LLM 选发言者）/ Swarm（handoff）/ DiGraph（DAG）。
- **特点**：actor-model 消息传递、可分布式 gRPC runtime、code-executor 沙箱、MCP 适配器成熟。
- **与本项目契合度**：若接 AutoGen 实为接 MAF（见 2.3），否则不应新项目选 AutoGen v0.4。

### 2.3 Microsoft Agent Framework（MAF，AutoGen + Semantic Kernel 合并）
- **现状**：2025-10 发布、2026-04-03 宣布 1.0 GA（.NET + Python，MIT）。是 AutoGen 与 Semantic Kernel 的直接继任，**同团队打造**。
- **核心抽象**：统一 `Agent` 基类 + `Workflow`（图工作流：executor 节点 + typed edge，superstep + checkpoint）+ 四种预置编排（sequential/concurrent/handoff/group-chat）+ 企业件（session 状态、middleware、telemetry、MCP 客户端、Foundry 集成）。
- **关键能力**：① **工作流与 Agent 可声明式 YAML 定义、版本化**；② checkpoint 持久化（每 superstep 落盘，支持恢复到任意检查点/跨环境 rehydrate）；③ "If you can write a function to handle the task, do that instead of using an AI agent"——明确区分确定性与智能体。
- **与本项目契合度**：能力最全、最接近"三支柱"诉求（Agent + Workflow + 集成）。但**深度绑定微软生态**（虽 1.0 宣称不绑 Azure，但 Foundry/Entra ID/M365 集成是主卖点），引入即把内核范式迁到 MAF 的 Agent/Workflow/Runtime，等于**重写编排层**。

### 2.4 LlamaIndex Workflows（事件驱动）
- **核心抽象**：`@step` 异步方法 emit/consume **typed Event**；共享 `Context`（可序列化、可暂停恢复）；并行 fan-out/fan-in、retry、human-in-the-loop event。
- **特点**：拓扑是**隐式的**（从事件接线推导，非显式图）；轻量、少样板；但可观测性弱（看不到整图，要 trace 事件）。已从 LlamaIndex 主仓独立为 `llama-index-workflows` 包。
- **与本项目契合度**：与 `ctx.tasks` 的"已注册 task 显式编排"范式**差异最大**（事件 vs 任务注册表），接入要改写调度范式，且自带 RAG/检索生态对本项目无直接价值（本项目非 RAG 引擎）。

### 2.5 Temporal（持久化执行标杆）
- **核心抽象**：**Durable Execution**——每个步骤写进 event history，worker 崩溃后由其他 worker 重放历史重建状态继续。**工作流可跑数天/数年**。
- **特点**：内置 retry/timer/signal/补偿（saga）；OpenAI 用它在生产跑 Codex（等数天人工审批不丢进度）。但**要求确定性工作流代码 + 独立 Temporal Server + worker**，运维重；非确定性 LLM 调用需包成 activity 适配。
- **与本项目契合度**：只解决"不能死的长流程"（跨天审批、分布式事务补偿）。它**不替代** agent 推理层，典型是"Temporal 外层持久化 + 内部 LangGraph/agent 做推理"的双层架构。当前本项目是单体 FastAPI，没有 Temporal Server，引入代价高。

### 2.6 n8n（可视化自动化）
- **核心抽象**：可视化画布 + 节点 + JSON 流程；数百 SaaS 集成；Wait 节点可暂停（webhook/form 恢复）；AI 节点库渐丰。
- **定位**：**业务 SaaS 胶水/运维自动化**，非 agent 内核编排。要接需把本仓 agent 暴露为 HTTP 节点，本质是在外部系统里拖拽调用本仓 API。
- **与本项目契合度**：若目标只是"可视化编排 + 连 SaaS"，n8n 上手最快；但若要作为 agent 内核编排层、保留 `ctx.tasks` 的护栏/审计/工具域裁剪，n8n 是"外挂"而非"内核"，不解决三支柱闭环。

---

## 3. 横向对比矩阵

| 维度 | LangGraph | AutoGen | Microsoft Agent Framework | LlamaIndex Workflows | Temporal | n8n | **本仓现有（ctx.tasks）** |
|---|---|---|---|---|---|---|---|
| 抽象 | State+Node+Edge 图 | 事件/Actor+Team | Agent+Workflow(图)+Orchestrator | Event+Step+Context | Durable Execution | 可视化节点 | **统一 Task 注册表** |
| 确定性 | 是（图可循环） | 是（team 模式） | 是（Workflow）+ 智能体 | 是（事件驱动） | 是（确定性代码） | 是（布尔路由） | **是（task 调用）** |
| 持久化/断点续跑 | ✅ Checkpoint | ⚠ 需自接 | ✅ Checkpoint(每 superstep) | ⚠ Context 可序列化，需自接 | ✅ **最强（event history 重放）** | ✅ 执行数据入库 | ⚠ audit events 表（回溯非续跑） |
| 人机协同 | ✅ interrupt | ✅ 终止条件 | ✅ 暂停+checkpoint | ✅ human event | ✅ signal/wait | ✅ Wait 节点 | ❌ 当前无 |
| 可视化编辑 | ✅ Studio | ✅ AutoGen Studio | ⚠ DevUI 预览 | ❌ | ❌ 需 Web UI | ✅ 画布 | ❌ 当前无 |
| 分布式 | ❌ | ✅ gRPC | ⚠ | ❌ | ✅ **原生** | ⚠ 自托管集群 | ❌ 单体 |
| 声明式 YAML | ❌ 代码 | ❌ 代码 | ✅ Workflow/Agent YAML | ❌ 代码 | ❌ 代码(Go/Py) | ✅ 画布 JSON | ✅ **拟用 `data/workflows/*.yaml`** |
| 引入依赖重量 | 重（langchain 生态） | 中 | 重（微软栈） | 中（独立包） | **重（独立 Server）** | 中（外部服务） | **零新增（已存在）** |
| 2026 状态 | 稳定 1.x | **维护模式** | 1.0 GA | 独立包 | 生产标准 | 活跃 | — |
| 与本项目范式距离 | 中（包成 node） | 远（应转 MAF） | 远（重写内核） | 远（事件范式） | 远（独立服务） | 远（外部） | **基准（0 距离）** |

---

## 4. 与当前 agent 现状的对照（关键）

**本仓已有的（改造起点，非空）：**
- `ctx.tasks`（`sys/tasks.py`）：统一 `Task` 抽象 + `invoke`（schema 校验 → handler → guard 裁决 → audit 发射）+ `resolve/catalog/owned_by` 目录。**所有能力已注册成 task**，并自动继承 guard 审计/预算记账。
- react loop 已有可换的四组件接缝（Reasoner/Actor/Observer/Finalizer），Actor 即工具执行器（`components.py`）。
- 文件型配置范式（`data/skills/`、 `agent_plugin_config.json`、`config_store.py`）。

**框架接入的本质代价 = "再包一层适配器"：**
| 框架 | 接入要把现有 task 适配成什么 | 新增改造成本 |
|---|---|---|
| LangGraph | `ctx.tasks.invoke(...)` 包成 `Node`，工作流变量换成 `State`+reducer | 写 node 适配层 + 引入 langgraph + 接管/并列 react loop |
| MAF | task 包成 `executor` / `Agent`；YAML 对齐其 schema | 引入微软栈 + 重写编排层（kernel 被替换） |
| LlamaIndex | task 包成 `@step`，工作流变量换成 `Event` 收发 | 改写调度范式（事件 vs 注册表）+ 引入包 |
| Temporal | task 包成 `Activity`，工作流包成确定性 `Workflow` + 起 Server | 引入 Temporal Server/worker + 确定性约束改造 |
| n8n | 把 agent 暴露成 HTTP 节点 | 在外部系统操作，不改内核，但失内核级护栏 |

**框架能给而 `ctx.tasks` 当前没有的（决策标准）：**
1. **持久化 checkpoint / 断点续跑**（LangGraph/MAF/Temporal）——当前仅有 audit events 表做回溯，不做续跑；
2. **可视化 DAG 编辑**（n8n/LangGraph Studio/MAF DevUI）——当前无；
3. **跨天 human-in-the-loop 暂停**（langgraph interrupt / Temporal signal）——当前无；
4. **分布式 worker**（Temporal/AutoGen）——当前单体。

**结论**：① 这些能力中多数本仓暂无，但**都不是"必须立刻上框架"**——可视化/持久化可用轻量自研扩展满足；② 只有当"跨天不能死的长事务/分布式"成为硬需求时，外部框架（尤其 Temporal）才真正划算；③ 直接采用现成框架 = 用**重依赖 + 范式迁移 + 可能重写内核** 换上述能力，对当前自研单体、无分布式需求的项目**性价比低**。

---

## 5. 接入改造路径（核心交付）

### 路径 A：自研轻量引擎（既有方案，推荐现阶段的起点）
- 复用 `ctx.tasks`，YAML 定义 DAG（`data/workflows/*.yaml`），引擎落 `agent/sys/workflow.py`，节点引用已注册 task、走 `ctx.tasks.invoke` 自动继承护栏/审计。
- 工作流自身注册成 `workflow.<name>` task ⇒ 与 MCP/Skill/tool 同构、可 LLM 调用、可组合。
- **改造量最小（零新依赖）、与现有 react 接缝/分层铁律天然契合**。
- 缺口：无持久化续跑、无可视化编辑、无分布式——但均可在后续按需叠加。
- 详见《工作流编排方案-2026-10-15.md》。

### 路径 B：引入 LangGraph 作为编排运行时（中远期可选）
- **改造**：① `pip install langgraph`；② 写适配层 `LangGraphAdapter`：把每个工作流节点映射为 LangGraph `Node`（`lambda state: ctx.tasks.invoke(...)`），把工作流输入/中间结果放进 `State`（TypedDict + reducer）；③ 用 `StateGraph.add_node/add_edge/add_conditional_edges/compile(checkpointer=PostgresSaver)`；④ 决定 LangGraph 是**替换** react loop 还是**并列**（工作流走 LangGraph、自由对话仍走 react）。
- **收益**：免费得 checkpoint 持久化 + 可视化 + time-travel + human-in-the-loop interrupt。
- **代价**：引入 langchain 生态重依赖；与现有 react loop 职责需理清（避免两套编排并存混乱）；需把现有 audit 体系与 LangSmith/checkpointer 协调。
- **何时值得**：当需要"可暂停数天等人工审批 + 崩溃精确续跑 + 可视化调试"且不想自建时。

### 路径 C：引入 Microsoft Agent Framework（仅当走微软生态）
- **改造**：把 `ctx.tasks` 能力适配成 MAF 的 `executor`/`Agent`，工作流 YAML 对齐其 schema，Runtime 接管执行；深度集成 Foundry/Entra ID/M365 时价值最大。
- **代价**：引入微软栈、重写编排内核（kernel 范式迁移），对当前无 Azure 绑定的自研 FastAPI 项目**收益低、风险高**。
- **何时值得**：项目确定要落在 Azure/Foundry/微软生态时。

### 路径 D：Temporal 做持久化外层（仅当"不能死的长流程"）
- **改造**：起 Temporal Server + worker；把长业务流程写成 Temporal `Workflow`，把 `ctx.tasks.invoke` 包成 `Activity`（带 retry/timer/补偿）；LLM/外部调用全部 activity 化以满足确定性。
- **收益**：生产级耐用执行（OpenAI Codex 同款），跨天不丢进度、失败可补偿。
- **代价**：独立服务 + 运维 + 确定性代码约束；**不替代** agent 推理层（通常 Temporal 外层 + 内层 LangGraph/agent）。
- **何时值得**：出现"金融/审批/多日长事务、失败有真实业务成本"的硬需求时。

### 路径 E：n8n 做可视化胶水（仅当"连 SaaS 自动化"）
- **改造**：把本仓 agent 暴露为 n8n HTTP 节点/Webhook；在 n8n 画布里拖拽编排，调本仓 API。
- **代价**：agent 内核不变，但编排逻辑移到外部，失去内核级 `tool_domains`/guard 统一管理。
- **何时值得**：团队是非开发、要快速连 Slack/CRM/邮件等 SaaS 时。

---

## 6. 决策建议

1. **现阶段（自研单体、无分布式、已有 agent 内核）：选路径 A（自研轻量引擎）**——零新依赖、复用 `ctx.tasks` 护栏/审计、与既有 react 接缝/分层铁律天然契合，最快满足"三支柱闭环"诉求。
2. **若随后出现可视化/持久化/人机协同硬需求**：优先在路径 A 上**轻量叠加**（YAML 编辑 UI + 运行态持久化到现有 audit 表 + 暂停/恢复），而非急着重写。
3. **仅当出现"跨天不能死/分布式"长事务**：引入 **Temporal（路径 D）做外层持久化**，内部仍用本仓 agent 推理（双层架构），不要为持久化而全盘迁 LangGraph/MAF。
4. **避免**：① 新项目选已进入维护模式的 AutoGen（应转 MAF）；② 无 Azure 诉求却引入 MAF 重写内核；③ 为"画 DAG"而引入 n8n 导致编排逻辑外溢出内核护栏。

**一句话**：现成框架不是"更优解"，而是"用重依赖换持久化/可视化/分布式"。本仓已有 `ctx.tasks` 基座，先走自研轻量引擎（路径 A），把"框架级能力缺口"作为后续按需叠加项，是性价比最高、风险最低的路线。

---

## 7. 与既有工作流方案的关系

- 本调研是《工作流编排方案-2026-10-15.md》的**前置选型论证**：后者默认路径 A，本调研解释了"为什么不直接用现成框架"。
- 关键衔接点：**路径 A 的 `ctx.tasks` 适配层应抽象干净**，使未来若要切到路径 B（LangGraph）时，只需把"节点执行器"从 `sys/workflow.py` 换成 LangGraph `Node` 适配，工作流定义（YAML）可不改——即把"执行后端"做成可插拔，避免路线锁定。

---

## 8. 结论
- 主流框架（LangGraph/MAF/LlamaIndex/Temporal/n8n）均要求把现有能力"再包一层"为各自执行单元；**本仓 `ctx.tasks` 已是统一基座**，引入框架 = 重依赖 + 范式迁移。
- 框架真正独特的能力是**持久化续跑 / 可视化编辑 / 跨天人机协同 / 分布式**，这些当前 `ctx.tasks` 暂无但可轻量叠加，且仅"不能死的长流程"才需要 Temporal 级。
- **推荐**：现走自研轻量引擎（路径 A），把执行后端做成可插拔；硬需求出现时再在 A 外层叠加 Temporal（路径 D）或 LangGraph（路径 B）。AutoGen 不应新选（维护模式，转 MAF）；无 Azure 诉求不引 MAF；n8n 仅适合 SaaS 胶水。

---

## 9. 待确认（接回工作流方案）
本调研不改《工作流编排方案》的待确认项（§7 的 6 项），并新增一条框架路线决策：
- **Q0（框架路线）**：确认走路径 A（自研，零依赖、最快闭环）起步，还是直接引入 LangGraph/Temporal（需接受重依赖与范式迁移）？**建议路径 A。**
