# 通用 Agent · 能力完善路线

| 文档属性 | 内容 |
|---|---|
| 文档状态 | **v1 评估与路线（未实施）** —— 仅盘点与排序，**未改任何代码** |
| 范围 | **通用 agent 本体**；**不含记忆**（见《记忆插件-能力缺口分析.md》）与**引擎接入**（已归档 `doc/`） |
| 依据 | 对当前代码的逐项核实（证据见 §8），基线为 **68 项离线测试全绿** |
| 关联文档 | 《agent内部架构-开发文档.md》（架构）·《记忆插件-能力缺口分析.md》（记忆待办）· `doc/README.md`（历史归档） |
| 编制日期 | 2026-09-24 |

---

## 0. 结论先行

1. **核心判断：它现在不是「agent」，是「单步工具调用器」。** `chat` 驱动走「规划一次 → 执行**一个**工具 → 解说一次」就结束，**没有"观察结果 → 再规划"的回环**。这是通用 agent 的**定义性缺口**，排在所有其他事项之前。
2. **三条主线，顺序不可倒**：**① 循环**（让它成为 agent）→ **② 体验**（看得见、停得下）→ **③ 安全与生态**（敢用、能长大）。顺序反了会浪费——例如先做流式输出，而底层没有多步循环，流式只是把"一次回复"打散成打字机效果。
3. **现状不是"架构不行"，而是"几处能力没接"**：内核解耦干净、插件化彻底、审计链完整、离线确定性可测、代码内零 TODO/FIXME。缺的是**智能体核心能力**与**工程化收口**。

> 一句话：**先补"循环"，再补"看得见的体验"，最后才是"安全与生态"。**

---

## 1. 现状核实（有 / 没有）

| 能力 | 现状 | 证据 |
|---|---|---|
| 单步工具调用 | ✅ 有（`ping` / `tool_call` / `chat` 三种意图） | `agent/sys/chat.py` `_execute` |
| **多步循环（观察→再规划）** | ❌ **无** | `_execute` 仅两个 `if` 分支，无循环/无回环 |
| 工具编排（并行/串行） | ❌ 无消费方 | `EventBus` 五模式中 `parallel` / `serial` / `bail` 全仓未使用 |
| 上下文治理（结果截断/压缩） | ❌ 无 | 工具结果原样进提示词 |
| 人在环中确认（ask） | ❌ 无（只有 allow/deny） | `agent/sys/guard.py` `_decide` |
| 流式输出 | ❌ 无 | `/api/chat` 一次性 `JSONResponse` |
| 取消 / 中断 | ❌ 无（仅进程级 Ctrl+C） | `AgentDriver` 协议只有 `run` |
| 定时 / 周期任务 | ❌ 无 | `agent/sys/` 无 scheduler 模块 |
| 会话持久化 / 多会话 | ❌ 无 | `session_id` 由入口进程 `uuid4()` 生成 |
| 事务 | ❌ 无 | `agent/sys/storage.py` 仅 `execute` / `query` |
| LLM 重试 / 退避 | ❌ 无 | `DeepSeekLlm.complete`：超时写死 90s，无重试 |
| 依赖声明 | ❌ 无 | `pyproject.toml` 无 `[project.dependencies]`、无 `[build-system]` |
| 插件生命周期 `start()` | ❌ 无人实现 | 仅 scaffold 模板里有；无任何插件落地 |
| 插件 `status()` 自述 | ⚠️ 仅 `windows_mcp` 有 | `container.plugin_statuses()` 支持，但多数插件未实现 |
| lint / 类型检查 / CI | ❌ 无 | `pyproject.toml` 只有 pytest 配置 |

---

## 2. A · 智能体核心（重点，其余均为配套）

### A1 多步工具循环

- **现状**：单步。`think → act` 之后直接 `narrate`，没有 `observe → replan`。
- **目标**：`think → act → observe → replan`，直至**完成**或**触顶**。
- **要点**（三把闸缺一不可，否则会失控烧钱）：
  - **步数上限**（如 max_steps=8）；
  - **时延上限**（每轮/整任务 deadline）；
  - **token / 成本预算**（复用现有 `budget` 接缝，天然生效）。
- **配套**：工具**失败要能纠错**——现在工具抛错即整体失败，应支持"改参数重试 / 换路径"。
- **落点**：`agent/sys/chat.py` 的 `send` 流程（把 `_plan → _execute → _narrate` 变为循环，`_narrate` 只在收敛后执行）。

### A2 工具编排（并行 / 串行）

- **现状**：一次只调一个工具。
- **目标**：同一轮内多工具并行或串行，并汇聚结果。
- **要点**：`EventBus` 的 `parallel` / `serial` **已实现且闲置**——正是为编排准备的现成原语；同时需与 `guard` / `budget` 的**逐次裁决**对齐（每个工具调用都要各自过闸）。

### A3 上下文治理（A1 的前置条件）

- **现状**：工具结果**完全不截断**，`result` 原样进 `evidence`，再 `json.dumps` 塞进解说提示词 ⇒ 一个大结果就撑爆上下文。
- **目标**：结果**截断 / 摘要**、历史**压缩**、**token 感知**的裁剪；工具目录与记忆块也要计入预算。
- **要点**：这是 A1 能否稳定的决定因素——多步循环下上下文增长是累积的。

### A4 人在环中（ask 模式）

- **现状**：`guard` 只有 allow / deny。
- **目标**：增加 **ask**（干跑 → 等确认），用于高风险/不可逆动作。
- **要点**：旧设计里 T3「干跑 → 确认」曾预留但未落地；可与 A1 的循环天然结合（在 step 之间挂起等确认）。

---

## 3. B · 运行控制（体验层）

| # | 事项 | 现状 | 目标 |
|---|---|---|---|
| B5 | **流式输出** | `/api/chat` 一次性 JSON，长任务全程转圈 | SSE 逐段吐字（解说与中间步骤） |
| B6 | **取消 / 中断** | 只能 Ctrl+C 杀进程 | cancel 端点 + 恢复 `AgentDriver` 的 `cancel` / `when_idle` 协议 |
| B7 | **定时 / 周期任务** | **无 scheduler** | 新增 scheduler 插件（interval / daily），owner 归属 + 停用自动清任务 |
| B8 | **会话管理** | `session_id` 进程内生成，重启即新会话 | 多会话、持久化、恢复、标题；`/api/session` 扩展 |

---

## 4. C · 安全与可靠

### C9 guard 分级 + 工具输出净化

- **现状**：只有 `default / llm / task_allow / task_deny`，**无读 / 写之分** ⇒ MCP 危险工具默认 open。
- **目标**：
  - **读 / 写分级**（只读工具放行，写类需白名单或 ask）；
  - **路径 / 工具白名单**；
  - **工具输出净化**——工具返回的文本会**原样进模型**，是提示注入的入口。

### C10 重试 / 退避 + 错误分类

- **现状**：LLM 无重试，超时写死 90s；一次抖动即整体降级。
- **目标**：最小重试（1 次 + 退避）、超时可配；工具错误**分类**（可重试 / 参数错 / 权限拒绝 / 未知），供 A1 的纠错分支使用。

---

## 5. D · 工程化与生态

| # | 事项 | 现状 | 目标 |
|---|---|---|---|
| D11 | **依赖声明** | `pyproject.toml` 无 `dependencies`、无 `[build-system]`，而代码已用 `httpx` / `fastapi` / `uvicorn` | 声明依赖与构建后端 ⇒ 干净环境可装可跑（**成本最低，建议最先做**） |
| D12 | **lint / 类型 / CI** | 仅有 pytest 配置 | ruff + 类型检查 + CI 流水线 |
| D13 | **可观测** | 仅事件面；无 trace / 指标 / 成本看板 | 结构化日志、关键指标、成本统计；插件 `status()` 补齐 |
| D14 | **插件 SDK 与文档** | `scaffold` 模板薄；无插件配置 schema；`start()` 无人用 | 插件开发指南、配置 schema、生命周期钩子真正落地 |
| D15 | **测试补强** | 68 项离线测试（无网络/无外部进程） | provider 契约测试、MCP 集成测试、**回归集 / eval**（改提示词能看出变好还是变坏） |

---

## 6. 建议路线

| 步骤 | 内容 | 目标 / 验收 |
|---|---|---|
| **第一步 · 让它真"通用"** | A1 多步循环 + A3 上下文治理 + A1 的三把闸（步数/时延/token） | 能完成"多步工具链"任务（如：先查再改再验），且步数/预算触顶时**优雅停止并如实说明** |
| **第二步 · 可交付** | B5 流式 + B6 取消 + D11 依赖声明 + D12 CI | 干净环境可装可跑；长任务可见过程、可中断；每次提交自动跑测试 |
| **第三步 · 规模化** | B7 调度器、B8 会话管理、A2 工具编排、A4 人工确认、C9 安全分级、C10 重试 | 具备常规 agent 运行时形态（周期任务、会话、编排、风控） |
| 收尾 | D13 可观测、D14 SDK/文档、D15 测试补强 | 可持续演进 |

> 原则：每一步都能独立验收、独立回滚；不引入新依赖前先补 D11。

---

## 7. 范围说明（本次评估**不含**）

| 项 | 去向 |
|---|---|
| **记忆模块**（实体/关系落库、ontology、context 邻边行、kind 覆盖、被动摄取等） | 用户明确暂缓；详见《记忆插件-能力缺口分析.md》 |
| **引擎接入**（双向适配契约、桥插件、facet、传输） | 已明确搁置；契约归档在 `doc/接入契约v2-双向适配.md`（零实现代码） |
| 领域内容（选品策略等） | 已从代码移除；设计留档在 `doc/` |

---

## 8. 附：证据索引

| 结论 | 证据位置 |
|---|---|
| chat 为单步执行 | `agent/sys/chat.py` · `_execute`（仅 `ping` / `tool_call` 分支） |
| 工具结果不截断 | `agent/sys/chat.py` · `_execute` 返回 `result` → `_narrate` 内 `json.dumps` |
| EventBus 五模式仅用两种 | `agent/sys/llm.py`（`waterfall` / `emit`）· `agent/sys/tasks.py`（`waterfall` / `emit`）· `agent/sys/chat.py`（`emit`） |
| guard 只有 allow / deny | `agent/sys/guard.py` · `_decide` |
| AgentDriver 协议只有 `run` | `agent/sys/loop.py` · `class AgentDriver(Protocol)` |
| 无 scheduler | `agent/sys/` 目录无 scheduler 模块 |
| 无事务 | `agent/sys/storage.py` · 仅 `execute` / `query` / `close` |
| 无取消 | `agent/run/chat.py` · 仅进程级 `KeyboardInterrupt` |
| session_id 进程内生成 | `agent/run/chat.py` · `session_id = uuid.uuid4().hex[:8]` |
| 依赖未声明 | `pyproject.toml`（无 `dependencies` / `build-system`）vs `agent/sys/llm.py`（httpx）· `agent/run/chat.py`（fastapi / uvicorn） |
| LLM 无重试 / 超时写死 | `agent/sys/llm.py` · `DeepSeekLlm.complete`（`timeout=90.0`，无 retry） |
| `ctx.state` 不持久 | `agent/sys/budget.py` · `ctx.state["budget_spent"]` |
| `start()` 钩子无人实现 | 全仓仅 `agent/sys/plugin_manager.py` 的 scaffold 模板中出现 |
| `status()` 仅 windows_mcp 实现 | `agent/core/container.py` · `plugin_statuses()`；`agent/plugins/windows_mcp` · `status()` |
