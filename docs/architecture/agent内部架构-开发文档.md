# Agent · 内部架构开发文档

| 文档属性 | 内容 |
|---|---|
| 文档状态 | **v2.0（通用型 agent）开发文档** —— 同步至当前实现 |
| 归属 | 通用 agent 运行时（`agent/`，独立可自举，不依赖任何外部引擎/Node） |
| 哲学来源 | DeepSeek-Harness 的「everything-is-a-plugin ⇒ Cordis」——**slim 重实现**，只借骨架不搬产品（§1） |
| 关联文档 | 《agent记忆架构-方案.md》（记忆子系统设计与落点）·《记忆插件-能力缺口分析.md》（记忆现状待办）·《agent通用能力-完善路线.md》（通用能力待办与优先级）·《ReAct插件-设计方案.md》（多步循环设计，§2 A1 的落地）· `README.md`（运行入口与现状摘要）· `doc/`（历史归档：领域与接口设计，**不作工作参考**） |
| 编制日期 | 2026-09-22（v1.0）／**2026-09-24（v2.0：由领域施工文档改写为通用型 agent 开发文档）** |

> **v2.0 变更说明**：v1.0 是面向特定领域的施工文档（含技能库 / 评分管线 / 领域数据表 / 引擎桥）。v2.0 移除全部领域与引擎专用内容，改为**如实描述当前通用 agent 实现**：内核 + 系统插件 + 两个 driver（loop / chat）+ 可管理的目录插件 + MCP 工具桥 + 记忆子系统。领域内容（如选品策略）落在各自的领域文档中，不进入本卷。

---

## 0. 结论先行

1. **Agent = 自带宿主的 slim Cordis 运行时**：自己一份 `agent.api`（Context / @plugin / Container / EventBus / effects / errors），零 `import engine.*`、零 Node、零三方重依赖；**可离线自举**（无网络、无真实模型即可跑通对话闭环）。
2. **一切皆插件，内核无特权**：主循环 `loop`、模型接缝 `llm`、任务注册表 `tasks`、事件 `events`、存储 `storage`、预算 `budget`、权限 `guard`、会话驱动 `chat` **全部是插件**；业务能力（记忆、工具桥）与**多步循环（`react`）**同样以插件挂入。
3. **「来源」与「层级」是两个正交的轴**：**来源**（内置 builtin / 目录 `agent/plugins/`）决定**生命周期**（能否启用 / 停用 / 卸载）；**layer**（`system` / `business`）只是**语义标签**。可管理性由来源判定（`delta ≠ system/builtin`），与 layer 无关。
4. **事件是扩展点，按三域切分**：durable 落库 / live 在途 / capability 挂在接缝上；五种 dispatch 模式做控制面（§8）。
5. **审计即轴心（model-visible means logged）**：凡进模型、进裁决的输入，都能从 `agent_events` / `agent_budget_log` 重建；tokens 逐条核算。
6. **诚实边界**：无 key / 缺数据 / 缺失能力 / 被 guard 或预算拒绝 → 一律 `CapabilityNotImplemented` 如实上报，**不虚标、不静默降级**。

> 一句话：**照 Cordis 的「无特权内核、一切皆插件」造一台通用 agent；能力靠插件挂、管理靠插件页，缺什么就诚实报缺。**

---

## 1. 借鉴边界：搬什么、不搬什么（有据）

### 1.1 六根脊梁（从 DeepSeek-Harness / Cordis 搬入 agent，逐条有出处）

| # | 脊梁 | 来源 | 落到 agent |
|---|---|---|---|
| S1 | **Ctx 服务仓 + inject 依赖声明** | cordis-primer「A context is a repository of services」「Declare service dependency via inject」 | `agent.api.Context`、@plugin 的 `inject/provides` |
| S2 | **类型化事件 + 5 种 dispatch**（emit/waterfall/parallel/serial/bail） | cordis-primer §Dispatch Modes | `agent.api.EventBus`，逐事件标 `@mode` |
| S3 | **注册皆可逆 effect**（teardown 对称展开） | cordis-primer「Registrations are reversible effects」 | `ctx.effect` / `ctx.on` |
| S4 | **协议装配：profile → bundle → patch 有序叠层** | architecture.md §Profiles and bundles | `profiles.build_agent`（配置 + patch 深合并） |
| S5 | **driver-as-plugin（循环可换、消费方只依赖接口）** | subsystems/core.md「Extension plugins … never on agent-loop directly」 | `AgentDriver` 协议；`loop` / `chat` 是两个 driver 插件 |
| S6 | **step/turn 模型 + 守卫点**（dsh `<tools.*>` ⇒ 本卷 `tasks.*` 钩点：pre-execute → execute → post-execute） | architecture.md §Turn flow | 任务执行的守卫点骨架（§4.2 / §5） |

并采纳两条软约束：**capability seam = Service Definition / Provider / Consumer 三件套**；**「model-visible means logged」不变量**。

### 1.2 三处主动降配（不搬）

1. **不 vendor dsh 产品**：不搬其 13k 文件 monorepo + Node runtime + 网页 UI + 会话 JSONL 的 fork/resume/migration。只重实现它的**内核 pattern**。
2. **不搬 inbox 型会话产品**：dsh 的 Agent 是「inbox + 交互句柄」。本卷的会话能力由 `chat` 驱动**轻量**实现（plan → execute → narrate，§5.2），**不建** inbox、不做会话 JSONL 版本迁移；交互形态后置为新 driver 插件即可。
3. **砍清单（不做）**：approval/sandbox、shell/subprocess/terminal 内建、webhook、agent-teams/subagent、desktop、会话 JSONL 版本迁移。

> 上游仓库存「developer preview，破坏性变更」，故**只摘思想、不锁它的 API 名**。

---

## 2. 架构总览

```
agent/                            ← agent 运行时（slim Cordis 重实现，独立）
├─ api.py                内核对外唯出口：Context/plugin/Container/EventBus/LlmProvider/errors（零业务词）
├─ core/                 内核实现：container / context / events / errors / contract
├─ sys/                  系统层插件（通用机制，内置恒定装配）
│  ├─ events_plugin.py  agent.events     事件总线装配（§8）
│  ├─ loop.py           agent.loop       driver：跑一次注册任务（loop-as-plugin）
│  ├─ llm.py            agent.llm        模型接缝：Provider 契约 + Fixture/DeepSeek/占位（§4.1）
│  ├─ tasks.py          agent.tasks      任务/工具注册表：invoke / resolve / catalog（§4.2）
│  ├─ storage.py        agent.db/migrations  独立 sqlite 库 + 迁移注册器（§6）
│  ├─ budget.py         agent.budget     成本护栏（挂在 llm 接缝，§4.3）
│  ├─ guard.py          agent.guard      权限网关：capability 裁决 + durable 审计（§4.4）
│  ├─ chat.py           agent.chat       会话驱动（第二 driver：plan → execute → narrate，§5.2）
│  ├─ audit.py                           durable 事件落盘 helper（durable / _now_iso）
│  ├─ mcp_client.py                      MCP stdio 客户端（通用，供工具桥插件使用）
│  └─ plugin_manager.py                  目录插件：发现 / 启停 / 新增 / 卸载（§7）
├─ plugins/              目录插件（来源 = 目录 ⇒ 可启停卸；layer 只是语义标签）
│  ├─ memory/       plugins.memory       记忆子系统（layer=system，默认启用）
│  ├─ react/        plugins.react        ReAct 多步工具循环（layer=system，默认启用；prompt/components/loop 三子模块）
│  └─ windows_mcp/  plugins.windows_mcp  windows-mcp 工具桥（layer=business，默认启用）
├─ fixtures/llm.py       离线确定性 LLM 夹具（零触网；chat/memory/react 五标记路由）
├─ run/chat.py           入口：网页 harness / 无头 `--once`（§9.2）
├─ profiles.py           装配：BASE 配置 + SYS/BUSINESS 插件 + 目录插件 opt-in
├─ tables.py             agent_* 表 DDL（迁移清单）
└─ tests/                离线确定性测试（82 项）
```

- **import 边界（硬规则）**：agent 本体只 `from agent.api import …`；不得 `import engine.*`、不得 import 大模型 SDK（仅 `agent.llm` 的 DeepSeek provider 允许 1 处 HTTP 出入，凭据不落明文）。
- **三层容器语义**：`core/` 零业务词（`test_agent_purity` 守卫）；`sys/` = 通用机制；`plugins/` = 可选目录插件。业务词只允许出现在 `plugins/` 内的业务插件、`fixtures/` 与领域文档。
- **两个正交的轴（重要）**：放进 `plugins/` 只改变「生命周期」（可启停卸），不改变「它是什么」（`layer`）。故 `plugins.memory` 是 `layer=system`（系统组件）却可被插件页启停卸——见 §7。

---

## 3. 内核：`agent.api`（slim Cordis 重实现）

### 3.1 Context —— 服务仓 + 作用域

```
ctx.provide(key, value) -> detach          # 注册服务；D-3 同作用域重复 → Conflict
ctx.get(key, default=None) / ctx.has(key)  # 自身 → 父链查找；关闭后只放行 default
ctx.set(key, value)                        # 覆盖已提供服务；未提供 → NotFound
ctx.keys()                                 # 已注册服务键
ctx.state                                  # 作用域变量（ctx.state.<x>，每容器隔离）
ctx.effect(disposer)                       # 可逆副作用；作用域 teardown 逆序执行
ctx.on(name, handler, *, prepend, once)    # 事件监听挂作用域，自动 effect 卸载
ctx.child(name) / ctx.scope(name)          # 子作用域 = GC 单元；teardown 逆序
ctx.capture() / ctx.release(cap)           # 装配期区间快照 / 回滚（失败对称收尾用）
```

### 3.2 插件协议 —— `@plugin`

```python
@plugin
class XxxSubsystem:
    name = "agent.xxx"        # 全局唯一（D-3）
    layer = "system"          # system=通用机制 / business=业务插件（**语义标签**，不闸管理）
    inject  = ["db", "llm", "events"]   # 依赖服务键（拓扑自动排序）
    provides = ["xxx"]        # 对外 ctx.<key>
    tables  = ("agent_xxx",)  # 自带表元数据（控制台/插件页聚合用）
    description = "..."

    async def apply(self, ctx): pass    # 装配期：建表/注册服务/订阅/effect
    async def start(self, ctx): pass    # 启动期：只准备，不自动执行
    async def run(self, ctx, **params): pass   # 主任务（可选；目录插件可被调度）
    async def status(self, ctx): pass   # 可选：运行态摘要（插件页展示，非必需）
    async def _teardown(self): pass     # 对称清理（通常经 ctx.effect）
```

另支持 `@plugin(name=..., inject=..., provides=...)` 函数形态（函数体即 apply）。

### 3.3 Container —— 装配 / 拓扑 / 对称 teardown

- `container.add(*插件)` 登记；`assemble()`（拓扑序 apply）→ `start()`（拓扑序 start）→ `teardown()`（逆序回收，effect 逆序 + 服务回收）。
- 拓扑解析：`inject` 依赖边；缺失给**缺失清单**；环报错；D-3 键唯一，冲突报错。`ready` 排序按插件 id 字典序（确定性）；**依赖方必须显式 `inject`**，不许靠字母序巧合。
- 装配期读某服务但未声明 `inject` ⇒ 拿 `None`（容器硬化）。
- **失败回滚**：任一插件 apply 抛错 → 先释放该插件已注册的部分（对称收尾），再逆序回收已完成插件。
- `plugin_manifest()` / `plugin_statuses()`：通用 introspection，供插件页与诊断（`status` 钩子未实现则诚实返回 `None`）。

### 3.4 EventBus —— 五种 dispatch（契约级）

| 模式 | Await | 顺序 | 返回值 | 典型用途 |
|---|---|---|---|---|
| `emit` | 否 | 注册序观察 | 无 | 广播遥测（durable/live） |
| `waterfall` | 是 | 序 + `next()` 传值 | 有 | 中间件/改写（预算、守卫） |
| `parallel` | 是 | 并发 | 无 | 并行消费者 |
| `serial` | 是 | 注册序 | 有 | 顺序裁决 |
| `bail` | 否 | 序，首个 bail 短路 | 有 | 策略性单决策（能力拒绝） |

`waterfall` 是 around-middleware：监听收到 `(…args, next)`，`next()` 放行/改写；**不 `next()` 即短路**（单决策事件的短路是设计本意）。payload 强制纯数据（可 JSON 化，防跨作用域泄漏）。

### 3.5 errors（诚实边界）

`CapabilityNotImplemented` / `ServiceNotFoundError` / `ServiceConflictError` / `CyclicDependencyError` / `MissingDependencyError` / `EventDispatchError` / `ScopeClosedError` / `TeardownError` / `AgentError`。

---

## 4. 子系统清单（键 = 服务键）

| 插件 | 键 | layer | 装配分组 | 职责 |
|---|---|---|---|---|
| **loop** | `loop` | system | 内置业务 | driver：跑一次注册任务；提供 `AgentDriver` 协议（§5.1） |
| **chat** | `chat` | business | 内置业务 | 会话驱动：意图规划 → 执行 → 解说（§5.2） |
| **llm** | `llm` | system | 内置系统 | 模型接缝：Provider 契约 + Fixture/DeepSeek/占位（§4.1） |
| **tasks** | `tasks` | system | 内置系统 | 任务/工具注册表：`invoke` / `resolve` / `catalog`（§4.2） |
| **events** | `events` | system | 内置系统 | 事件总线装配 + 三域协议（§8） |
| **storage** | `db` / `migrations` | system | 内置系统 | `agent.db` 独立库 + 迁移注册器（§6） |
| **budget** | `budget` | system | 内置系统 | 成本护栏：capability 事件挂 llm 接缝（§4.3） |
| **guard** | `guard` | system | 内置系统 | 权限网关：capability 裁决 + 矩阵 + durable 审计（§4.4） |
| **plugins.memory** | `memory` | system | 目录插件 | 记忆子系统（LLM 提取 + 打分衰减 + recall/context 注入） |
| **plugins.react** | `react` | system | 目录插件 | ReAct 多步工具循环（Think→Act+Observe→收敛 + 三把闸 + 四组件接缝）（§5.3） |
| **plugins.windows_mcp** | `windows_mcp` | business | 目录插件 | windows-mcp-server 工具桥（MCP stdio → agent 工具目录） |

> **装配分组 ≠ layer**：`loop` 的 layer 是 `system` 却在「内置业务」分组；`chat` 是 `business` 也在该分组。分组只表达「随 `build_agent` 恒定装配」，与语义层无关（§0.3）。

### 4.1 `agent.llm` —— 模型接缝（Service Definition / Provider / Consumer）

```
ServiceDefinition:  LlmProvider（agent.core.contract，零第三方依赖）
  async complete(ctx, messages: list[dict], schema: dict|None = None,
                 tools: list[dict]|None = None) -> dict      # tools = 加法式扩展（native 工具调用）
  interface_version = (1, 0)

装配层:  LlmService（ctx.llm）
  complete(ctx, messages, *, schema=None, tools=None, caller="unknown") -> LlmResult
    ├─ waterfall "agent/llm/pre-call"（guard 先裁、budget 后裁；被拒 → CapabilityNotImplemented）
    ├─ provider.complete(ctx, messages, schema=…, tools=…)   ← Provider 层不含 caller
    └─ emit "agent/llm/post-call"（tokens 记账）

Provider:  FixtureLlm      离线确定性 canned 响应（测试/自举，零触网）
           DeepSeekLlm     真实（OpenAI 兼容；schema 非空 → json_object；tools 非空 → native tools）
           PlaceholderLlm  未接入/未配 key → CapabilityNotImplemented（不虚标）
```

- key 解析顺序：`config.agent.llm.api_key` → `env DEEPSEEK_API_KEY` → 工程根 `local_secrets.json`。
- 凭据不落代码；**tokens 逐条入 budget**（§4.3）。
- **`tools` 是加法扩展**：不传即旧行为（chat/memory 不受影响）。传 `tools` 时返回可含
  `tool_calls: [{id, name, arguments}]`；不支持 native 工具调用的 provider 应诚实报缺（不得用文本约定假装）。
  `agent/sys/llm.py` 的 `_wire_messages / _wire_tools / _wire_call` 负责内部消息形状 ↔ OpenAI wire 的转换。
- 消费方：`chat`（plan/narrate）、`plugins.memory`（提取）、**`plugins.react`（每轮 Think）**。

### 4.2 `agent.tasks` —— 任务/工具注册表

- `tasks.register(ctx, name, *, schema, handler, owner, description, risk)`：名称唯一（D-3），**注册即逆**（`ctx.effect` 自动卸载）。
- `risk`：工具风险元数据 `read / write / danger`（默认 `write`，保守）。**本次只落字段与目录输出，无判定行为**——供 approval / 权限模式消费（§5.3、AC-9）。
- schema 轻量校验：`{参数名: {type, required, default}}`；未知参数拒绝、类型不符拒绝。
- `tasks.invoke(ctx, name, *, caller, **kwargs)`：先过 `agent/tasks/pre-execute`（waterfall，guard 挂这）→ 校验 → 执行 → `agent/tasks/post-execute`（emit）。
- `tasks.resolve(ctx, name) -> (lambda **kw)`：面向 driver / 桥的可调用入口（无则 `None`）。
- `tasks.catalog(include=None)`：工具目录（供 tool-call 编排：把注册任务暴露成可调用的工具描述，含 `risk`；`include` 是权限模式的裁剪点）。
- 内置 `agent.ping`（存活/时间戳，owner=None）。

### 4.3 `agent.budget` —— 成本护栏（挂在接缝，非管线内嵌）

- `agent/llm/pre-call`（waterfall）：`已花 + 本次估算 > 上限` → 不调 `next()` 短路；否则**预借记账**（写入 `ctx.state.budget_spent`）后放行。
- `agent/llm/post-call`（emit）：以实际 tokens 落 `agent_budget_log`（审计行）。
- 配置 `config["agent"]["budget"]`：`{max_tokens_per_run, reset_on_new_session}`；额度为**容器生命周期累计**，`reset_on_new_session` 时监听 `agent/chat/reset` 归零。
- **同 seam 多裁决次序**：执行序 = 装配注册序，短路即停；**guard 先、budget 后**（权限先、成本后）。

### 4.4 `agent.guard` —— 权限网关

- 矩阵是配置数据 `config["agent"]["permissions"]`：`{default: "open", llm: "open", task_allow: ["*"], task_deny: []}`，**默认 open**（不先限制能力）。
- 挂在 capability 事件上裁决（waterfall，`prepend=True` 先裁）：`agent/llm/pre-call`、`agent/tasks/pre-execute`。
- 允许 → `next()` 放行；拒绝 → 不带 `next()` 短路，返回 `{"denied": True, reason}`。
- **每次裁决落 durable** `agent/permission/decision`（who/level/target/decision/at）——权限审计 = 事件查询，**不另建表**（保持单条审计脊柱）。
- `guard.policy(ctx)` → 当前矩阵副本（诊断用，插件页「权限」视图）。

---

## 5. 驱动：`agent.loop` 与 `agent.chat`

驱动（driver）= 决定「什么时候、按什么节奏让 agent 做事」。本卷有两个，均为插件，可替换/新增。

### 5.1 `agent.loop` —— 任务驱动（loop-as-plugin）

```
class AgentDriver(Protocol):
    async def run(self, ctx, *, task: str, params: dict) -> RunResult
```

- 实现 `ProduceDriver`：`run` 解析 `tasks.resolve(task)` → 执行 → 返回 `RunResult{ok, result, error, skipped, span_ms}`。
- 收敛条件：**重叠 skip**（同名任务上轮未结束则跳过）、**deadline 上限**（`config.agent.loop.deadline_s`，默认 60s）、**单次异常捕获**（不 kill 循环）。
- 「开着不活」：loop 自身不产生副作用，生产在任务实现与持久化侧。

### 5.2 `agent.chat` —— 会话驱动（第二 driver，交互外设）

```
chat.send(ctx, message, session_id="default") -> ChatReply{text, plan, evidence, tokens_in, tokens_out, ok, error, session_id}
```

三步（机器权威，LLM 只做规划与复述）：

```
① 规划  LLM(system: <!--chat:plan--> + 记忆块 + 工具目录 + 环境事实) → JSON {intent: tool_call|ping|chat, tool, args, note}
② 执行  一律经 tasks.invoke（guard 矩阵照生效，owner/caller 审计）
③ 解说  LLM(system: <!--chat:answer--> + 证据 JSON + 记忆块) → markdown 文本
```

- **短期记忆**：每轮把用户/助手消息写入 `agent_messages`（session_id + role/content 时序），并取最近 `HISTORY_LIMIT=12` 条（≈6 轮）作为会话上下文。
- **长期记忆**：若 `ctx.has("memory")`，轮次结束后调 `memory.remember(...)` 提取落库；**失败不破坏主链路**。
- **注入点**：规划与解说前各注入一次 `memory.context(query)` 记忆块（缺该插件则跳过）。
- **诚实降级**：LLM 任一被拒（预算/guard/解析失败）→ 给证据兜底文本 + 如实上浮 `error`，不给假成功。
- durable 事件：`agent/chat/in`、`agent/chat/done`、`agent/chat/reset`。
- 环境事实块（`_env_facts`）把真实用户名/主目录/桌面喂给规划器，并禁用 `%VAR%`/`~` 占位符（路径类工具取真实值）。
- **多步委托（软依赖）**：`send()` 里若 `ctx.has("react")` → 交给 `plugins.react` 走多步循环，再按同一外部纪律收尾（写短期记忆、`agent/chat/done`、喂 memory、返回 `ChatReply`）。**chat 只经 `ctx.has` 感知它，不 import 其实现**；插件停用即自动回退上面的单步路径（契约一行不改）。

### 5.3 `plugins.react` —— ReAct 多步工具循环（目录插件）

```
ctx.react.run(ctx, prompt, *, session_id="default", prior=None) -> ReactResult
  {ok, text, steps, tokens_in, tokens_out, exhausted, reason, tools, observations}
```

循环形状（取自 `loong-agent` 0.0.2）：

```
每轮：Think   ctx.llm.complete(messages, tools=可见工具目录)      ← guard/budget/审计照生效
      └─ 模型**不再返回 tool_calls** ⇒ 收敛（本轮 text 即最终答复）
      └─ 有 tool_calls ⇒ Act   ctx.tasks.invoke(逐个)              ← guard 裁决 + schema 校验 + owner 审计
                        Observe 把结果（截断后）回灌为一条 user 消息 → 下一轮
```

- **三把闸**：`max_steps`（默认 8）· `deadline_s`（默认 120）· **token 预算**（不另写——每步都走 `ctx.llm`，`agent/llm/pre-call` 上 guard/budget 已生效）。
- **失败即观察**：工具异常 / 参数错 / 被拒一律转成 `ToolResult(is_error=True)` 回灌，让模型自纠（**永不抛出**）；被拒另带 `denied=True`，同一工具连拒超 `max_rejects`（默认 3）才终止，防死循环。
- **触顶诚实收尾**：durable `agent/react/exhausted` + 标注原因；可选再问一次模型（`finalize_on_exceed`，不给工具），失败退回证据兜底文本。**不崩、不静默、不假成功。**
- **上下文治理**：工具结果按 `tool_result_max_chars`（默认 2000）截断并标注；喂给模型的窗口按 `history_keep` 裁剪（保 system + 首个用户请求 + 最近 N 条）。
- **四组件接缝**（可换）：`Reasoner / Actor / Observer / Finalizer` 均为 Protocol；`ctx.provide("react.actor", ...)` 等任一注入即替换缺省实现（与 `ctx.llm` 的 provider 选择同构）。**approval 将来只需替换 `Actor`**。
- **事件**：durable `agent/react/start|step|final|exhausted`（可重建一次 run 的轨迹）；live `agent/react/step/start|end`（前端进度）。
- **零特权**：模型一律 `ctx.llm`，工具一律 `ctx.tasks.invoke`；`tables=()` ⇒ 卸载零残留。
- **权限模式接口预留**（本次只落位置，行为不变）：`ctx.state["permission_mode"]` 状态位、`visible_tools(ctx)` 裁剪点（**恒返回全量**）、`Tool.risk` 元数据。
- 配置 `config["agent"]["react"]`：`{max_steps, deadline_s, tool_result_max_chars, history_keep, max_rejects, finalize_on_exceed}`。
- 详见《ReAct插件-设计方案.md》（含四组件定义、启停组合矩阵、与 approval 的依赖方向）。

---

## 6. 数据面：`agent.db`（独立库 + namespace）

- **独立文件**（默认 `data/agent.db`）；**namespace 统一 `agent_`**，永不与他人共用表。
- 迁移：自研 `MigrationRegistry`（sqlite 先行 + `IF NOT EXISTS` 幂等 + 已应用集合）。
- **防脏武**：迁移已写入库、但当前代码的 `MIGRATIONS` 里找不到 → **拒绝启动**（勿删旧迁移）。
- 读写纪律：只经 `ctx.db`（`execute` / `query`）；DDL 集中在 `agent/tables.py`，由 `agent.storage` 装配时全量应用。

| 表 | 用途 | 键 |
|---|---|---|
| `agent_migrations` | 迁移登记（name / applied_at） | `name` |
| `agent_events` | durable 事件日志（append-only，**审计轴心**） | `seq` |
| `agent_budget_log` | token / 成本账 | `seq` |
| `agent_messages` | 会话转写（**短期记忆**：session_id + role/content 时序） | `seq` |
| `agent_memories` | 记忆事实（**长期记忆**：kind/content/score/有效期/source） | `id` |
| `agent_entities` | 记忆实体（图：节点） | `id` |
| `agent_memory_edges` | 记忆关系边（图：边） | `(source_id, target_id, rel_type)` |

> 记忆三表的**能力现状**（`agent_entities` / `agent_memory_edges` 当前未落数据等）见《记忆插件-能力缺口分析.md》，本卷只描述结构与归属。

---

## 7. 插件管理（目录插件：发现 / 启停 / 新增 / 卸载）

`agent.plugin_manager.PluginManager` 管理 `agent/plugins/<包名>/`（`__init__.py` 或 `plugin.py` 内的 `@plugin` 类）。

| 能力 | API | 语义 |
|---|---|---|
| 发现 | `discovered()` | 扫目录得候选元数据（id / package / layer / provides / inject / tables / runnable） |
| 读期望态 | `enabled()` / `removed()` | 期望态存 `data/agent_plugin_state.json` |
| 启停 | `set_enabled(id, enable)` | **持久化期望态**；装配在启动/重建时一次性完成 → **不热插拔** |
| 新增 | `scaffold(name, provides)` | 建目录 + 模板 + **自动启用** |
| 卸载 | `remove(id_or_pkg)` | 停用 + 删目录 + `removed` 标记（**表与数据保留**） |
| 装配 | `load()` | 只加载 `enabled()` 里的插件 |

- `build_agent(..., plugin_mgr=pm)` 才装配目录插件——**显式 opt-in**，测试/无头入口默认不扫目录（避免误拉子进程）。
- **可管理性判定（关键）**：网页插件页按 `delta` 给按钮——
  `delta = "system/builtin"` ⇒ 内置插件，不给操作；目录插件（discovered）的 delta 是
  `enabled · assembled / enabled · 待重启装配 / disabled · 未装配 / 待停用` ⇒ **一律给「启用 / 停用 / 移除」按钮**。
- 因此 **`layer` 不参与可管理性判定**：目录插件无论 `layer=system` 还是 `business`，都可启停卸。这正是 §0.3「两个正交的轴」的落地方式。
- 现状：`plugins.memory`（记忆）、`plugins.react`（ReAct 多步循环）与 `plugins.windows_mcp`（工具桥）均为目录插件，默认启用（见 `data/agent_plugin_state.json`）。
- **插件包**：以 `__init__.py` 为入口的插件是**包**，可含子模块并用包内相对导入（`from .loop import …`）——加载器按包加载（`submodule_search_locations` + 命名空间父包 `agent_plugins`）。`plugins.react` 即四文件形态（`prompt/components/loop/__init__`）。

---

## 8. 事件契约（三域 + 命名）

| 域 | 定义 | 落点 | 事件（命名空间 `agent/*`） |
|---|---|---|---|
| **durable** | 须跨重启存活的事实 | 落 `agent_events` | `agent/chat/in` · `agent/chat/done` · `agent/chat/reset` · `agent/permission/decision` · `agent/memory/remembered` · `agent/memory/recall` · `agent/react/start` · `agent/react/step` · `agent/react/final` · `agent/react/exhausted` |
| **live** | 在途可观测 | 内存 emit（不落库） | `agent/llm/post-call` · `agent/tasks/post-execute` · `agent/react/step/start` · `agent/react/step/end` |
| **capability** | 把策略/约束挂在 seam 上 | seam dispatch（waterfall） | `agent/llm/pre-call`（guard·budget）· `agent/tasks/pre-execute`（guard） |

- 每个事件声明 `@mode`（emit/waterfall/parallel/serial/bail）；本卷运行时实际挂载 `waterfall`（pre-call / pre-execute）与 `emit`（post-*）。
- **payload 强制纯数据 JSON**（`strict_payload` 校验，禁传内核内部对象）。
- `events` 是**内存态**事件总线（不落盘）；durable 落盘由 `agent.sys.audit.durable(ctx, event, payload)` 完成（无 db 时**不假装**，直接跳过）。

---

## 9. 离线夹具与可自举（独立性的硬证明）

### 9.1 离线确定性

- `FixtureLlm` 按 system prompt 里的**标记路由**到 canned 响应，保证「同一输入 → 同一输出」：
  - `<!--chat:plan-->` → 规划 canned（按关键词路由）；
  - `<!--chat:answer-->` → 解说 canned；
  - `<!--memory:extract-->` → 记忆抽取 canned（`{"facts": [...], ...}`）；
  - `<!--react:turn-->` → ReAct 脚本化：按「请求关键词 + 已完成轮数」给出确定性 `tool_calls` 或最终答复；
  - `<!--react:final-->` → ReAct 触顶收尾 canned。
- 全测试**不触网、不触任何外部进程**。
- 注意：路由**只拼接 `str` 内容**——ReAct 的观察回灌消息 `content` 是 dict，不参与关键词/标记匹配。

### 9.2 入口

```bash
python -m agent.run.chat                    # 网页 harness（默认 deepseek，127.0.0.1:8720）
python -m agent.run.chat --fixture          # 离线夹具（不动真 API）
python -m agent.run.chat --once "你好"       # 无头跑一次后退出并打印
```

- 网页 harness 视图：**会话 / 事件 / 权限 / 插件 / 记忆 / 工具**；改插件（启用/停用/新增/移除）后**即时重建容器**生效。
- 无头 `--once` 走同一 `ChatDriver`，返回退出码（ok=0）。

---

## 10. 目录布局（具体到文件）

```
agent/
├─ api.py                       # 唯一公共出口：Context/plugin/Container/EventBus/errors/LlmProvider/…
├─ core/                        # 内核实现（container / context / events / errors / contract）
├─ sys/                         # 系统层插件（events_plugin / loop / llm / tasks / storage / budget / guard / chat
│                               #              / audit / mcp_client / plugin_manager）
├─ plugins/                     # 目录插件（来源 = 目录 ⇒ 可启停卸）
│  ├─ memory/                   #   plugins.memory —— 记忆子系统（layer=system，默认启用）
│  ├─ react/                    #   plugins.react —— ReAct 多步循环（layer=system，默认启用）
│  │                            #     └ prompt.py / components.py / loop.py / __init__.py（插件包）
│  └─ windows_mcp/              #   plugins.windows_mcp —— windows-mcp 工具桥（layer=business，默认启用）
├─ fixtures/llm.py              # FixtureLlm（离线确定性 canned 路由）
├─ run/chat.py                  # 入口：网页 harness + 无头 --once
├─ profiles.py                  # BASE_AGENT_CONFIG / SYS_PLUGINS / BUSINESS_PLUGINS / build_agent
├─ tables.py                    # agent_* 表 DDL（MIGRATIONS）
└─ tests/                       # 离线确定性测试（test_kernel / test_sys / test_memory / test_react /
                                #              test_plugin_manager / test_chat_* / test_guard / test_mcp_client /
                                #              test_tool_call / test_profiles …）
```

---

## 11. 工具桥：MCP（外部能力的现实接入）

- `agent.core.mcp_client.McpStdioClient`：通用 MCP **stdio** 客户端，零三方依赖（原在
  `agent.sys`，解耦审计后下沉内核——它只服务业务插件，放 sys 属"共享库放错层"；
  插件经 `agent.api` 出口引用）。
  （`start()` / `list_tools()` / `call_tool(name, args)` / `close()`）。
- `plugins.windows_mcp`：把 windows-mcp-server 的工具**自动注册进 agent 工具目录**
  - 工具 → 任务名 `mcp.windows.<tool>`，`owner="plugins.windows_mcp"`，输入 schema 由 MCP `inputSchema` 映射为 tasks 类型；
  - 生命周期对称：`apply` 起子进程 → `ctx.effect` 关子进程；
  - 诚实边界：未配置命令 → 装配期记录错误、不假挂；启动失败 → 报明原因，不拖垮装配；
  - 可管理：作为目录插件，插件页可停用/启用/卸载；`ctx.state` 暴露 `windows_mcp_tools` / `windows_mcp_error`（`status()` 供插件页）。
- **安全**：这些工具是**高危**（任意 PowerShell / 注册表 / 文件 / 进程 / 键鼠）。工具调用仍过 guard 矩阵（默认 open），建议在 `config["agent"]["permissions"]["task_deny"]` 按需收紧 `mcp.windows.PowerShell` / `Registry` / `Process` 等。
- 形态启示：**任何外部系统接入都收敛为「一个目录插件 + 一组 tasks」**——新增能力 = 挂一个插件，不改内核。

---

## 12. 里程碑与现状

| # | 内容 | 状态 |
|---|---|---|
| A0 | 内核 `agent.api`（Ctx/Plugin/Container/EventBus/effects/errors）+ 装配硬化 | ✅ |
| A1 | `agent.llm` 接缝（Fixture/DeepSeek/占位）+ `agent.budget` 护栏 | ✅ |
| A2 | `agent.storage`（`agent.db` + 迁移）+ `agent.events` 三域 + `agent.tasks` | ✅ |
| A2·G | `agent.guard` 权限网关 + 双 profile（offline / tight） | ✅ |
| A3 | `agent.loop` 任务驱动 + `agent.chat` 会话驱动 + 网页 harness | ✅ |
| A4 | `agent.sys.plugin_manager` 目录插件管理（发现/启停/新增/卸载） | ✅ |
| A5 | 工具调用（tool_call）+ MCP 工具桥（`plugins.windows_mcp`） | ✅ |
| A6 | 记忆子系统（`plugins.memory` 目录插件：提取 + 打分衰减 + recall/context 注入） | ✅ 主链路；图谱能力见待办 |
| A7 | **ReAct 多步循环**（`plugins.react`：Think→Act+Observe→收敛 + 三把闸 + 四组件接缝；`chat` 软委托） | ✅ |
| — | 离线确定性测试 | **82 项全绿** |

> 原则：全程不依赖任何外部进程；每段可独立回滚；任一步 `tests/` 全绿。

---

## 13. 契约冻结区（agent 本体开发护栏）

| # | 契约 | 定义 | 状态 |
|---|---|---|---|
| AC-1 | import 边界 | agent 本体只 `from agent.api import`；不得 `import engine.*` / 大模型 SDK（llm provider 单点 HTTP 除外） | ✅ |
| AC-2 | 键命名 | 系统键 `loop/llm/tasks/events/db/migrations/budget/guard/react`；业务键 `chat` / `memory` / `windows_mcp` | ✅ |
| AC-3 | 表命名 | 一律 `agent_*`、独立 `agent.db` | ✅ |
| AC-4 | 事件模式 | 事件声明 `@mode`；三域命名 `agent/*`；payload 纯数据 | ✅ |
| AC-5 | honest 边界 | 无 key / 缺数据 / 缺失能力 → `CapabilityNotImplemented`，不虚标、不静默 | ✅ |
| AC-6 | 驱动可换 | 消费方只依赖 `AgentDriver` 接口，不依赖具体 driver 实现 | ✅ |
| AC-7 | 可自举 | `python -m agent.run.chat --fixture` 离线跑通对话闭环 | ✅ |
| AC-8 | 来源/层级正交 | 目录插件的可管理性由**来源**判定，与 `layer` 无关（`layer` 仅为语义标签） | ✅ |
| AC-9 | 接缝加法式演进 | 扩展接缝（如 `LlmProvider.tools`）只做**加法**：缺省参数 ⇒ 既有调用方行为不变；不支持者诚实报 `CapabilityNotImplemented`，不用文本约定假装 | ✅ |
| AC-10 | 无特权通道 | 多步循环（`plugins.react`）不新建模型/工具通道：模型一律 `ctx.llm`、工具一律 `ctx.tasks.invoke` ⇒ guard/budget/schema/审计自动继承 | ✅ |

---

## 14. 待完善（OPEN）

| # | 待定 | 现状 / 方向 |
|---|---|---|
| OPEN-1 | 记忆图谱能力（实体/关系落库、context 邻边行、ontology） | **未实现**；`agent_entities` / `agent_memory_edges` 当前无写入。详见《记忆插件-能力缺口分析.md》 |
| OPEN-2 | 被动摄取（订阅 durable 事件自动提取） | **未实现**；当前仅 chat 同步 push + 手动 `add` |
| OPEN-3 | 语义召回（向量 / MMR / RRF） | **未实现**；当前为词面 + 衰减排序，seam 已留（`MemorySearch`） |
| OPEN-4 | 记忆 kind 覆盖（decision / observation） | 提取侧仅产 fact / intention（详见分析文档） |
| OPEN-5 | 热插拔（启停不重建容器） | 现为「重启/重建后生效」这一诚实边界 |
| OPEN-6 | 会话持久化粒度 | `session_id` 由入口进程生成；跨进程/跨重启的会话管理未做 |
| OPEN-7 | 外部系统桥（引擎等） | MCP 已通；其它系统接入按 §11 模式（目录插件 + tasks）逐个添加 |
| OPEN-8 | 多 provider / 模型路由 | 现为单 provider 装配；路由与降级未做 |
| OPEN-9 | 审批与权限模式（`plan` / `auto` / `bypass`） | **接口已预留未实现**（`permission_mode` 状态位 / `visible_tools` 裁剪点 / `Tool.risk`）；设计见《审批插件-设计方案.md》 |
| OPEN-10 | 中途回话通道（SSE / WebSocket / 取消） | **未实现**；是 approval 问人、流式输出、取消共用的基础设施 |
| OPEN-11 | `guard` 表达力（通配符 / 参数级规则 / 读·写分级） | 现为精确名匹配、只看任务名；读写分级已由 `Tool.risk` 补上元数据，判定未接 |
| OPEN-12 | 并行工具编排 | `EventBus.parallel` 已实现但**无消费方**；`plugins.react` 的 `DefaultActor` 目前顺序执行 |

---

## 15. 参考

- DeepSeek-Harness（本地 clone）：`README.md`、`docs/architecture.md`、`docs/cordis-primer.md`、`docs/subsystems/core.md`、`docs/capability-seams.md` —— 六根脊梁 / 三域事件 / step-turn / driver-as-plugin / capability seam 的原文出处
- 《agent记忆架构-方案.md》 —— 记忆子系统设计与落点
- 《记忆插件-能力缺口分析.md》 —— 记忆子系统现状与待办
- `doc/引擎统一管理-改造意见.md` —— 目录插件管理的引擎式语义（desired/actual/delta）；**历史归档，不作工作参考**
- `README.md` —— 运行入口与现状摘要
