# 通用 Agent · 能力完善路线

| 文档属性 | 内容 |
|---|---|
| 文档状态 | **v1.1（A1 与 A3 已实施，其余未动）** —— 代码见 `agent/plugins/react/`；本卷保留为通用能力的**待办总账** |
| 范围 | **通用 agent 本体**；**不含记忆**（见《记忆插件-能力缺口分析.md》）与**引擎接入**（已归档 `doc/`） |
| 依据 | 对当前代码的逐项核实（证据见 §8），基线由 **68 项**更新为 **82 项离线测试全绿** |
| 关联文档 | 《agent内部架构-开发文档.md》（架构）·《ReAct插件-设计方案.md》（A1/A3 的落地设计）·《审批插件-设计方案.md》（A4/C9 的落地设计）·《记忆插件-能力缺口分析.md》· `doc/README.md`（历史归档） |
| 编制日期 | 2026-09-24（v1.1 同日：ReAct 多步循环落地） |

---

## 0. 结论先行

1. **核心判断（v1.1 更新）：多步循环已补齐。** 原判断「它不是 agent，是单步工具调用器」的**定义性缺口已修复**——`plugins.react` 提供了 `think → act → observe → replan` 回环，`chat` 在装配该插件时委托给它、缺省回退单步（见 §9）。
2. **仍成立的部分**：① 循环虽已补，但**流式 / 取消 / 编排 / 审批**未做；② 三条主线顺序不变：**循环（已做）→ 体验（看得见、停得下）→ 安全与生态**。
3. **现状不是"架构不行"，而是"几处能力没接"**：内核解耦干净、插件化彻底、审计链完整、离线确定性可测、代码内零 TODO/FIXME。缺的是**运行控制**与**工程化收口**。

> 一句话：**先补"循环"，再补"看得见的体验"，最后才是"安全与生态"。**

---

## 1. 现状核实（有 / 没有）

| 能力 | 现状 | 证据 |
|---|---|---|
| 单步工具调用 | ✅ 有（`ping` / `tool_call` / `chat` 三种意图） | `agent/sys/chat.py` `_execute` |
| **多步循环（观察→再规划）** | ✅ **已有**（`plugins.react`；chat 软委托，停用即回退单步） | `agent/plugins/react/loop.py` · `agent/sys/chat.py` `_send_via_react` |
| 工具编排（并行/串行） | ⚠️ 部分：同轮可多工具但**顺序执行**；`EventBus.parallel/serial` 仍无消费方 | `agent/plugins/react/components.py` · `DefaultActor` |
| 上下文治理（结果截断/压缩） | ⚠️ 部分：**结果截断 + 历史裁剪已做**；摘要压缩 / token 感知裁剪未做 | `agent/plugins/react/prompt.py` `truncate` · `loop.py` `_trim` |
| 人在环中确认（ask） | ❌ 无（只有 allow/deny）；`permission_mode`/`Tool.risk` 已预留 | `agent/sys/guard.py` `_decide` · `agent/sys/tasks.py` `RISKS` |
| 流式输出 | ❌ 无 | `/api/chat` 一次性 `JSONResponse` |
| 取消 / 中断 | ❌ 无（仅进程级 Ctrl+C） | `AgentDriver` 协议只有 `run` |
| 定时 / 周期任务 | ❌ 无 | `agent/sys/` 无 scheduler 模块 |
| 会话持久化 / 多会话 | ❌ 无 | `session_id` 由入口进程 `uuid4()` 生成 |
| 事务 | ❌ 无 | `agent/sys/storage.py` 仅 `execute` / `query` |
| LLM 重试 / 退避 | ❌ 无 | `DeepSeekLlm.complete`：超时写死 90s，无重试 |
| 依赖声明 | ❌ 无 | `pyproject.toml` 无 `[project.dependencies]`、无 `[build-system]` |
| 插件生命周期 `start()` | ❌ 无人实现 | 仅 scaffold 模板里有；无任何插件落地 |
| 插件 `status()` 自述 | ⚠️ `windows_mcp` / `plugins.react` 有 | `container.plugin_statuses()` 支持，多数插件未实现 |
| lint / 类型检查 / CI | ❌ 无 | `pyproject.toml` 只有 pytest 配置 |

---

## 2. A · 智能体核心（重点，其余均为配套）

### A1 多步工具循环 ✅ **已实施（2026-09-24）**

- **原现状**：单步。`think → act` 之后直接 `narrate`，没有 `observe → replan`。
- **已落地**：`plugins.react`（目录插件，默认启用）提供 `think → act → observe → replan`，直至**收敛**（模型不再返回工具调用）或**触顶**。三把闸齐备：
  - `max_steps`（默认 8）· `deadline_s`（默认 120）· **token 预算**（复用 `budget` 接缝，天然生效）。
- **配套（已做）**：工具**失败即观察**——参数错 / 工具不存在 / 被 guard 拒，全部转成 error 观察回灌，让模型"改参数重试 / 换路径"；被拒另带 `denied` 标记，同一工具连拒超 `max_rejects` 才终止（防死循环）。
- **关键决策**：循环**不新建特权通道**——模型一律 `ctx.llm`、工具一律 `ctx.tasks.invoke` ⇒ guard / budget / schema / 审计自动继承（见《ReAct插件-设计方案.md》§2 红线）。
- **仍未做**：触顶时的**摘要压缩**（现为"再答一次 + 证据兜底"）；并行工具编排（→ A2）。

### A2 工具编排（并行 / 串行）

- **现状**：同一轮**可发起多个**工具调用，但 `DefaultActor` **顺序执行**；`EventBus.parallel` / `serial` 仍无消费方。
- **目标**：并行 / 串行编排与结果汇聚；需与 `guard` / `budget` 的**逐次裁决**对齐（每个工具调用各自过闸）。
- **要点**：`EventBus` 的 `parallel` / `serial` **已实现且闲置**——正是为编排准备的现成原语；`plugins.react` 的四组件接缝里，只需替换 `Actor` 即可（不必改循环）。

### A3 上下文治理（A1 的前置条件）⚠️ **部分已实施**

- **原现状**：工具结果**完全不截断** ⇒ 一个大结果就撑爆上下文。
- **已落地**：`plugins.react` 内——工具结果按 `tool_result_max_chars`（默认 2000）**截断并标注**；喂给模型的窗口按 `history_keep`（默认 20）**裁剪**（保 system + 首个用户请求 + 最近 N 条）。
- **仍未做**：旧工具结果的**摘要压缩**（现为直接裁掉）；**token 感知**的动态裁剪（当前按条数，不按 token）；工具目录 / 记忆块计入预算的精细核算。
- **注意**：单步 `chat` 路径的工具结果**仍不截断**（`_execute` → `_narrate` 直接 `json.dumps`）——若停用 `react`，这条老路径的问题依旧存在。

### A4 人在环中（ask 模式）

- **现状**：`guard` 只有 allow / deny；**接口已预留**——`ctx.state["permission_mode"]`（`default/plan/auto/bypass`）、`visible_tools(ctx)` 裁剪点、`Tool.risk`（`read/write/danger`）均已就位，但**行为未变**（`visible_tools` 恒返回全量）。
- **目标**：增加 **ask**（干跑 → 等确认），用于高风险/不可逆动作；权限模式三档（`plan`/`auto`/`bypass`）。
- **要点**：完整设计见《审批插件-设计方案.md》：**职责三分（mode / policy / provider）**，`guard` 一行不改，唯一重活是"中途回话通道"（与 B5/B6 同批）。

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
| D15 | **测试补强** | 82 项离线测试（无网络/无外部进程） | provider 契约测试、MCP 集成测试、**回归集 / eval**（改提示词能看出变好还是变坏） |

---

## 6. 建议路线

| 步骤 | 内容 | 目标 / 验收 | 状态 |
|---|---|---|---|
| **第一步 · 让它真"通用"** | A1 多步循环 + A3 上下文治理 + A1 的三把闸（步数/时延/token） | 能完成"多步工具链"任务（如：先查再改再验），且步数/预算触顶时**优雅停止并如实说明** | ✅ **已达成**（`plugins.react`；13 项新用例覆盖多步链、失败自纠、双触顶、结果截断、chat 委托与回退） |
| **第二步 · 可交付** | B5 流式 + B6 取消 + D11 依赖声明 + D12 CI | 干净环境可装可跑；长任务可见过程、可中断；每次提交自动跑测试 | ⏳ 未开始（B5/B6 与 A4 共用"中途回话通道"） |
| **第三步 · 规模化** | B7 调度器、B8 会话管理、A2 工具编排、A4 人工确认、C9 安全分级、C10 重试 | 具备常规 agent 运行时形态（周期任务、会话、编排、风控） | ⏳ 未开始（A4/C9 已有接口预留与设计） |
| 收尾 | D13 可观测、D14 SDK/文档、D15 测试补强 | 可持续演进 | ⏳ 部分：`plugins.react` 已实现 `status()` 钩子（D13 一小步） |

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
| ~~chat 为单步执行~~ → 多步由 `plugins.react` 承担 | `agent/plugins/react/loop.py`（`while steps < max_steps`）；`agent/sys/chat.py` `_send_via_react`（软委托） |
| 单步路径仍在（回退用） | `agent/sys/chat.py` · `_execute`（仅 `ping` / `tool_call` 分支） |
| 工具结果截断（多步路径） | `agent/plugins/react/prompt.py` · `truncate`；`loop.py` · `_trim`（历史裁剪） |
| 单步路径仍不截断（待办） | `agent/sys/chat.py` · `_execute` 返回 `result` → `_narrate` 内 `json.dumps` |
| EventBus 五模式仅用两种 | `agent/sys/llm.py`（`waterfall` / `emit`）· `agent/sys/tasks.py`（`waterfall` / `emit`）· `agent/sys/chat.py`（`emit`）· `agent/plugins/react/loop.py`（`emit`） |
| guard 只有 allow / deny | `agent/sys/guard.py` · `_decide` |
| 权限模式接口已预留 | `agent/sys/tasks.py` · `RISKS` / `register(risk=…)`；`agent/plugins/react/loop.py` · `visible_tools`（恒全量） |
| AgentDriver 协议只有 `run` | `agent/sys/loop.py` · `class AgentDriver(Protocol)` |
| 无 scheduler | `agent/sys/` 目录无 scheduler 模块 |
| 无事务 | `agent/sys/storage.py` · 仅 `execute` / `query` / `close` |
| 无取消 | `agent/run/chat.py` · 仅进程级 `KeyboardInterrupt` |
| session_id 进程内生成 | `agent/run/chat.py` · `session_id = uuid.uuid4().hex[:8]` |
| 依赖未声明 | `pyproject.toml`（无 `dependencies` / `build-system`）vs `agent/sys/llm.py`（httpx）· `agent/run/chat.py`（fastapi / uvicorn） |
| LLM 无重试 / 超时写死 | `agent/sys/llm.py` · `DeepSeekLlm.complete`（`timeout=90.0`，无 retry） |
| `ctx.state` 不持久 | `agent/sys/budget.py` · `ctx.state["budget_spent"]` |
| `start()` 钩子无人实现 | 全仓仅 `agent/sys/plugin_manager.py` 的 scaffold 模板中出现 |
| `status()` 已实现者 | `agent/plugins/windows_mcp` · `status()`；`agent/plugins/react/__init__.py` · `status()` |

---

## 9. 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-24 | **A1 落地**：新增目录插件 `plugins.react`（`agent/plugins/react/`，四文件：`prompt/components/loop/__init__`），默认启用；`chat` 经 `ctx.has("react")` 软委托、停用即回退单步。**A3 部分落地**（结果截断 + 历史裁剪）。**A4/C9 接口预留**（`permission_mode` / `visible_tools` / `Tool.risk`，行为未变）。`LlmProvider` 加法式支持 `tools`/`tool_calls`；插件加载器支持"插件包"。测试 68 → **82 项全绿**。设计与实现详见《ReAct插件-设计方案.md》。 |
| — | 未做项（保持范围）：A2 并行编排、A4 审批本体、B5 流式、B6 取消、B7 调度器、B8 会话管理、C9 分级判定、C10 重试、D11–D12 工程化。 |
