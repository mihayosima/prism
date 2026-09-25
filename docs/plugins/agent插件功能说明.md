# Agent 运行时 · 各插件功能说明

> 阅读对象：本项目（`agent/`）是一套 **slim Cordis 重实现**的通用 Agent 运行时。
> 核心理念是「everything-is-a-plugin，无特权内核」：主循环、模型接缝、任务注册表、事件、
> 存储、预算、权限、会话驱动**全部是插件**；业务能力（记忆、ReAct 多步循环、MCP 工具桥）
> 也以插件挂入。本文档按「实际代码」逐插件说明其功能、关键行为与协作关系。
>
> 插件分两类（**来源决定生命周期，layer 只是语义标签**）：
> - **内核系统插件**（`agent/sys/`，经 `profiles.build_agent` 恒定装配，**不可**由插件页启停）
> - **目录插件**（`agent/plugins/`，来源=目录 ⇒ 可启用/停用/卸载，默认随 `data/agent_plugin_state.json` 启用）

---

## 一、内核系统插件（`agent/sys/`，内置、恒定装配）

### 1. `events_plugin` —— 事件总线（`agent.events`）
- **文件**：`sys/events_plugin.py`
- **功能**：装配 `EventBus` 实例并注册到 `ctx.events`；在作用域 teardown 时自动取消在途监听。
- **关键行为**：事件是整套系统的扩展点。支持 `emit`（广播）/ `waterfall`（中间件改写，不 `next()` 即短路）/ `parallel` / `serial` / `bail` 五种分发模式；所有事件名集中在 `core/events_catalog.py`（`Events` 常量，单一真相源），防止改名/拼错导致运行期静默失效。
- **协作**：所有插件都通过 `ctx.events` 互发通知（权限裁决、预算记账、进度推送均挂在事件上）。

### 2. `tasks` —— 任务/工具注册表（`agent.tasks`）
- **文件**：`sys/tasks.py`
- **功能**：统一的服务注册与调用入口，是「工具调用」能力的基础。
- **关键行为**：
  - `register(ctx, name, *, schema, handler, owner, description, risk)`：名称唯一，**注册即逆**（teardown 自动卸载）。
  - `invoke(ctx, name, *, caller, **kwargs)`：**先过 `agent/tasks/pre-execute` 能力事件（guard 挂这）→ schema 轻量校验 → 执行 → 发 `post-execute`**。
  - `catalog(include=...)`：把注册任务暴露成「工具目录」（含 `risk` 风险级别），供规划器/ReAct 选工具。
  - `resolve(name)`：返回可调用入口（面向 driver/桥）。内置 `agent.ping`（健康检查）。
  - `risk`：`read/write/danger` 风险元数据（当前只落字段与目录输出，**无判定行为**，供未来 approval 消费）。
- **协作**：`chat`/`react` 的所有工具执行、MCP 工具桥的桥接，最终都走 `ctx.tasks.invoke`。

### 3. `llm` —— 模型接缝（`agent.llm`）
- **文件**：`sys/llm.py`
- **功能**：把「调用大模型」抽象成一个能力接缝，消费方一律 `ctx.llm.complete(...)`，不关心背后是哪个 provider。
- **关键行为**：
  - `complete(ctx, messages, *, schema, tools, caller)`：先发 `agent/llm/pre-call`（waterfall，**guard/budget 在此裁决**，被拒→ `CapabilityNotImplemented`）；再调 provider；最后发 `agent/llm/post-call`（emit 记账）。
  - **Provider 三选一**（按配置）：
    - `FixtureLlm`：离线确定性 canned 响应（测试/自举）。
    - `DeepSeekLlm`：真实 OpenAI 兼容接口，`schema`→`json_object`、`tools`→native function-calling；支持**流式传输（B5.2 打字机 + 思考链 `reasoning_content`）**；wire 转换 `_wire_messages/_wire_tools/_wire_call` 处理内部消息形状与工具名净化/还原。
    - `PlaceholderLlm`：未配 key/未接入 → 诚实拒绝，**不虚标**。
  - `tools` 是**加法扩展**：不传即旧行为；不支持 native 工具调用者诚实报缺。
  - key 解析顺序：`config.agent.llm.api_key` → 环境变量 `DEEPSEEK_API_KEY` → 工程根 `local_secrets.json`（凭据不落代码）。
- **协作**：`chat`（规划/解说）、`plugins.memory`（提取）、`plugins.react`（每轮 Think）均为消费者。

### 4. `storage` —— 存储与迁移（`agent.db` / `migrations`）
- **文件**：`sys/storage.py`
- **功能**：独立 SQLite 库（默认 `data/agent.db`，namespace 统一 `agent_*`）+ 迁移注册器。
- **关键行为**：
  - `SqliteDb`：stdlib sqlite3，零后台线程（事件循环内直跑 + `asyncio.Lock` 串行化防并发写）。
  - `MigrationRegistry`：路径内**幂等**应用；**防脏武**——库里已应用但当前代码已删的迁移名 → 拒绝启动（切勿删旧迁移）。
  - 所有表 DDL 集中在 `tables.py`，装配期由 Storage 全量建表。
- **表**：`agent_migrations`（迁移登记）、`agent_events`（审计脊柱）、`agent_budget_log`（成本账）、`agent_messages`（短期记忆）、`agent_memories`/`agent_entities`/`agent_memory_edges`（长期记忆）。
- **协作**：被 `audit`、`guard`、`memory`、`chat` 等几乎所有需要持久化的插件依赖。

### 5. `budget` —— 成本护栏（`agent.budget`）
- **文件**：`sys/budget.py`
- **功能**：挂在**模型接缝**上的成本闸门（非管线内嵌）。
- **关键行为**：
  - `agent/llm/pre-call`（waterfall）：`已花 + 本次估算 > max_tokens_per_run` → 否决短路；否则**预借记账**（写 `ctx.state.budget_spent`）。
  - `agent/llm/post-call`（emit）：真实 token 落 `agent_budget_log`。
  - `reset_on_new_session`（默认 True）→ 监听 `agent/chat/reset` 归零累计。
  - token 估算为「字符数≈tokens/4」的粗略口径（诚实标注 estimate）。
- **协作**：与 guard **同一 seam 多裁决**，执行序 = 装配注册序，**guard 先、budget 后**。

### 6. `guard` —— 权限网关（`agent.guard`）
- **文件**：`sys/guard.py`
- **功能**：对等执行契约/权限网关，所有模型调用与工具执行的最终裁决点。
- **关键行为**：
  - 矩阵是配置数据 `config.agent.permissions`（`default: open`、`llm`、`task_allow`、`task_deny`），**默认全开**（不先限制能力）。
  - 挂在 capability 事件 `agent/llm/pre-call`、`agent/tasks/pre-execute`（`prepend=True` 先裁）。
  - 允许 → `next()` 放行；拒绝 → 不带 `next()` 短路，返回 `{denied, reason}`。
  - **每次裁决落 durable `agent/permission/decision`**（审计=事件查询，不另建表）；`policy(ctx)` 供插件页「权限」视图。
- **协作**：被 `budget` 同 seam 依赖；MCP 高危工具（PowerShell/Registry/Process）建议在此 `task_deny` 收紧。

### 7. `loop` —— 任务驱动（`agent.loop`）
- **文件**：`sys/loop.py`
- **功能**：第一 driver（loop-as-plugin）。实现 `AgentDriver` 协议（`run/cancel/when_idle`），消费方只依赖接口，换驱动=换插件。
- **关键行为**（实现 `ProduceDriver`）：
  - `run(ctx, *, task, params)`：`tasks.resolve(task)` → 执行 → 返回 `RunResult{ok,result,error,skipped,span_ms}`。
  - **收敛三条件**：重叠 skip（同名任务上一轮未结束则跳过）、`deadline_s` 上限（默认 60s）、单次异常捕获（不 kill 循环）。
  - `cancel()`：`Task.cancel()` 在任意 await 点即时生效（B6 基础设施）。
- **协作**：与 `chat` 是两个并列 driver；当前 Web harness 默认用 `chat`。

### 8. `chat` —— 会话驱动（`agent.chat`）
- **文件**：`sys/chat.py`
- **功能**：第二 driver（交互外设），把用户输入变成「规划→执行→解说」三步闭环。
- **关键行为**（`ChatDriver.send`）：
  - ① **规划**：LLM(`<!--chat:plan-->`) → JSON `{intent: tool_call|ping|chat, tool, args}`；提示词内注入记忆块、动态工具目录、`_env_facts`（真实用户名/主目录/桌面，并禁用 `%VAR%`/`~` 占位符）。
  - ② **执行**：机器权威，一律经 `ctx.tasks.invoke`（guard 照生效、owner/caller 审计）。
  - ③ **解说**：LLM(`<!--chat:answer-->`) 按机器证据复述成 markdown。
  - **短期记忆**：每轮把 user/assistant 写入 `agent_messages`，取最近 ~6 轮作上下文。
  - **长期记忆**：若 `ctx.has("memory")` → 轮后调 `remember`（失败不破坏主链路）。
  - **多步委托**：若 `ctx.has("react")` → 把本轮交给 ReAct 循环（`_send_via_react`），契约不变；**停用 react 自动回退单步路径**。
  - **诚实降级**：LLM 任一步被拒（预算/guard/解析失败）→ 给证据兜底文本 + 如实上浮 `error`。
  - durable 事件：`agent/chat/in`、`agent/chat/done`、`agent/chat/reset`。
- **协作**：核心枢纽，串联 `llm`/`tasks`/`memory`/`react`/`events`。

---

## 二、支撑模块（`sys/` 内非 `@plugin`，但系统关键）

### `audit` —— durable 落盘（`sys/audit.py`）
- 提供 `durable(ctx, event, payload)`：写一条 durable 事件到 `agent_events`（可跨重启）。
- 无 db（纯内存装配）→ **不落盘也不假装**（诚实）；写失败只告警、不阻断主流程。

### `mcp_client` —— MCP stdio 客户端（`sys/mcp_client.py`）
- 通用 MCP **stdio 客户端**（零第三方依赖，仅 stdlib）：`start()` → `initialize` + `notifications/initialized` → `list_tools()` → `call_tool(name, args)`。
- 子进程断连/超时 → 诚实抛错（不静默）；是后面两个 MCP 工具桥插件的公共底座。

### `plugin_manager` —— 目录插件管理（`sys/plugin_manager.py`）
- 发现/启停/新增/卸载：
  - `discovered()`：扫 `agent/plugins/<包>/` 得候选元数据（id/package/layer/provides/inject/tables/runnable）。
  - `set_enabled(id, enable)`：持久化期望态到 `data/agent_plugin_state.json`。
  - `scaffold(name, provides)`：建目录 + 模板 + 自动启用。
  - `remove(id)`：停用 + 删目录 + `removed` 标记（**表与数据保留**）。
  - `load()`：只加载 `enabled()` 里的插件。
- **诚实边界**：装配在启动/重建时一次性完成 → 启停「重启/重建后生效」，不热插拔。
- 加载器支持「插件包」（`__init__.py` 为多文件包，可包内相对导入），通过命名空间父包 `agent_plugins` 实现。

### `progress` —— 流式/取消进度桥（`sys/progress.py`）
- B5 流式 / B6 取消的**公共底座**：把内核事件桥接成异步队列，供 SSE/CLI/WebSocket 消费，**与 HTTP 无关**。
- `tap_progress(ctx, *, queue, events)`：用 `ctx.scope()` 订阅 `Events`（退出作用域自动注销），只认 `Events` 常量、不 import 任何业务/插件 → 与 ReAct 零耦合。
- 默认订阅：`REACT_STEP_START/END`、`TASKS_POST_EXECUTE`、`LLM_TOKEN`（打字机）、`LLM_THINKING`（思考流）；`token/thinking` 帧类型由 `FRAME_TYPES` 映射。
- 注意：durable 帧（`REACT_FINAL/EXHAUSTED`）只落库不进总线，订阅不到；最终文本由调用方 await 结果后作为 `final` 帧发出。

---

## 三、目录插件（`agent/plugins/`，可管理：启用/停用/卸载）

### 1. `memory` —— 记忆子系统（`plugins.memory`）
- **文件**：`plugins/memory/__init__.py`（单文件包）
- **功能**：Zep 式长期记忆——时序事实 + 打分衰减 + 上下文注入，压成单一插件。
- **关键行为**（`MemoryDriver`）：
  - `remember(payload)`：LLM 提取（`<!--memory:extract-->`）→ 落 `agent_memories`（fact/intention；实体/关系图接口已留但当前 MVP 主要落 fact/intention）。被 guard/预算拒绝 → 诚实跳过。
  - `add(kind, content, ...)`：手动/决策直写（不经过 LLM），合并去重（`_mem_id` 用 SHA1 内容哈希）。
  - `recall(query, limit)` / `context(query, limit)`：召回打分 = **词面×0.5 + 新鲜度×0.3 + 重访×0.2**（MVP 词面；向量/MMR 后置），组装成注入块。
  - `stats/prune/wipe`：运维；`prune` 按「足够旧且低分」淡化清理。
  - durable：`agent/memory/remembered`、`agent/memory/recall`。
- **协作**：由 `chat` 在每轮规划/解说前注入记忆块，并在轮后提取；启用态由 `ctx.has("memory")` 控制（停用即跳过）。
- **现状缺口**（见《记忆插件-能力缺口分析.md》）：语义召回、被动摄取、图谱（entity/edge 表当前无写入）、kind 覆盖不全。

### 2. `react` —— ReAct 多步工具循环（`plugins.react`）
- **文件**：`plugins/react/{__init__,prompt,components,loop}.py`（四文件插件包）
- **功能**：让模型「多步使用工具」：Think → Act+Observe → 收敛（模型不再返回 tool_calls 即最终答复）。
- **关键行为**（`ReactLoop`）：
  - 每轮 Think 经 `ctx.llm`（guard/budget 照生效）；有 tool_calls → Act（`ctx.tasks.invoke`）→ Observe 把结果（截断后）回灌 → 下一轮；无 tool_calls → 收敛。
  - **三把闸**：`max_steps`（默认 8）、`deadline_s`（默认 120）、token 预算（每步已过 `pre-call`，天然生效）。
  - **失败即观察**：工具异常/参数错/被拒一律转 `ToolResult(is_error/denied)` 回灌让模型自纠；同一工具连拒超 `max_rejects`（默认 3）才终止（防死循环）。
  - **触顶诚实收尾**：durable `agent/react/exhausted` + 标注原因；`finalize_on_exceed` 再答一次；不崩/不静默/不假成功。
  - **四组件可换接缝**（Protocol）：`Reasoner/Actor/Observer/Finalizer`；`ctx.provide("react.actor", ...)` 即替换（将来 approval 只需换 Actor）。
  - **零特权**：模型一律 `ctx.llm`、工具一律 `ctx.tasks.invoke` ⇒ guard/schema/审计/预算全部继承。
  - `tables=()` ⇒ 卸载零残留（轨迹落 `agent_events`）。
- **协作**：由 `chat` 软委托（`ctx.has("react")` 感知，不 import 实现）；停用即回退单步路径。

### 3. `windows_mcp` —— Windows MCP 工具桥（`plugins.windows_mcp`）
- **文件**：`plugins/windows_mcp/__init__.py`
- **功能**：把 `windows-mcp-server`（MCP stdio 子进程）的工具自动挂进 agent 工具目录。
- **关键行为**：
  - `apply`：起 MCP 子进程（`command` 默认 `uvx windows-mcp serve`，可 env/配置覆盖）→ `list_tools()` → 把每个工具注册为 `mcp.windows.<tool>` 任务（owner=`plugins.windows_mcp`）。
  - 生命周期对称：起子进程 → `ctx.effect` 关子进程。
  - 诚实边界：未配置 command → 装配期记录未就绪；启动失败 → 记录原因、不假挂、不拖垮装配。
  - `status()`：暴露 `windows_mcp_tools` / `windows_mcp_error` 供插件页。
- **安全**：工具为**高危**（任意 PowerShell/注册表/文件/进程/键鼠）。调用仍过 guard（默认 open），建议在 `task_deny` 收紧 `mcp.windows.PowerShell`/`Registry`/`Process` 等。

### 4. `playwright_mcp` —— Playwright MCP 工具桥（`plugins.playwright_mcp`）
- **文件**：`plugins/playwright_mcp/__init__.py`
- **功能**：把 `microsoft/playwright-mcp`（MCP stdio 子进程）的工具（browser_navigate / browser_click 等）挂进 agent 工具目录。
- **关键行为**：与 `windows_mcp` 同构，差异在默认启动命令：
  - `command` 默认 `npx`（Windows 上自动解析 `.cmd`/`.exe` 绝对路径）；`args` 默认 `-y @playwright/mcp@latest`。
  - 默认 `headless` + `chromium`（后台服务无显示器）；可配 `headless=false`、浏览器、`--isolated`、caps（默认不开，仅核心无障碍树自动化）。
  - 同样支持 `resolve(cfg, env)`（config > env > 默认）、对称生命周期、诚实边界、`status()`。
- **协作**：同为 MCP 工具桥范式示例——「**任何外部系统接入 = 一个目录插件 + 一组 tasks**」。

---

## 四、离线夹具与运行入口

### `fixtures.llm` —— 离线确定性 LLM（`fixtures/llm.py`）
- `FixtureLlm`：按 system prompt 标记路由到 canned 响应，保证「同一输入→同一输出」。
- 路由标记：`<!--chat:plan-->`（规划）、`<!--chat:answer-->`（解说）、`<!--memory:extract-->`（抽取）、`<!--react:turn-->`（脚本化 tool_calls）、`<!--react:final-->`（触顶收尾）。
- ReAct 脚本化：`_react_reply` 按「请求关键词 + 已完成轮数」给出确定性 tool_calls 或最终答复（覆盖两步链路、自纠、大结果、触顶等场景）。
- 开启 `stream` 时把散文切成若干片 emit `LLM_TOKEN`（B5.2 离线切帧）。
- 用途：全测试不触网、不触任何外部进程即可跑通对话闭环（对应 `python -m agent.run.chat --fixture`）。

### `run/chat.py` —— Web harness + 无头入口
- **文件**：`run/chat.py`
- **功能**：通用 Agent 前端（会话/事件/权限/插件/记忆/工具 六视图）+ 无头 `--once`。
- **关键能力**：
  - `POST /api/chat/stream`：SSE 逐帧推送（`start` → `progress`/`token`/`thinking` → `final`/`cancelled`/`error`）—— 整轮包成 `asyncio.Task`。
  - `POST /api/chat/cancel {run_id}`：按 run_id 取消在跑那轮（任意 await 点即时生效；断连自动取消，不留孤儿任务）。
  - 插件管理 API：`/api/plugins`（`enable`/`scaffold`/`remove`）—— 改动即时**重建容器**生效。
  - `data/agent_plugin_state.json` 驱动目录插件期望态。

---

## 五、贯穿全局的关键设计约定（摘要）

1. **无特权内核**：模型一律 `ctx.llm`、工具一律 `ctx.tasks.invoke` ⇒ guard/budget/schema/审计天然继承，无后门通道。
2. **诚实边界**：无 key / 缺数据 / 缺失能力 / 被拒 → 一律 `CapabilityNotImplemented` 如实上报，不虚标、不静默降级。
3. **审计即脊柱**：凡进模型、进裁决的输入都能从 `agent_events`/`agent_budget_log` 重建；tokens 逐条核算。
4. **隐式契约加固**：事件名（`Events` 常量）/ 服务键（`Services` 常量）/ 配置 schema 校验三层 fail-fast，把「改名拼错→运行期静默失效」变成启动即报错。
5. **来源/层级正交**：目录插件的「可管理性」由来源（目录）判定，与 `layer`（system/business 语义标签）无关。
6. **两个驱动可换**：`loop` 与 `chat` 均实现 `AgentDriver` 协议，消费方只依赖接口。

---

## 六、插件清单速查表

| 插件 | 服务键 | 位置 | 层级 | 可管理 | 一句话功能 |
|---|---|---|---|---|---|
| events_plugin | `events` | sys | system | 否 | 事件总线装配（5 种分发） |
| tasks | `tasks` | sys | system | 否 | 任务/工具注册表 + 调用 + 目录 |
| llm | `llm` | sys | system | 否 | 模型接缝（fixture/deepseek/占位） |
| storage | `db`/`migrations` | sys | system | 否 | 独立 SQLite 库 + 迁移注册器 |
| budget | `budget` | sys | system | 否 | 成本护栏（挂 llm 接缝） |
| guard | `guard` | sys | system | 否 | 权限网关 + durable 审计 |
| loop | `loop` | sys | system | 否 | 任务驱动（driver 协议） |
| chat | `chat` | sys | business | 否 | 会话驱动（规划→执行→解说） |
| audit | —（helper） | sys | system | 否 | durable 事件落盘 |
| mcp_client | —（helper） | sys | system | 否 | 通用 MCP stdio 客户端 |
| plugin_manager | —（helper） | sys | system | 否 | 目录插件发现/启停/新增/卸载 |
| progress | —（helper） | sys | system | 否 | 流式/取消进度桥（事件→队列） |
| **memory** | `memory` | plugins | system | **是** | 长期记忆（提取+衰减+召回注入） |
| **react** | `react` | plugins | system | **是** | ReAct 多步工具循环（三把闸） |
| **windows_mcp** | `windows_mcp` | plugins | business | **是** | windows-mcp 工具桥 |
| **playwright_mcp** | `playwright_mcp` | plugins | business | **是** | playwright-mcp 工具桥 |
| FixtureLlm | —（fixture） | fixtures | — | 否 | 离线确定性 LLM（测试/自举） |
| run/chat | —（入口） | run | — | 否 | Web harness + 无头 + SSE/取消 |
