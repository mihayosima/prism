# 选品 Agent · 记忆架构设计（参考 Zep · 封装为单一插件）

| 文档属性 | 内容 |
|---|---|
| 文档状态 | **v0.1 方案（已批准并实现）** —— 实现落点：`agent/plugins/memory/__init__.py`（目录插件 `plugins.memory`）、`agent/tables.py` 记忆三表、`agent/fixtures/llm.py` 提取路由、chat 接线、`agent/tests/test_memory.py`（9 项）；真实 DeepSeek 提取实测通过 |
| 归属 | 通用 Agent 工程（`agent/`）· 原对应 `doc/agent完善路线图-待完善设计清单.md` §2.8 记忆项 |
| 承载形态 | **整块记忆架构 = 一个子系统插件 `plugins.memory`（provides `ctx.memory`，layer=system）**，与 guard/llm 同级；**以「目录插件」形态落在 `agent/plugins/`** —— 来源（目录）决定可启停卸，layer 只是语义标签，二者正交 |
| 最近变更 | 2026-09-24 由系统层（`SYS_PLUGINS`）下沉为目录插件 `plugins.memory`（layer=system）；插件页可启用 / 停用 / 卸载，默认启用 |
| 设计参考 | Zep 记忆架构（本地 clone `/tmp/zep`：README / ingestion / legacy 源码）+ 开源时序图 Graphiti（`getzep/graphiti`） |
| 关联文档 | 《agent内部架构-开发文档.md》·《记忆插件-能力缺口分析.md》· `doc/agent搭建计划.md` · `doc/接入契约v2-双向适配.md` · `doc/agent完善路线图-待完善设计清单.md`（后三者为**历史归档，不作工作参考**） |
| 编制日期 | 2026-09-22 |

---

## 0. 结论先行

1. **记忆 = 有生效期的图事实 + 原始时序**,不是"把聊天记录再存一遍"。借鉴 Zep 的 `Fact{valid_at/invalid_at}` 与 graphiti 的时序图:每条记忆是一个 (实体→关系→实体) 的三元组事实,带时间戳与生效区间。
2. **三层结构**:`Episodic`(agent_events 已是 append-only 时序,直接当"原始记忆")→ 经 **LLM 提取**成 `Semantic` 持久事实 → 再经 **打分/衰减/召回** 进上下文。**提取的动作本身也走 `ctx.llm`(guard/budget 照生效)**——记忆不是特权通道。
3. **打分模型借 Zep 经典口径**:`observed_score`(首见加权)+ `last_accessed`(重访)+ **时间衰减**;召回按 相关度×新鲜度×重访度 排序,可选 **MMR/RRF** 去重融合(留作 SearchProvider 演进)。
4. **注入点收敛**:`recall → context` 组装成一个"记忆上下文块",注入 chat 的规划/解说;**决策记忆**(技能运行为什么 pick/拒)异步自动写库——让 agent "记得自己做过什么决定"。
5. **全部封装在插件内**:表、提取、打分、衰减、召回、上下文组装、prune 都在 `plugins.memory` 里;对外只有 `ctx.memory.*` 与事件。离线确定性可测,卸载无残留。

> 一句话:**把 Zep 的"时序事实 + 打分衰减 + 上下文注入"压成一个自洽的 `memory` 插件;记忆本身走 LLM 提取,但仍过 guard/budget,不给记忆开特权。**

---

## 1. 借鉴 Zep 的骨架（有据 · borrowed / trimmed）

| # | 借鉴 Zep 的点 | 出处（本地 clone） | 落到 `plugins.memory` |
|---|---|---|---|
| Z1 | **Fact 带时空**：`valid_at / invalid_at / created_at`，事实是"带生效期的" | legacy `lib/graphiti/service_ce.go: Fact{ValidAt, InvalidAt}` | 每条记忆含 `valid_at/invalid_at`（否决策可"失效"而非删） |
| Z2 | **Ontology（实体/关系类型先声明，不追溯）** | `ingestion/README.md#Ontology` | `memory` 插件内声明领域 ontology（实体类型：product/skill/user 偏好/决策；关系类型：`skill→picked`、`pick→denied_reason` 等）——**机制层零业务词，ontology 是可配数据** |
| Z3 | **Episodic 原始序列 vs Semantic 持久事实** | legacy memory（episodic/semantic/perpetual）概念 | Episodic = 复用 `agent_events`（已是 append-only 时序日志）；Semantic = `agent_memories` 提取事实 |
| Z4 | **LLM 自动提取 事实/意图/决策** | legacy API 记忆提取器 | `Extractor`（schema 化，走 `ctx.llm.complete`，离线 Fixture 可测） |
| Z5 | **打分口径：observed_score + last_accessed + 时间衰减** | legacy memory scoring（经典 Zep 回忆排序） | `agent_memories.observed_score / last_accessed_at / decay` + 排序公式 |
| Z6 | **语义召回 + MMR + RRF** | legacy `lib/search/{mmr,rrf}.go` | 抽象 `MemorySearchProvider`：MVP=词面+衰减排序；演进=向量 + MMR/RRF |
| Z7 | **get-memory → 注入上下文**（`to_messages/to_search_query`） | Zep 记忆到上下文惯例 | `memory.context(ctx, query)` → 记忆块注入 chat system |
| Z8 | **增量更新、不重建** | Graphiti 设计哲学 | 提取/合并/失效都是单条增量，不整库重建 |

**trim（主动不做 / 后置）**：Graphiti 的全图 Pagerank 社区聚类、深度图遍历、多租户 group 隔离、云端托管——均后置（§9），MVP 先把"事实+打分+召回+注入"这条主轴做实。

---

## 2. 定位与集成点（封装边界）

```
plugins.memory（子系统插件 · 目录插件形态，provides ctx.memory）
  ├─ 自带表（namespace agent_）：agent_memories / agent_entities / agent_memory_edges
  ├─ 消费：agent_events（episodes 源，只读）· ctx.llm（提取，guard/budget 照生效）· ctx.db
  ├─ 服务面：remember / add / recall / context / stats / prune / wipe
  └─ 事件（durable）：agent/memory/remembered · agent/memory/recall
```

集成点（都不反向依赖 memory 内部）：
- `chat` 通过 `ctx.has("memory")` 尽力注入（**不**声明 `inject ["memory"]`，缺此插件也照常对话）：plan 前注入 `memory.context(...)`；narrate 可带相关记忆。
- `scoring` 完成后异步 `add(kind="decision", source="run:…")`：把"为什么 pick/拒"写进记忆（决策记忆）。
- `produce`（loop）后续可选注入（MVP 先只接 chat + scoring）。

**红线**：`memory` 不是特权通道——提取每一次都走 `ctx.llm.complete`（guard 矩阵、预算照样裁决）；写库只经 `ctx.db`。纯机制零业务词（业务词只出现在 ontology 配置与内容层）。

---

## 3. 记忆模型（类型 + 字段）

### 3.1 四类记忆（kind）

| kind | 含义 | 例子（domain 由内容层给，机制不认识） |
|---|---|---|
| `fact` | 持久事实 | "用户偏好低库存类目"、"p_1002 单价 9.9 属热销带" |
| `intention` | 意图/任务 | "用户要求下周一重跑替代型" |
| `decision` | 决策记忆（agent 做过什么决定） | "run_xxx：p_1003 被否（价格偏高）" |
| `observation` | 观察/信号 | "消费类目销量近 7 日+11%" |

### 3.2 字段（`agent_memories`）

```
id TEXT PK · kind TEXT · content TEXT · entity_ids TEXT(json)
score_observed REAL · last_accessed_at TEXT · accessed_count INT
created_at TEXT · valid_at TEXT · invalid_at TEXT(NULL=仍有效)
source TEXT（引用 event/run，如 'agent/chat/done:seq12'）· extra TEXT(json)
```

### 3.3 实体与关系（`agent_entities` / `agent_memory_edges`）

```
entities: id · kind(product|skill|user|decision) · name · meta
edges:    source_id · target_id · rel_type · weight · at
```

> 机制只保留**通用图结构**；`kind/rel_type` 的取值 = 可配 ontology（§1 Z2）。MVP 只做**一级邻边**（skill→picked product 等），不做深度遍历（后置）。

---

## 4. 三个核心动作

### 4.1 remember（摄取 / 提取）
1. 触发：① 监听 durable 事件（`agent/chat/in` 用户消息、`agent/turn/end` 技能运行）被动摄取；② 手动 `add(ctx, kind, content, entity_ids)`（如 scoring 的 decision）写入。
2. 提取（`Extractor`）：`(messages|事件载荷) → LLM(schema 化) → {facts[], intentions[], entities[], relations[]}`；走 `ctx.llm.complete`（caller=`plugins.memory`），guard/budget 照生效；离线 Fixture 路由（`<!--memory:extract-->` 标记）确定性可测。
3. 落库：规范化实体（去重/别名合并）→ 建 edge → 写入/合并（同 content 合并时 `score_observed` 取高、`last_accessed_at`=now、`valid_at` 刷新）。
4. durable 事件 `agent/memory/remembered`（审计）。

### 4.2 recall（召回）
`recall(ctx, query, limit=8)` → 候选 == 打分排序：
```
score = w_sem * semantic(query, content) + w_fresh * freshness(created_at) + w_access * recency(last_accessed)
       (在失效/低证据记忆上 × 0.5 淡化，不删除)
回到语义搜索前，MVP 的 semantic 项 = 词面重叠（token 交集/字面命中）* observed_score；
向量/嵌入 + MMR/RRF 由 MemorySearchProvider 演进。
```
→ 返回 `[RankedMemory(...)]`（content/kind/score/at/source）。

### 4.3 context（注入）
`context(ctx, query, limit)` = recall + 邻边关系行 → 剪成上限 token 的纯文本块：
```
[相关记忆]
- (fact) 用户偏好低库存类目 (score .82 · 3d前)
[相关实体关系]
- skill:substitution → picked:product:p_1001 (8.2)
```
注入点：chat 的 plan/narrate system 内存块（`to_messages` 的本地版）；上限可配，避免吞上下文。

### 4.4 decay / prune
- 时间衰减：新鲜度按半衰期衰减；`last_accessed_at` 越久未访问、accessed_count 越少 → 排名越低（不删除，只淡化）。
- `prune(ctx, *, older_than_days=30, min_score=0.25)`：清理"足够旧且打分低于阈值"的记忆（以及已失效记忆），可作维护任务。`wipe(ctx, *, kind=None)`：按 kind 清空（`kind=None` 即全清）。二者为逐条硬删；日常的"衰减"只影响排名，不改数据。

---

## 5. Seam（插件内部接口，可换实现）

| seam | 现在 | 演进 |
|---|---|---|
| `MemorySearchProvider` | 词面重叠 + 衰减排序（离线确定） | 向量/嵌入 + BM25 混合 + MMR 去重 + RRF 融合 |
| `MemoryExtractor` | LLM（Fixture / deepseek） | 可按记忆类型分派、批量合成（perpetual） |
| `MemoryStore` | sqlite（`agent_*` 表） | 换后端=换 provider |

> 全部在插件内部声明；`test_agent_purity` 只扫 core，`memory` 属子系统层，业务词只允许出现在 ontology 配置。

---

## 6. 事件与审计
- durable：`agent/memory/remembered`（{id,kind,at}）、`agent/memory/recall`（{query, hits, at}）。
- 与 `agent_events` 的关系：memory **消费但不污染** events（events 仍是单一审计脊柱；memory 落自己的表）。

---

## 7. 目录 / 文件落点（已落地）

```
agent/plugins/memory/__init__.py   # 插件 plugins.memory（provides ctx.memory · layer=system · 目录插件形态）
agent/tables.py                    # 三表 DDL：agent_memories / agent_entities / agent_memory_edges
                                   #   —— 仍由 Storage 恒定应用（迁移已入库 → 防脏武：勿从 MIGRATIONS 删除）
agent/fixtures/llm.py              # <!--memory:extract--> canned 路由（离线确定性）
data/agent_plugin_state.json       # 期望态 enabled 含 plugins.memory（默认启用）
agent/run/chat.py                  # 装配经 PluginManager；/api/memory 判空降级（未启用不裸崩）
agent/tests/test_memory.py         # 9 项：提取/合并/打分/衰减/召回/上下文/prune/卸载无残留/可管理性/停用降级
```

---

## 8. MVP 范围线
**MVP IN**：四类记忆存取 · LLM 提取（Fixture/deepseek）· 词面+衰减打分 · recall/context 注入 chat · scoring decision 自动记忆 · prune/wipe · offline 全绿。
**MVP OUT（路线）**：向量/嵌入召回 · MMR/RRF · 实体图深度遍历/社区聚类（Graphiti 全量）· perpetual 合成/摘要 · multi-session 隔离。

---

## 9. 与《待完善清单》对标 & 新 OPEN

| 澄清/决策 | 我的建议 |
|---|---|
| OPEN-8 记忆注入（清单 §4） | 先摘要式记忆块注入 chat（低风险），全文会话检索后置 |
| MEM-1 提取触发时机 | 事件驱动（chat/turn 结束）+ 手动 `add` 双轨；异步不阻塞主链路 |
| MEM-2 决策记忆自动写库 | 是——scoring 的 pick/拒由 `add(kind="decision", source="run:…")` 自动落，让 agent "记得自己的决定" |
| MEM-3 search 演进线 | 先词面；`MemorySearchProvider` 插槽留好，向量时换 provider 不换架构 |
| MEM-4 提取走 ctx.llm | 是——guard/budget 照生效，记忆非特权通道 |
| MEM-5 decay 参数 | 半衰期/权重做成配置（`config["agent"]["memory"]`），不写死 |

---

## 10. 参考
- Zep（本地 `/tmp/zep`）：`README.md`（Zep Cloud + Graphiti 定位）、`legacy/src/lib/graphiti/service_ce.go`（Fact{ValidAt,InvalidAt} 时序事实）、`legacy/src/models/fact_common.go`、`legacy/src/lib/search/{mmr,rrf}.go`（召回融合）、`ingestion/README.md#Ontology`（可配实体/关系、先设后灌）
- Graphiti（`getzep/graphiti`，时序图谱思想）
- 《agent内部架构-开发文档.md》§4（子系统=插件）· `doc/接入契约v2-双向适配.md`（memory 不改变引擎边界；历史归档）

---

> 批准点：**整体作为单一插件 `plugins.memory` 落地的方案是否通过**；以及 §9 的 5 个 MEM 建议取舍。你点头(或圈改)后我按 §7 落地实现并全量测试。
>
> **2026-09-24 更新**：已通过并落地；落点由系统层 `agent/sys/memory.py` 调整为目录插件 `agent/plugins/memory/`（id `plugins.memory`、layer=system），支持插件页启用 / 停用 / 卸载，默认启用。