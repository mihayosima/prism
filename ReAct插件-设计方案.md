# ReAct 插件 · 设计方案

| 文档属性 | 内容 |
|---|---|
| 文档状态 | **v1.1 已实施（R1–R4 全部落地）** —— 代码见 `agent/plugins/react/`；用例见 `agent/tests/test_react.py`（13 项，全绿） |
| 范围 | ① `plugins.react` 多步工具循环（**已实施**）；② 权限模式与 approval 的**接口预留**（已落位置，行为未变） |
| 依据 | 对当前代码的逐项核实 + `loong-agent` 0.0.2 与 `LoongFlow`（`framework/react`）两款开源实现的对照（见 §11） |
| 关联文档 | 《agent内部架构-开发文档.md》（架构）·《agent通用能力-完善路线.md》（A1/A3/A4 的落地）·《记忆插件-能力缺口分析.md》 |
| 编制日期 | 2026-09-24（v1.1 同日实施） |

---

## 0. 结论先行

1. **形态**：目录插件 `plugins.react`（`layer="system"`、`provides=["react"]`），默认启用、可经插件页启停卸。
2. **循环形状**：采用 **loong-agent 流派** —— 每轮 `Think`；模型**不再返回工具调用**即为最终答案；有则 `Act + Observe`，把工具结果回灌后进入下一轮。相比 LoongFlow 的"final-answer-as-tool"，它与 function-calling 天然契合，且更贴我们的接缝模型。
3. **补上 loong-agent 缺的三把闸**：`max_steps` · 整任务 `deadline` · **token 预算（天然生效——每步都走 `ctx.llm`，guard/budget 已在 seam 上）**。
4. **四组件化**：`Reasoner / Actor / Observer / Finalizer` 定义为 **可选注入的接缝服务**，缺省用内置实现；这是"可换"的落点，也是将来放"人在环中"的位置。
5. **工具复用 `ctx.tasks`**，不另建工具系统 ⇒ `guard` 裁决、schema 校验、owner 审计**全部自动继承**。
6. **`chat` 通过 `ctx.has("react")` 委托**：契约一行不改；插件不在时自动回退现有单步路径。
7. **权限模式本次只做接口预留**：`permission_mode` 状态位、`catalog(include=)` 裁剪点、`Tool.risk` 字段。**approval 插件与"中途回话通道"不在本次范围。**

---

## 1. 目标与非目标

### 1.1 本次要做（IN）

| # | 项 | 验收 |
|---|---|---|
| 1 | 多步循环：`Think → (有工具) Act+Observe → 再 Think` | 一个需要"两步工具"的任务能跑通 |
| 2 | 三把闸：`max_steps` / `deadline` / token 预算 | 触顶时**优雅停止 + 如实说明**，不崩、不静默 |
| 3 | 工具失败即观察（异常不抛出，转 error 观察回灌） | 模型能据此改参数重试 |
| 4 | 上下文治理：工具结果截断 + 历史裁剪 | 大结果不会撑爆上下文 |
| 5 | 事件与审计：步骤级 durable + 实时 live | 可从 `agent_events` 重建一次 run |
| 6 | `chat` 委托 + 回退 | 启用则多步、停用则回退单步 |
| 7 | 权限模式**接口预留** | 状态位/裁剪点/`risk` 字段就位，行为不变 |
| 8 | 离线确定性测试 | 全绿，且不触网 |

### 1.2 本次不做（OUT）

- ❌ `plugins.approval`（问人）与**中途回话通道**（SSE / WebSocket）→ 下一批，与"流式输出 / 取消"同批做
- ❌ 工具级细粒度与参数级规则（如"只准写 `./data`"）→ 属 `guard` 的能力补强，另列
- ❌ 子代理（subagents）、Skills、RAG、并行工具编排
- ❌ 记忆改造（用户已明确暂缓）

---

## 2. 接线契约（与现有架构的边界）

```python
@plugin
class React:
    name = "plugins.react"
    version = "0.1.0"
    layer = "system"                 # 系统组件（语义层），来源=目录（可启停卸）
    provides = ["react"]             # ctx.react
    inject = ["llm", "tasks", "events", "db"]
    tables = ()                      # 不自带表（转写复用 agent_messages / agent_events）
    description = "ReAct 多步工具循环（Think → Act+Observe → 收敛）"
```

**红线（沿用本仓库纪律）**：

1. **不新建模型通道**：每步必须走 `ctx.llm.complete(...)` ⇒ guard/budget/审计照生效，**记忆不是特权通道，ReAct 也不是**。
2. **不绕过任务注册表**：工具调用必须走 `ctx.tasks.invoke(...)` ⇒ guard 裁决 + schema 校验 + owner 审计自动继承。
3. **不反向依赖内部**：`react` 只依赖 `ctx.*` 服务键；`chat` 只通过 `ctx.has("react")` 感知它存在，**不 import** 其实现。
4. **诚实边界**：provider 不支持 native 工具调用 → `CapabilityNotImplemented`（见 §3.1），**不偷偷用 JSON 兜底**。

---

## 3. 循环设计

### 3.1 前置决策：模型如何表达"我要调工具"

| | 方案 A · JSON 约定 | **方案 B · native tools（推荐）** |
|---|---|---|
| 做法 | 提示词要求输出 `{"thought", "tool_calls", "answer"}`，走 `schema` | `LlmProvider` 加法式扩展：`complete(ctx, messages, *, schema=None, tools=None)`，返回可含 `tool_calls` |
| 接缝改动 | 零 | **加法**（现有调用方不传 `tools`，行为不变） |
| 可靠性 | 模型遵循度低；参数无 provider 侧保证 | 标准做法；参数由 provider 保证合法 JSON |
| 离线可测 | 容易 | 同样容易（Fixture 按标记回脚本化 `tool_calls`） |

**推荐 B**，理由：ReAct 的核心就是**可靠的工具选择**。若某 provider 不支持 native 工具调用，则该 provider **不支持 react**，如实报 `CapabilityNotImplemented`（符合"不虚标"纪律）；方案 A 留作将来对弱 provider 的降级路径。

> ⚠️ 这是本方案**第一个需你确认的决策点**（它触及 `LlmProvider` 契约）。

### 3.2 主循环（伪码）

```
async def run(ctx, prompt, *, session_id="default") -> ReactResult:
    await durable("agent/react/start", {prompt, session_id})
    messages = [system(系统提示 + 工具目录) , user(prompt)]
    steps, tin, tout = 0, 0, 0
    deadline = now() + cfg.deadline_s
    rejected = defaultdict(int)                    # 工具被拒计数（防死循环）

    while steps < cfg.max_steps:
        if now() > deadline:  → 触顶(超时)；break
        steps += 1
        emit("agent/react/step/start", {step: steps})
        try:
            r = await ctx.llm.complete(ctx, messages, tools=ctx.tasks.catalog(...), caller="plugins.react")
        except CapabilityNotImplemented as exc:     # guard/budget 拒绝
            → 触顶(预算/权限)；break
        tin += r.tokens_in; tout += r.tokens_out
        messages.append(assistant(r.data))

        calls = r.data.get("tool_calls") or []
        if not calls:                               # ← 收敛：不再需要工具
            final = r.data.get("text") or "（未给出回答）"
            await durable("agent/react/final", {steps, tokens})
            return ReactResult(ok=True, text=final, steps=steps, tokens_in=tin, tokens_out=tout)

        results = []
        for call in calls:
            results.append(await self._act_one(ctx, call, rejected))
        messages.append(user(tool_results=results))  # ← 观察回灌
        emit("agent/react/step/end", {step: steps, tools: [c.name for c in calls]})
    else:
        → 触顶(max_steps)

    # 触顶统一收尾：模型再答一次（不给工具），并如实标注
    final = await self._finalize_on_exceed(ctx, messages, reason)
    await durable("agent/react/exhausted", {steps, reason, tokens})
    return ReactResult(ok=True, exhausted=True, text=final, ...)   # ok=True 但 exhausted 标真
```

### 3.3 三把闸

| 闸 | 位置 | 行为 |
|---|---|---|
| **`max_steps`** | 循环条件（配置，默认 8） | 触顶 → §3.2 收尾 |
| **`deadline_s`** | 每轮开头检查（配置，默认 120s） | 触顶 → 收尾 |
| **token 预算** | **不用写**：每步 `ctx.llm` → `agent/llm/pre-call` 上 guard/budget 已生效 | 被拒 → `CapabilityNotImplemented` → 转"触顶"收尾（**不是崩**） |

### 3.4 失败即观察（关键）

```
async def _act_one(ctx, call, rejected) -> ToolResult:
    try:
        out = await ctx.tasks.invoke(ctx, call.name, caller="plugins.react", **call.args)
        return ToolResult(name=call.name, content=truncate(out), is_error=False)
    except CapabilityNotImplemented as exc:        # guard/预算拒绝
        rejected[call.name] += 1
        if rejected[call.name] > cfg.max_rejects:  # 防"反复试同一被拒工具"
            raise StopReAct("工具反复被拒，终止")    # → 触顶收尾
        return ToolResult(name=call.name, content=f"被拒绝：{exc}", is_error=True)
    except AgentError as exc:                       # 参数错 / 工具不存在
        return ToolResult(name=call.name, content=f"出错：{exc}", is_error=True)
```

要点：**一律回灌给模型**（而不是抛出终止），让模型自纠；但**同一工具被拒超过 N 次即终止**，防止死循环。这是 loong-agent 的做法 + 我们补的一道保险。

---

## 4. 四组件接缝（可换）

```python
class Reasoner(Protocol):     # 决定下一步（默认：调 ctx.llm）
    async def reason(self, ctx, messages, tools) -> dict: ...
class Actor(Protocol):        # 执行工具调用（默认：逐个经 ctx.tasks.invoke）
    async def act(self, ctx, messages, calls) -> list[ToolResult]: ...
class Observer(Protocol):     # 把结果整理成回灌消息（默认：截断 + 结构化）
    async def observe(self, ctx, results) -> dict: ...
class Finalizer(Protocol):    # 收敛/触顶时产出最终答复（默认：再调一次模型，不给工具）
    async def finalize(self, ctx, messages, reason) -> str: ...
```

**缺省与替换规则**（与 `llm` 的 provider 选择同构）：

```
若 ctx.has("react.reasoner") → 用注入的；否则用内置 DefaultReasoner
（actor / observer / finalizer 同理）
```

好处：将来 `plugins.approval` 只需替换 `Actor`（在 `act` 里插"问人"），**不必改循环本身**；并行工具编排也只需换 `Actor`。

---

## 5. 消息与上下文治理

- **消息形状**：`{role: "system"|"assistant"|"user", content: str, tool_calls?: [...], tool_results?: [...]}`（纯 dict，可 JSON 化 —— 与事件面 payload 同纪律）。
- **工具结果截断**：默认 2000 字符（配置 `tool_result_max_chars`），超出截断并附 `…(truncated)`。**这是本次必做**，否则多步必然撑爆上下文。
- **历史裁剪**：保留 首条 system + 最近 `history_keep` 条（默认 20）；中间的旧工具结果可折叠为一行摘要（先做简单裁剪，摘要压缩留后续）。
- **与短期记忆的关系**：本轮 transcript 写入 `agent_messages`（session_id 维度），与现有一致；`react` **不读**记忆插件（记忆暂缓）。

---

## 6. 事件与审计

| 域 | 事件 | 时机 |
|---|---|---|
| **durable** | `agent/react/start` / `agent/react/step` / `agent/react/final` / `agent/react/exhausted` | 落 `agent_events`（审计脊柱） |
| **live** | `agent/react/step/start` / `agent/react/step/end` | 内存 emit（前端进度） |
| **capability** | **不新增** —— 复用 `agent/llm/pre-call` 与 `agent/tasks/pre-execute` | guard/budget 挂点不变 |

> 只读侧：`agent/react/step` 含 `{step, tools, is_error, tokens}`，因此**一次 run 的完整轨迹可由 `agent_events` 重建**。

---

## 7. 权限模式与 approval · 接口预留（**本次不实现**）

设计结论（**完整设计已单独成篇：《审批插件-设计方案.md》**）：`plan / auto / bypass` 是**两个正交开关**的组合 —— *工具可见集* × *approval 打扰策略*；而 `guard` 硬规则在**任何模式下都生效**。

本次**只落三处"位置"**，行为保持不变：

| # | 预留项 | 落点 | 本次行为 |
|---|---|---|---|
| 1 | 会话级模式状态 | `ctx.state["permission_mode"]`，取值 `default`（默认）/`plan`/`auto`/`bypass` | 只读取，不改变判定 |
| 2 | 工具可见集裁剪点 | `ctx.tasks.catalog(include=...)` —— **参数已存在**；`react` 组装工具目录处加一层 `_visible_tools(ctx)` | 该函数**恒返回全量**（占位） |
| 3 | 工具风险元数据 | `tasks.register(..., risk="read"|"write"|"danger")`，默认 `"write"`（保守），随 `catalog()` 输出 | 只落字段，**无人消费** |

**明确不做**（避免半成品契约）：

- ❌ 不改 `tasks.invoke` 的决策契约（不改 `kwargs` 回读、不加 `ask` 字段）→ 与 approval 那批一起做
- ❌ 不实现 `plan` 的硬约束兜底（可见集是软约束，硬拒绝由 approval/guard 补）
- ❌ 不做 `bypass` 的任何"跳过"逻辑

> 安全提醒（写进设计，供将来实现时遵守）：`bypass` **不得持久化**（重启必须回 `default`）；`bypass` 下 **guard 的 deny 仍应生效**；`bypass` 下必须**照记 durable 审计**。

---

## 8. 配置项

```toml
[agent.react]              # 即 config["agent"]["react"]
max_steps = 8              # 循环上限
deadline_s = 120           # 整任务时延上限
tool_result_max_chars = 2000
history_keep = 20          # 保最近 N 条
max_rejects = 3            # 同一工具被拒多少次即终止
finalize_on_exceed = true  # 触顶时再答一次（不给工具）
```

---

## 9. 文件落点与改动清单

```
agent/plugins/react/__init__.py     # 新建：@plugin React（provides ctx.react + status()）
agent/plugins/react/loop.py         # 新建：ReactLoop（§3.2 主循环 + 三把闸 + visible_tools）
agent/plugins/react/components.py   # 新建：四组件 Protocol + 默认实现（§4）
agent/plugins/react/prompt.py       # 新建：系统提示 + 工具目录渲染 + 结果截断
agent/fixtures/llm.py               # 改：+ <!--react:turn--> / <!--react:final--> 脚本化路由
agent/sys/chat.py                   # 改：send 里 ctx.has("react") → 委托（ReactResult 适配成 ChatReply）
agent/sys/tasks.py                  # 改：register 增可选 risk 字段；catalog 输出带 risk（§7-3）
agent/sys/llm.py                    # 改：LlmProvider 加法式支持 tools / tool_calls（§3.1 方案 B）
agent/core/contract.py              # 改：LlmProvider 契约同步（同样的加法式签名）
agent/sys/plugin_manager.py         # 改：支持「插件包」入口（__init__.py 相对导入）——见 §15.3
agent/tests/test_react.py           # 新建：见 §10
data/agent_plugin_state.json        # 改：enabled 加 plugins.react
```

**不改**：`profiles.py`（目录插件无需登记）、`tables.py`（无新表）、`guard.py`、`budget.py`、记忆插件。

---

## 10. 测试清单（离线确定性，全部不触网）

| # | 用例 | 断言 |
|---|---|---|
| 1 | 单步收敛 | 模型直接答复（无工具）→ 1 步结束，`steps==1` |
| 2 | 两步工具链 | 工具 → 再推理 → 收敛，`steps==2`，轨迹落 durable |
| 3 | **工具失败自纠** | 首次参数错 → error 观察 → 模型改参数 → 成功 |
| 4 | guard 拒绝回灌 | 被拒 → error 观察；连拒超 `max_rejects` → 终止并如实说明 |
| 5 | 触顶（`max_steps`） | `exhausted=True`，文本含说明，**不抛异常** |
| 6 | 触顶（预算） | budget 拒绝 → 同样优雅收尾 |
| 7 | 结果截断 | 超长工具结果被截断且标注 |
| 8 | 事件完整性 | `agent/react/*` durable 事件齐备，可重建轨迹 |
| 9 | **chat 委托** | 启用 react → 多步；停用 → 回退单步（两条路径都测） |
| 10 | 权限预留 | `permission_mode="plan"` 时 `_visible_tools` 占位仍返回全量（**锁住"本次不改行为"**） |
| 11 | 卸载无残留 | `teardown` 后 `ctx.has("react")` 为 False |

---

## 11. 里程碑与验收

| 步 | 内容 | 验收 | 状态 |
|---|---|---|---|
| **R1** | L1 接缝（`tools`/`tool_calls`）+ Fixture 脚本化路由 | `test_react` 用例 1/2 绿 | ✅ |
| **R2** | 主循环 + 三把闸 + 失败即观察 | 用例 3/4/5/6 绿 | ✅ |
| **R3** | 上下文治理 + 事件审计 | 用例 7/8 绿 | ✅ |
| **R4** | chat 委托 + 权限预留 + 收尾 | 用例 9/10/11/12 绿；**全量 68+ 项测试仍全绿** | ✅ |

R1 依赖 §3.1 的决策点确认（已确认：**方案 B**）。

---

## 12. 待你确认（OPEN）

| # | 待定 | 结论（已拍板） |
|---|---|---|
| **OPEN-1** | 模型如何表达工具调用 | ✅ **方案 B（native tools，加法式扩展 `LlmProvider`）**；不支持者诚实报缺（不偷偷用 JSON 兜底） |
| **OPEN-2** | `plugins.react` 默认启用还是停用 | ✅ **默认启用**（写入 `data/agent_plugin_state.json`；停用即回退单步） |
| **OPEN-3** | `max_steps` 默认值 | ✅ **8** |
| **OPEN-4** | 触顶时是否再调一次模型收尾 | ✅ **是**（`finalize_on_exceed=true`），失败退回证据兜底文本 |
| **OPEN-5** | 是否顺带在 `guard` 加"通配符匹配" | ⏸ 暂不（记入《agent通用能力-完善路线.md》） |

---

## 13. 参考

- **`loong-agent` 0.0.2**（PyPI，MIT）：`loop/engine.py`（`while turn_count < max_turns` + 触顶 `is_error=True`）· `loop/turn.py`（Think → 无 tool_calls 即收敛 → Act+Observe；工具异常转 `ToolResult(is_error=True)`）· `permissions/policy.py`（allow/deny/ask + fnmatch，**ask 未落地**）· `client.py`（流式 + `interrupt()`）
- **`LoongFlow`**（`baidu-baige`）：`framework/react/README.md`（四组件 Protocol）· `react_agent.py`（**final-answer-as-tool**、finalize 判定在 observe 之前、`summarize_on_exceed`）
- 《agent内部架构-开发文档.md》§3（内核）· §4.2（tasks）· §8（事件三域）
- Claude Code 的 `default / plan / acceptEdits / bypassPermissions` 模式 —— 本方案 §7 的映射来源（按语义对齐，不照搬实现）

---

## 14. 解耦契约与可管理性（v1.1 补充，开工前确认）

### 14.1 依赖方向：单向（**已发现并修正一处双向耦合**）

```
chat ──软(ctx.has "react")──▶ react ──软(读 permission_mode)──▶ approval
                                └────▶ ctx.llm / ctx.tasks（系统服务）◀────┘
approval ──✗──▶ react            （零依赖）
```

| 关系 | 类型 | 缺省行为（对侧插件不存在时） |
|---|---|---|
| `chat` → `react` | 软：`ctx.has("react")` | 回退现有单步路径 |
| `react` → `approval` | 软：`ctx.get("permission_mode", "default")` | 恒为 `default` ⇒ 全量工具、不裁剪 |
| `approval` → `react` | **零依赖** | 不受影响，独立可用 |

**修正的问题**：初版 §7 里，`plan` 模式会造成 **react ↔ approval 双向耦合**（react 要读 mode；approval 要 react 注册 `submit_plan`）。解法是把每个关注点归到单一所有者：

| 关注点 | 归属 | 说明 |
|---|---|---|
| 持有 `mode` 状态 | **approval** | `ctx.state["permission_mode"]` |
| 读 mode 裁剪工具可见集 | **react** | 每次组装工具目录时读一次 |
| 注册 `submit_plan` | **react**（`risk="read"`） | 它是循环的收敛工具；approval **完全不认识它** |
| "计划待批准"的问人 | **approval 的普通 `ask` 路径** | `plan` 模式下 `submit_plan` 命中 ask → 复用同一套问人逻辑 |
| 批准后切模式（`plan`→`auto`） | **approval 自己的策略** | 答"是"时顺手改 mode |
| 拒绝后如何继续 | **react 的普通错误回灌** | 被拒 = error 观察，模型重新规划，**无需特判** |

⇒ `plan` 完全由「mode + `Tool.risk` + ask」三个**通用概念**拼出，**approval 不需要任何 react 知识**。这是本次解耦的关键。

### 14.2 可管理性核对（与 `plugins.memory` 同规格）

| 项 | 要求 | 状态 |
|---|---|---|
| 位置 | `agent/plugins/<name>/__init__.py` | 设计已定 |
| 不进 `SYS_PLUGINS` | 由 `PluginManager` 按期望态装配 | 设计已定 |
| `layer` | 仅语义标签（`system`），**不闸可管理性**（AC-8） | 已确认 |
| 默认启停 | 写入 `data/agent_plugin_state.json` | 待实现 |
| 卸载残留 | **两插件都不自建表** ⇒ 卸载零残留 | 设计已定 |
| 停用降级 | chat 回退单步；react 读到 `default` | 设计已定 |
| **插件页可观测** | 两者都实现 `status(ctx)`：react → 最近 run 的步数/工具；approval → 当前 mode + provider | **本版新增要求** |

### 14.3 启停组合矩阵（四种组合都不得报错）

| react | approval | 行为 |
|---|---|---|
| 关 | 关 | 现状单步、无审批（等价于今天） |
| 开 | 关 | 多步循环、全量工具、不问人 |
| 关 | 开 | 单步，但 `ask` 名单生效 |
| 开 | 开 | 多步 + 审批 + 权限模式 |

→ 测试清单补充一条"组合矩阵"用例（§10 用例 12）。

---

## 15. 实施记录（v1.1 · 2026-09-24）

### 15.1 落地清单

| 文件 | 状态 | 说明 |
|---|---|---|
| `agent/plugins/react/prompt.py` | 新增 | 系统提示（含两标记）、工具目录渲染、环境事实块、`truncate` |
| `agent/plugins/react/components.py` | 新增 | `ToolCall/ToolResult/ReasonOutput` + 四组件 Protocol + 默认实现 |
| `agent/plugins/react/loop.py` | 新增 | `ReactLoop`（主循环 + 三把闸 + 历史裁剪 + `visible_tools`）· `ReactConfig` · `ReactResult` |
| `agent/plugins/react/__init__.py` | 新增 | `@plugin React`（`layer=system`/`provides=["react"]`/`inject=[llm,tasks,events,db]`/`tables=()`）+ `status()` |
| `agent/sys/llm.py` · `agent/core/contract.py` | 改 | `complete(..., tools=None)` 加法式；`_wire_messages/_wire_tools/_wire_call` 做 OpenAI 兼容转换；`DeepSeekLlm` 解析 `tool_calls` |
| `agent/fixtures/llm.py` | 改 | `_route` 增 react 两标记；`_react_reply` 按"关键词 + 已完成轮数"给出确定性 tool_calls / 最终答复 |
| `agent/sys/tasks.py` | 改 | `register(..., risk="write")` + `catalog()` 输出 `risk`；新增 `RISKS`（**只落字段，无判定行为**） |
| `agent/sys/chat.py` | 改 | `send()` 中 `ctx.has("react")` → 委托；外部纪律（短期记忆 / `chat/done` / memory / ChatReply）保持一致 |
| `agent/sys/plugin_manager.py` | 改 | 插件包入口支持（见 §15.3） |
| `agent/tests/test_react.py` | 新增 | 13 项（§10 用例 1–12 + wire 转换断言） |
| `data/agent_plugin_state.json` | 改 | `enabled` 增 `plugins.react` |

### 15.2 与设计的差异（三处，均为落地必需）

1. **"连拒终止"归循环、不归 `Actor`**：`DefaultActor` 只把拒绝转成 `denied=True` 的观察（保持无状态、可替换）；计数与终止由 `ReactLoop` 持有（跨调用状态归循环）。好处：将来 approval 替换 `Actor` 时，防死循环这道保险仍在。
2. **触顶收尾追加** `{"role":"system","content":FINAL_SYSTEM}`（而非改写原 system 消息）：不改循环持有的消息全集；Fixture 只在 system 消息里找标记，两标记在列表中共存、互不干扰。
3. **`_trim` 保 system + 首个用户请求 + 最近 N 条**（而非简单截尾）：避免多步之后把"用户到底要什么"裁掉。

### 15.3 副作用：插件加载器支持「插件包」

原 `_load_module` 以合成模块名 `agent_plugins.<name>` 执行 `__init__.py`，**没有父包**，因此本设计的四文件布局会在 `from .loop import …` 处 ImportError。已把 `__init__.py` 入口按**包**加载（`submodule_search_locations`）并登记命名空间父包 `agent_plugins`，使目录插件可多文件、可用包内相对导入。新增用例 `test_plugin_manager.py::test_package_plugin_with_relative_import_loads` 锁定该能力（单文件 `plugin.py` 形态不受影响）。

### 15.4 验收

- `pytest -q` → **82 passed**（原 68 + 新增 14：`test_react.py` 13 项 + `test_plugin_manager.py` 的"插件包"1 项），全离线、不触网。
- 冒烟（走真实 `agent/plugins` 目录、模拟网页启动）：装配清单含 `plugins.react`；`react.run` 两步链路 → `steps=2, tools=['test.echo']`；`chat.send` 走 `intent=react`；durable `agent/react/*` 6 条；`teardown` 后 `has("react")=False`；`tables=()`。

### 15.5 明确未做（保持范围）

- ❌ `plugins.approval` 与"中途回话通道"（SSE / WebSocket）→ 下一批，见《审批插件-设计方案.md》
- ❌ `guard` 通配符 / 参数级规则；`plan` 的硬约束兜底
- ❌ 并行工具编排（`EventBus.parallel` 仍是闲置原语；`DefaultActor` 目前顺序执行）
