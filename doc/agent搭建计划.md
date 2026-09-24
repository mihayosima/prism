# 选品 Agent 搭建计划（分步骤 · MVP 先行）

| 文档属性 | 内容 |
|---|---|
| 文档状态 | **v1.0（MVP 审查闭环，可按此动工）** |
| 归属 | 选品 Agent 工程（`选品agent/`）—— **如何把《agent内部架构-开发文档.md》按步骤建起来** |
| 关联文档 | 《agent内部架构-开发文档.md》（=设计图）·《接入契约v2-双向适配.md》（=引擎边界）·《选品策略技能库-设计.md》（=内容层）· DeepSeek-Harness/Cordis（=哲学来源） |
| 编制日期 | 2026-09-22 |

---

## 0. 结论先行

1. **本计划回答的是「怎么一步步盖」，架构文档回答「盖成什么样」**。最大改动：把**分级权限**从"待定"升格为**架构设计 + MVP 机制**（粒度留到 MVP 后细化），并把**插件思想**从"设计语"变成每步的**落地自检**。
2. **插件思想真实落地 = 七个「是插件」**：每个能力是 `@plugin`（容器拓扑装配）· loop 是插件（可换）· 接缝是插件（capability 事件挂守卫）· **权限网关也是插件**（不是散装 if）· 技能库是**数据层插件**（YAML=内容可插）· 装配走 profile→patch（配置驱动）· 注册皆可逆（teardown 对称）。§1 给自检清单，每步对照。
3. **分级权限 = 分「执行怎么落地」≠ 分「能不能请求」**：repertoire 无上限、初始矩阵默认全开（open），「不要一开始就限制」。外圈两道门（凭据在场、引擎重启生效）是**世界侧纪律**，不是对 agent 特设的限制。§2。
4. **MVP = 一条 t0 策略离线闭环 + 权限网关机制 + 可自举**，全程不依赖引擎进程；**分级的具体矩阵/审批细节**列进"MVP 后细化"清单。§3 阶段 0–5 为 MVP，阶段 6 起为后 MVP。
5. **T4（agent 写插件装进引擎）** 作为「装配扩展面」写进架构与路线图，MVP 只画接口与占位，真正实现评估于引擎侧能力成熟后。§2.3/§3-阶段 6。

---

## 1. 搭建总原则：把插件思想真正落实

### 1.1 七个「是插件」（每步的落地自检）

| # | 原则 | 落地形态 | 自检（每步验收用） |
|---|---|---|---|
| P1 | 能力是插件 | 每个子系统 = 一组 `@plugin`（name/inject/provides/layer/tables），容器拓扑装配，D-3 键唯一 | 新增能力 = 挂一个插件，不是往里写分支 |
| P2 | loop 是插件 | `AgentDriver` 协议；消费方只依赖接口，不依赖实现 | 换驱动 = 换一个插件，消费方零改动 |
| P3 | 接缝是插件 | llm/tasks/storage 各自是 `@plugin` 提供的服务键 | 换 Provider = 换按键注册的插件，业务不动 |
| P4 | 权限/守卫也是插件 | `guard` 以 capability 事件挂 seam（waterfall/bail）裁决 | 散装 if 违规；必须经网关统一裁决 |
| P5 | 内容=数据层插件 | 20 策略 = `skills/*.yaml`（anchor/rubric/prompt…），`_index.yaml` 注册 | 加一条策略 = 加文件+注册一行，机制零改动 |
| P6 | 装配配置驱动 | 装配清单 = profile/bundle 有序叠层 + patch 覆盖（Cordis 同构） | 组合形态 = 配置选择，不是改装配代码 |
| P7 | 注册皆可逆 | 一切注册经 `ctx.effect` / `ctx.on`，teardown 逆序展开 | 卸载插件后无残留效果/服务/调度 |

### 1.2 三条贯穿纪律

- **离线优先**：所有测试走 fixture，不触网、不触引擎、不触 Node——独立性的硬证明。
- **诚实就绪度**：新能力初始 `t0`（未校准），probe/soak 出证据才升级；无 key/缺数据/缺能力 → `CapabilityNotImplemented`，不虚标。
- **不做伪热插拔**：装配启动一次性、重启生效（引擎全局纪律）——它同时是 T4 的天然复查窗口。

---

## 2. 分级权限架构设计（MVP 落地，粒度后细）

### 2.1 分级模型（变现成步骤里怎么"落地去执行"）

| 级 | 名 | Agent 可尝试（repertoire 无上限） | 执行的承载方式（分级的真正对象） |
|---|---|---|---|
| **T0** | 学 | 读引擎核心源码/docs/契约/插件模板 · probe 站点结构（只读） | 永远全开 |
| **T1** | 产 | 内部全部：评分/排榜/报告/技能库维护/自举执行 | 永远全开（自家能力） |
| **T2** | 驱 | 触发引擎只读插件/任务（采集·观测·分析）+ `persist` 登记持久任务（owner=agent） | **白名单矩阵**（配置数据，可增可扩） |
| **T3** | 写 | 触发引擎**写类**插件（自动上架 / 测品 / 外部平台） | **凭据在场 + 干跑 → 人工确认 → 落地**，分步放行、配额、审计 |
| **T4** | 授 | **往引擎装配扩展面写程序**：生成插件 → 离线夹具自测 → dry-run → enable → 观察 | **生成即装；生效走引擎重启窗口**（天然复查）+ 模板/静态校验收窄代码面 |

### 2.2 五条设计原则

1. **分级分"执行怎么落地"，不分"能不能请求"**——agent 可以对 T3/T4 发起请求，决议权分步走，**请求权恒在**。
2. **初始默认全开**——matrix 对 agent 默认 **open**；收/扩都是配置数据，不是改架构、不是写死门。
3. **一 agent 一矩阵**——权限矩阵按 agent 独立配置（`config["agent"]["permissions"]`），换 agent = 换矩阵；矩阵本身可审计。
4. **外圈门在世界侧**——T3 写外部平台必须有**平台卖家凭据**（在引擎钥匙库，不在全局）；T4 生效走引擎**重启窗口**。这两道门对所有人（包括人工）一视同仁，不是把 agent 压到平台纪律之下。
5. **诚实就绪度 + 证据升级**——新生成/新接入能力初始 t0，probe/soak 出证据升 t1/t2，不虚标"能跑"。

### 2.3 机制落点（guard = 对等执行契约 / 权限网关）

- `guard` 是**一个 `@plugin`**，不散装：在 capability 事件上裁决——
  `agent/llm/pre-call`（预算/权限）、`agent/tasks/pre-execute|post-execute`（任务白名单/约束）；另两个能力事件 `agent/action/precommit`（T3 干跑→确认）、`agent/assembly/precommit`（T4 授装）**入契约登记，MVP 不挂监听**（能力到位时再挂）。
- 裁决用 `waterfall/bail`：监听收 `(…, next)` → 放行传 `next()`；拒绝即短路。**同一 seam 多裁决插件的执行序 = 装配注册序，短路即停**；MVP 默认 guard 先、budget 后（权限先、成本后）。
- **审计不另建表**：裁决作为 durable 事件 `agent/permission/decision` 落 `agent_events`（who/level/target/decision/at），权限审计 = 事件查询，保持单条审计脊柱。
- 矩阵为配置数据：`{default: "open", tiers: {T2:{allow: [...]}, T3:{require_confirm: true}, T4:{template_ok: true, require_restart: true}}}`。
- **MVP 范围**：架构设计 + 网关机制（矩阵可配、默认 open、审计落事件）；**矩阵的具体内容 / 审批流程 / 引擎侧 T3 插件** 列为"MVP 后细化"（§6）。

### 2.4 与契约联动

- 接入契约 v2 §7 的"只读开放/写默认禁"表述**升级**为本模型（默认 open、分执行模式）；契约 ACL 表待 v2 版次更新（§5）。
- T2/T3/T4 与契约任务面（`run(ctx,**params)`）/ 装配路径不冲突：分级是"执行侧裁决"，契约是"消息形状"。

---

## 3. 分步骤搭建（阶段 0–5 = MVP；阶段 6+ = 后 MVP）

> 每阶段含：目标 · 交付物 · 验收（离线确定性全绿）· 回滚。依赖自上而下；插件思想用 §1.1 自检。

### 阶段 0 — 底座与参考研读（准备）
- **目标**：把「参照 deepseek-harness 建」变成落地底座。
- **动作**：
  - 研读本地 clone（`/tmp/deepseek-harness`）的 `docs/architecture.md · cordis-primer.md · subsystems/core.md · capability-seams.md`——沉淀「六根脊梁」（架构文档 §1）已做。
  - 建立 `选品agent/` 骨架：`pyproject.toml / pytest / helpers`；约定 module 名、`import` 边界（agent 本体只 `from agent.api import`）。
- **交付**：目录骨架 + 测试约定 + README 占位。
- **验收**：`pytest` 可空跑；纯度守卫目录就位。

### 阶段 1 — 内核 `agent.api`（架构 §3）
- **目标**：agent 自己的 slim Cordis 内核。
- **交付**：`agent/core/`（Ctx 服务仓 / @plugin / Container 拓扑 / EventBus 5 模式 / effects / errors）+ `agent/api.py` + `tests/test_kernel*` + `test_agent_purity`（核心零业务词）。
- **打法（spike）**：先写**最薄可跑的 Container + @plugin + EventBus + errors**，用 tests 钉住最小闭环即往下走，不求全；镀金项（并行 dispatch 调度、作用域 GC 边界）显式压到阶段 6。
- **验收**：内核单测全绿；`@plugin` 装配/对称 teardown/环报错/缺失清单正确；**P1/P6/P7 自检**。

### 阶段 2 — 通用机制子系统（架构 §4）
- **目标**：事件/任务/存储/AI 四条脊柱就位，全部「是插件」。
- **交付**：`agent/events`（三域 + @mode，先 emit/waterfall/bail）· `agent/tasks`（schema 校验 / resolve(`run(ctx,**params)`) / owner；内置 `agent.ping`）· `agent/storage`（独立 `agent.db` + `agent_*` 迁移，幂等）· `agent/llm`（`LlmProvider` + `FixtureLlm` + 占位不虚标 + `interface_version`；MVP 只 `complete`，`analyze_image` 有消费方再加）· `agent/budget`（capability 事件包夹 llm，记账）。
- **验收**：各子系统离线单测全绿；补「换 Provider（fixture→占位）业务零改动」冒烟测试；**P3/P5/P7 自检**。

### 阶段 3 — 权限网关 `guard`（架构 §2.3 / 本计划 §2）
- **目标**：分级权限的**机制**落地（矩阵 + capability 裁决 + 审计），地用**默认全开**。
- **交付**：`agent/guard`（`@plugin`）：按 agent 读矩阵 → `agent/llm/pre-call`、`agent/tasks/pre-execute|post-execute` 裁决 → 拒绝即短路并落 durable 事件 `agent/permission/decision` 审计；**MVP 不挂 `agent/action/precommit` / `agent/assembly/precommit` 监听**（能力到位再挂）。**双装配 profile**：`offline`（默认全开）+ `tight`（仅测试，矩阵收窄）——一举验 P6 叠层/patch，并给 guard 造真实拒绝场景。
- **验收**：默认 `offline` 全开放行（不阻碍 T0/T1/T2 默认路径）；`tight` 收窄时正确拒绝且**走 guard 的 capability 事件路径（P4 正面证明）**、审计落 `agent/permission/decision`；**无散装 if**；粒度细化留后。
- **备注**：`sites/plugin_author`（T4）**不建占位包**，留到阶段 6 一次设计实现。

### 阶段 4 — 技能库内容层 + 评分管线（架构 §7）
- **目标**：一条 t0 策略真正跑通。
- **交付**：`skills/_index.yaml` + 首条策略（如 `substitution`）YAML 做透（anchor/data_feed/signals/rubric/prompt/filters/readiness）；**readiness = 能力清单匹配**：fixture provider 声明能力清单（`bestseller_list / ticks_series / categories / …`，复用《技能库》§7 抽象词表），技能 `data_feed` 也改指抽象能力 → 就绪判定 = 技能声明的需求 ⊆ provider 能力清单；`scoring/pipeline.py`（加载 skill → 能力就绪检查→candidate 组装（fixture provider）→逐候选喂 llm→schema 校验/重试/拒脏→落 `agent_scores`→rubric 合成→`agent_picks`→`agent_runs` 审计）。
- **验收**：单策略端到端绿（fixture 数据）；补「缺能力 → 诚实抛 `CapabilityNotImplemented`」测试（AC-5 直接证据）；**P5/P7 自检**。

### 阶段 5 — 裁决驱动 loop + 可自举（架构 §5/§9）
- **目标**：MVP 闭环 + 独立性硬证明。
- **交付**：`agent/loop`（`AgentDriver` 协议 + `ProduceDriver`：turn/step 守卫点、job 进度、重叠 skip、deadline 上限）；`run/agent.py` 自举入口（fixture providers）+ 双装配 profile（`offline` / `tight`）；全量离线测试集。
- **验收**：`python -m run.agent` 跑通「候选→打分→排榜→落库→收尾」；离线确定性测试**全绿**；补负路径单测（**重叠 skip / deadline 上限**）+ 卸载 loop **无残留 teardown 断言（P7）**；**P2/P6 自检**。
- **＝MVP 达成**：可自举 + 单策略闭环 + 权限网关机制，全程零引擎依赖。

### 阶段 6 — 后 MVP（路线图，按需分期）
- 权限分级**细化**：矩阵具体内容、T3 审批流程、配额/干跑 UI、引擎侧写类插件的领域建模。
- **交互驱动**（第二 driver）：对话式问诊/复盘/调参（复用 `agent_events` 根原语，loop 可换）。
- **T4 装配扩展面**：`sites`（读站→规格化）+ `plugin_author`（规格→配置/代码→fixture→dry-run→enable），接引擎 scaffold/enable/重启窗；宿主按 T4 分级授权。
- **引擎接入**：契约 §6/§11 的引擎侧桥、facet 提供者、T2 `persist` 持久任务、T3 写类插件（凭据+干跑）。
- **memory / 更硬 guard**：记忆系统、更细策略审批。
- 每期独立验收、独立回滚。

---

## 4. MVP 范围线（明确"做到哪算第一版"）

**MVP IN（阶段 0–5）**：内核 · 事件/任务/存储/llm/budget · 权限网关机制（默认 open）· 单条 t0 技能 · 评分闭环保· loop· 自举入口 · 离线全绿。
**MVP OUT（阶段 6）**：分级粒化（矩阵内容/审批）· 交互驱动 · memory · sites/plugin_author（T4）· 引擎桥与 T3 写类 · 报告导出插件美化。

> 出 MVP 后，按观察数据再细分级权限——这正是「先不限制、后按证据收口」的节奏。

---

## 5. 文档联动（每阶段结束后回写）

| 阶段 | 回写 |
|---|---|
| 阶段 3 | 权限分级设计入《agent内部架构-开发文档.md》（替换原仅"占位"表述）；**同步事件契约表**（§8 统一 `agent/tasks/*` 并登记 `action/assembly/precommit` 预留）；契约 v2 §7 ACL 升级为"默认 open + 执行模式" |
| 阶段 4 | 技能库 `_index.yaml` 首批就绪度落表（t0/t1/t2 如实标注） |
| 阶段 5 | 架构文档 A0–A4 里程碑状态置 ✅；本计划阶段 0–5 状态置 ✅ |
| 阶段 6 | 权限细则 / T4 / 引擎桥分别立小节文档 |

---

## 6. 待拍板（隐含自架构文档，随 MVP 验证）

| # | 项 | 推荐 |
|---|---|---|
| OPEN-1 | 驱动形状 | v1 裁决驱动（produce），交互 driver 后置（loop 可换） |
| OPEN-2 | 技能库首版 | 单条 t0 做透，六条随后按「加文件」批量 |
| OPEN-3 | loop 是否 inbox | v1 不用；保留 `AgentDriver` 接口 |
| OPEN-4 | 事件模式 | 先 emit/waterfall/bail，parallel/serial 按需 |
| OPEN-5 | 存储 | sqlite 先行（独立 `agent.db`），dialect 映射留位 |
| OPEN-6 | guard/memory 文件时机 | guard 进 MVP（机制）；memory 后置；`sites/plugin_author` 仅接口占位 |
| OPEN-7 | 权限初始姿态 | T0–T4 默认全开（open），一 agent 一矩阵；粒度后细化 |
| OPEN-8 | T4 评估窗 | MVP 只在架构/接口占位层面保留 T4；引擎侧成熟后再全权实现 |

---

## 7. 参考

- 《agent内部架构-开发文档.md》= 设计图（本计划按阶段挂载）
- 《接入契约v2-双向适配.md》= 引擎边界（§2.4/§5 联动）
- 《选品策略技能库-设计.md》= 内容层（阶段 4 承接）
- DeepSeek-Harness 本地 clone `/tmp/deepseek-harness`：`docs/architecture.md` · `cordis-primer.md` · `subsystems/core.md` · `capability-seams.md`（哲学来源）
- Meridian 引擎 `RPA/`：`engine/core/{container,context,events,commands,scheduler}.py` · `engine/core/plugin_manager.py` · `engine/system/engine_kernel.py`（同源 Cordis 语义的实战版，借鉴规则不 import 代码）