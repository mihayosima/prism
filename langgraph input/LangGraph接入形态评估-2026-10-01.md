# LangGraph 接入形态评估

编制时间：2026-10-01 15:42（GMT+8）
版本：v1.0

## 更新记录

| 时间 | 版本 | 变更摘要 |
|---|---|---|
| 2026-10-01 15:42 | v1.0 | 基于 `agent/` 真实代码核查，评估 LangGraph 作为插件接入与外部服务接入两种形态，给出决策树与最小验证路径。 |

---

## 1. 背景与问题

本项目（FastAPI + 插件化内核的 agent harness，仓库 `mihayosima/prism`）当前是一个**线性单循环 ReAct agent**：

- 主循环为 `agent/plugins/react/loop.py` 的 `ReactLoop`，核心是 `while steps < cfg.max_steps`（`:495`），纯直线 think→act→observe，无分支、无子代理、无并行。
- 全仓库**零 `langgraph`/`langchain` 依赖**，设计上刻意"零外部引擎、零 Node、零三方重依赖"（`agent/api.py:3`、`agent/__init__.py:3`）。

围绕"断点续传 / 回滚"需求，连续提出三个问题，本档汇总其结论：

1. **要不要引入新架构（如 LangGraph）？** —— 不建议为"断点续传+回滚"整体换引擎；该能力只需在原接缝上加 checkpointer，不必引入图框架。
2. **LangGraph 作为插件接入如何？** —— 接缝现成、架构上最顺，但只解决隔离，不减免"把定制控制流搬进 LangGraph"的硬活，且受 D-3 单 provider 约束（互斥切换）。
3. **落盘成文档** —— 即本档。

---

## 2. 核心结论（一句话）

> **LangGraph 作为插件接入是本项目最顺的接法，接缝已经现成；但"插件"只解决隔离/打包，不减免"把定制控制流搬进 LangGraph"的硬活，且受 D-3 单 provider 约束（必须 A/B 互斥，不能共存）。**
> 若目标仅为断点续传+回滚，原生扩展 `ReactLoop` 加 checkpointer 即够，根本不需要 LangGraph；若未来出现图形态工作流，则以**外部服务 via MCP** 接入最干净。

---

## 3. 现状事实核查（已逐条核实 file:line）

| 事实 | 代码证据 |
|---|---|
| 引擎接缝已是服务注入点 | `plugins/react/__init__.py:56` `provides=[Services.REACT]`；`sys/chat.py:237` 仅调 `ctx.react.run(...)`，不认具体实现 |
| 消费方契约已固化 | `core/contract.py:40` 的 `ReactResultLike`，替代引擎返回同结构即可，chat 零改动 |
| 项目本就设计成"可换" | `react/__init__.py:15-16, 81-85` 的"四组件可换"（`react.reasoner/actor/observer/finalizer` 经 `ctx.get`/`ctx.provide` 注入） |
| 目录插件可绕开内核清单 | `profiles.py:111` 目录插件经 `plugin_dir` opt-in，**不用改 `SYS_PLUGINS`/`BUSINESS_PLUGINS`**（`:82-83`） |
| D-3 单 provider 互斥 | `core/container.py:222-226`：同一 service 键只能由单一插件提供，两个 provider 装配期直接 `AgentError` |
| 零重依赖纪律 | `agent/api.py:3` 明文"零外部引擎、零三方重依赖"；耦合守卫（K0）锁进测试 |
| 工具税优化位置 | `loop.py:402` `_tools_for_step`/`_card`、域裁剪 `allowed_domains`/`clip_by_domain`、`render_tools`/`render_domains` 系统提示、`wire_tools`（`core/request_cost`） |
| 审计/遥测脊柱 | `durable` 落 `agent_events`（append-only WAL）：`REACT_START/STEP/FINAL/EXHAUSTED/ARCHIVED`（`core/events_catalog.py:52-58`） |
| 续接雏形（非断点续传） | `loop.py::run(resume=...)`（`:388`）+ `sys/chat.py::is_resume_intent`（`:147`）关键词触发，把上轮链塞回窗口——是上下文连续性，**非**崩溃恢复、**非**回滚 |
| 持久化缺口 | `TaskContext` 显式不持久化（`loop.py:100-125`）；一轮 run 在单次请求内跑完（`run/chat.py::_worker:509`）；链存档 `_archive_chain` 只在收尾写（`:1186-1211`，调用 `:565/:653`）；`work_memory` 默认 in-memory（`backend.py:52`，Redis 降级） |
| 取消无半成品 | `active_runs` 内存 dict（`run/chat.py:529`）；`/api/chat/cancel`（`:549`）仅 `task.cancel()` |

---

## 4. 方案 A：进程内插件接入（LangGraph as Plugin）

### 4.1 可行性（接缝现成）
- 新插件 `plugins/react_langgraph` 只需 `provides=[Services.REACT]` 并实现一个 `run(ctx, message, session_id=...)` 返回 `ReactResultLike`，即可替换当前 `plugins/react`。
- 因是**目录插件**，经 `plugin_dir` 加载，**无需改 `SYS_PLUGINS`/`BUSINESS_PLUGINS` 内核清单**，绕开 SSOT 痛点。
- LangGraph 仅存在于该插件内 `from agent.api import ...`，core/sys 零触碰 → **K0 守卫不破**，`api.py:3` 的"零重依赖"在代码层保住。

### 4.2 必须过的硬坎（与"打包"无关）
1. **D-3 单 provider 互斥**：不能同时让 `plugins/react` 与 `plugins/react_langgraph` 提供 `Services.REACT`。必须在 `data/agent_plugin_state.json` 里**关掉一个、开另一个**做互斥 A/B 切换（无热并存）。
2. **控制流要在 LangGraph 里重写一遍**：`ReactLoop` 的工具税优化、治理壳 `GovernorLike`、预算遥测 `request_cost`/`wire_tools`、工作记忆 `wm.checkpoint`、审计 `durable`——LangGraph 不白给。要么在其 node/state/tool-call 模型里重写同样逻辑（保留优化），要么接受默认（失去优化）。
3. **工具税 vs LangGraph 默认 schema**：`create_react_agent`/`@tool` 发全量工具 schema，会让你压下来的 native tools 体积（单轮约 90% token）回弹；要复用 `visible_tools` 必须自己包一层。
4. **双引擎维护**：两套 resume/rollback/治理语义要同步，直到决定切默认。

### 4.3 最小验证路径（零 LangGraph 依赖）
1. 先写**空壳** `plugins/react_langgraph`：`provides=[Services.REACT]`，`run(...)` 返回 `ReactResultLike` 占位；在 `agent_plugin_state.json` 关 `plugins.react` 开它 → 跑通 `sys/chat.py:237` 调用契约，证明 `Services.REACT` 可替换。
2. 逐步把 think→act→observe 搬成 LangGraph node，复用 `visible_tools` 做工具裁剪。
3. 用 LangGraph checkpointer 实现每步快照 + 时间旅行（命中断点续传/回滚需求）。

---

## 5. 方案 B：外部服务 via MCP（更优隔离）

### 5.1 动机
LangGraph 拉 `langchain-core` 一族重依赖，即使放目录插件也**污染单进程运行时体积**（代码层隔离得干净，运行时没隔离）。本项目本就熟悉微服务形态（G 端"AI+水利" B3-B8 微服务架构），故更优解是**进程外**。

### 5.2 落点
- 把 LangGraph agent 做成**独立服务/进程**，经现有 **MCP 桥**（`agent.api` 的 `McpStdioClient` / `build_task_schema`）暴露给内核作为一组工具。
- 内核零重依赖、零红线破、运行时不膨胀；LangGraph 的 **checkpointer 直接白送断点续传 + 时间旅行回滚**——恰好命中上一轮需求。

### 5.3 代价
- 引入分布式边界：网络延迟、MCP 桥健壮性（已有 `mcp-stdio-bridge-hardening` 相关技能/守卫可复用）。
- 工具调用从进程内函数调用变为跨进程 RPC，需重新评估预算遥测口径。

---

## 6. 决策树（落到真实需求）

| 你的目标 | 推荐方案 | 理由 |
|---|---|---|
| 只拿**断点续传+回滚**、保持单循环 | **原生扩展**（`ReactLoop` 加 checkpointer） | 根本不需要 LangGraph；复用 `work_memory`/`durable` 接缝，保住全部优化 |
| 想**安全试 LangGraph** | 方案 A（进程内插件，A/B 互斥切换） | 接缝现成、core 零改动；先验证契约再逐步搬控制流 |
| 未来出现**图形态工作流**（分支/多 agent/HITL 闸门） | 方案 B（外部 LangGraph 服务 via MCP） | 隔离最干净，且 checkpointer 白送持久化 |

---

## 7. 推荐路径与优先级

1. **前置**（与《生产就绪度评估》P0/P1 重叠）：持久化后端（Redis 或 `agent.db` 新表 `run_snapshots`）+ **多会话/租户模型**（现全局单 `session_id`，见 P0-1）。没有 owner，"恢复我的中断 run"无从归属。
2. **断点续传**：每步写全量快照（链 + head + step + tokens + loaded + status + lease）；`run(resume=)` 由 bool 升级为接受 run_id，上一 run 仍 `running` 且 lease 过期→自动续。
3. **回滚**：版本化快照表 `state_versions(session, version, kind, payload)`（append-only）；新增 `/api/session/rollback`；工具补偿账本 `(tool, args, result, inverse_hint)`。
4. **是否引入 LangGraph**：仅当产品演进到图形态工作流时，按方案 B（外部服务 via MCP）引入；此前保持原生扩展。

---

## 8. 风险与未决项

- **D-3 互斥切换**要求运维层明确"默认引擎"，避免两插件同时启用的装配期崩溃。
- **工具税回弹**是方案 A 的最大隐性成本，需在 PoC 阶段量化（对比原生 `wire_tools` 体积 vs LangGraph 默认）。
- **多租户模型**是断点续传/回滚的硬前置，须先于持久化扩展落地。
- **双引擎语义漂移**：若长期 A/B 并存，须把 resume/rollback/治理契约抽成共享接口，防止两套实现分叉。

---

## 9. 参考代码位置索引

| 关注点 | 位置 |
|---|---|
| 引擎接缝 / 消费调用 | `agent/plugins/react/__init__.py:56,85`；`agent/sys/chat.py:237` |
| 消费方契约 | `agent/core/contract.py:40`（`ReactResultLike`） |
| 四组件可换设计 | `agent/plugins/react/__init__.py:15-16, 81-85` |
| 目录插件 opt-in | `agent/profiles.py:111` |
| 单 provider 互斥（D-3） | `agent/core/container.py:222-226` |
| 零重依赖纪律 | `agent/api.py:3` |
| 工具税优化 | `agent/plugins/react/loop.py:402`（`_tools_for_step`/`_card`）、`allowed_domains`/`clip_by_domain`、`render_tools`/`render_domains`；`agent/core/request_cost.py`（`wire_tools`） |
| 审计 WAL | `agent/core/events_catalog.py:52-58`（`REACT_*`） |
| 续接雏形（非断点续传） | `agent/plugins/react/loop.py:388,418-478`；`agent/sys/chat.py:147` |
| 持久化缺口 | `agent/plugins/react/loop.py:100-125,1186-1211`；`agent/plugins/work_memory/backend.py:52`；`agent/run/chat.py:509,529,549` |
| MCP 桥（方案 B 复用） | `agent/api.py:27`（`McpStdioClient`/`build_task_schema`） |
