# Skill 与 MCP/Tool 更新方案（基于 FastSkill 研究）

编制时间：2026-10-11 17:37（GMT+8）  
更新记录：

| 时间               | 版本 | 变更摘要                                                                                                                   |
| ---------------- | -- | ---------------------------------------------------------------------------------------------------------------------- |
| 2026-10-11 17:37 | v1 | 基于 FastSkill 研究，给出 skill / MCP / tool 三者统一更新方案，待评审                                                                     |
| 2026-10-11 23:04 | v2 | 新增 §5「统一能力面设计」（四层落地、无耦合规则、契约测试）与 §6 分步计划，收敛为「reactor 层统一、最小档优先」；§7 待拍板收敛到统一能力面维度                                       |
| 2026-10-11 23:21 | v3 | 用户拍板：本轮先做 **MCP 增量适配器（路线2 原生只读适配器，仅 list/view 最小可用）**。重写 §3 方向 D 为已决方案、新增 §8 实施方案、§7 待拍板收敛为「MCP 适配器先行，其余方向 A/B/C/E 待定」 |

---

## 0. 研究结论：市面上有两个同名项目，定位完全不同

### 0.1 gofastskill/fastskill —— 技能的「包管理器 + 运维工具链」（Rust CLI）

关键词：**npm/uv 但针对 agent 技能**。

- 沿用 Claude Code 的 `SKILL.md` 布局，额外增加：
  - `skill-project.toml` 清单（声明依赖、分组 prod/dev、来源 origin：local/git/zip/repository）；
  - `skills.lock` 锁文件（按 digest 锁定，可离线复现安装）；
  - 自包含团队 bundle（含完整依赖闭包，可版本化离线分发）；
  - 语义检索（需 OpenAI embedding，本地向量索引）；
  - 质量评估 `fastskill eval` 与文本梯度自动优化 `fastskill optimization`；
  - 本地只读 HTTP API + Web UI（`fastskill server serve`）；
  - **只读默认**的 MCP server（`fastskill mcp serve`，`--enable-write` 才放开写）；
  - `managed` 签名状态：按 digest 校验、隔离(quarantine)封禁内容、按签名门禁安装。
- 核心哲学：*FastSkill manages the files; your agent reads them.* 它不直接进 agent 内核，而是管理磁盘上的技能文件，agent 通过自己的机制读取。

### 0.2 matthewlee0102/FastSkills —— 技能的「MCP 引擎」（FastMCP server）

关键词：**把技能暴露成 MCP 工具，渐进式披露**。

- 暴露的核心工具（常驻、仅 ~5 个元工具，不是每个技能一个工具）：
  - `list_skills`（~100 token/技能的名字+描述索引）、`view`（按需读完整 SKILL.md）、  
    `bash_tool`（执行技能脚本）、`file_create`/`str_replace`（产出文件）、  
    以及云工具 `search_cloud_skills`/`install_cloud_skill`。
- 工作流 = **Scan → Match → Read → Follow → Deliver**（Claude Code / Copilot / Codex 同款渐进式披露）：  
  索引只给 ~100 token/技能的元数据，agent 自行判断是否匹配，匹配才 `view` 读全量正文，再用 `bash_tool` 跑脚本。
- 技能来源 = 单一 `--skills-dir` 目录，丢一个带 `SKILL.md` 的文件夹即自动发现。

> 与你的设计哲学**关键一致点**：FastSkills 也是「少量元工具 + 按需注入完整正文」，而非「每个技能注册一个工具」——这正是你当初否决 tool 注册式 token 膨胀的理由。你的单一 `skill` 元工具 ≈ FastSkills 的 `list_skills`+`view` 合并版。

---

## 1. 你现有架构复盘（已核实，带行号）

| 能力       | 现状                                                                                                                      | 关键位置                                                                                    |
| -------- | ----------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------- |
| 技能数据模型   | `Skill` dataclass（name/description/content/risk/tags/version/run/owner/source/enabled）                                  | `agent/core/skill.py`                                                                   |
| 技能目录     | `SkillsCatalog`：磁盘扫描、注册、查询、单 `skill` 元工具、index_block、管理面 CRUD、持久化到 `skills_state.json`                                  | `agent/sys/skills.py`、`agent/sys/skills_loader.py`                                      |
| 解耦方式     | 插件 `Skills` 经 `ctx.provide(Services.SKILLS, catalog)`，react **反向读取** `ctx.get(Services.SKILLS)`；不耦合内核                   | `agent/plugins/skills/__init__.py`、`loop.py:604/1594`                                   |
| 技能暴露给模型  | 常驻 `skill` 元工具 + 系统提示里的 index_block（预算截断）；`load` 动作把正文注入 `task.skill_docs` → `reason(system_note=...)` 按需注入             | `loop.py:604/769/849`                                                                   |
| MCP 工具接入 | `McpStdioClient.list_tools()` 拉取 → 各 MCP 插件 `ctx.tasks.register(ctx, "mcp.<server>.<name>", schema=...)` 注册为**一等 task** | `core/mcp_client.py:215`、`playwright_mcp/__init__.py:157`、`windows_mcp/__init__.py:144` |
| 工具暴露给模型  | `visible_tools(ctx)` 读取 task 目录，`_slim_description` 截 120 字；`lazy_tools` 可选开启（默认关）                                      | `loop.py:229/248/483`                                                                   |
| 工具审计     | `ctx.tasks.invoke` 走 `sys/guard.py` 统一策略（risk 模式 open/deny、owner 域分桶）                                                   | `react/__init__.py:19`、`sys/guard.py`                                                   |
| 管理面      | `GET/POST/PUT/DELETE /api/skills` + 前端 `SkillsView.vue`                                                                 | `run/skills_routes.py`、`web/src/views/SkillsView.vue`                                   |

**结论**：你已经有「渐进式披露 + 解耦 + 管理面」的骨架，与 FastSkill 的前沿做法方向一致；差距在**运维成熟度**（可复现安装、语义检索、质量评估、统一治理）和**MCP/tool 与 skill 的策略统一**上。

---

## 2. 差距 / 可借鉴点对照

| FastSkill 能力              | 你现状                                  | 借鉴建议                                           |
| ------------------------- | ------------------------------------ | ---------------------------------------------- |
| 渐进式披露（少元工具+按需注入）          | ✅ 已有（skill 元工具+index_block）          | 维持，无需改                                         |
| 清单+锁文件（manifest/lockfile） | ⚠️ 仅 `skills_state.json`（enabled 标记） | **方向 A**：升级为带 origin/group/depends 的清单 + 锁     |
| 分组 prod/dev               | ❌ 无                                  | 方向 A 附带                                        |
| 语义检索                      | ❌ 仅关键词 `search()`                    | 方向 B：可选 embedding，回退关键词                        |
| 质量评估 eval                 | ❌ 无                                  | 方向 E（大，后做）                                     |
| 自动优化 optimization         | ❌ 无                                  | 方向 E（大，后做）                                     |
| 团队 bundle / 离线分发          | ❌ 无                                  | 方向 A 延伸（中优先级）                                  |
| 只读 MCP 暴露自身技能             | ❌ 无                                  | 方向 D（按需，跨 agent 共享才做）                          |
| 统一策略/治理（签名隔离）             | ⚠️ tool 走 guard，skill 治理较轻           | **方向 C**：skill 与 MCP/tool 共用策略引擎 + 按 digest 门禁 |
| 每服务器工具预算 / 命名空间隔离         | ⚠️ MCP 已 `mcp.<server>.` 命名空间，但无预算上限 | 方向 C 附带                                        |

---

## 3. 更新方案（分方向，请你拍板范围与优先级）

### 方向 A：技能可复现安装（manifest + lockfile + 分组）★ 推荐首发

- **目标**：把 `skills_state.json` 升级为 `skill-manifest.toml`（schema_version、dependencies=[{name,origin,group,enabled}]）+ `skills.lock`（digest 锁定）。支持本地/远程来源、prod/dev 分组、依赖声明与加载顺序。
- **涉及文件**：`agent/sys/skills_loader.py`（读写 manifest/lock）、`SkillsCatalog`（分组过滤、依赖解析）、`Skill` dataclass（加 `group`/`origin`/`depends` 字段）、`run/skills_routes.py`（`?group=` 过滤）、`SkillsView.vue`（分组切换）、文档。
- **改动量**：中。复用现有 CRUD 骨架，增量加字段。
- **风险**：低。向后兼容现有 `skills_state.json`（迁移函数）。

### 方向 B：语义检索（可选、embedding 门控）

- **目标**：`SkillsCatalog.search` 在有 embedding provider 时走向量检索，否则回退关键词；index_block 可据查询重排。
- **涉及文件**：`skills.py`（检索策略）、配置（`config_schema` 加 `skills.embedding` 节）、前端搜索框不变。
- **改动量**：小–中。
- **风险**：低（无 provider 即降级）。需评估是否引入外部依赖（embedding SDK）。

### 方向 C：Skill 与 MCP/Tool 统一治理 ★ 与"A"并列核心

- **目标**：
  1. 技能 `run` 调用复用 `sys/guard.py` 同一策略引擎（当前 skill 的 risk 校验在 `handle_meta` 内手写，与 tool 审计双轨）；
  2. 增设「按 digest 门禁 / quarantine」注册表：对 MCP 工具与插件型技能做内容哈希登记，可疑项可一键隔离（呼应 FastSkill `managed` 思路，契合 G 端"AI+水利"安全诉求）；
  3. 每 MCP 服务器工具预算上限（已有 `mcp.<server>.` 命名空间，补预算）。
- **涉及文件**：`sys/guard.py`（策略统一入口）、`sys/skills.py`（`run` 走 guard）、`core/mcp_client.py`（digest 登记）、`run/skills_routes.py`（quarantine 管理端点）。
- **改动量**：中–大（治理是结构性）。
- **风险**：中。需保证不破坏现有 tool 审计路径（有 `test_guard_*` 守护）。

### 方向 D：对外只读暴露自身技能为 MCP（**本轮已拍板：路线2 原生适配器，list/view 最小可用**）

**用户决策（v3）**：不走「接第三方 FastSkills」的路线1，而走**路线2 原生只读 MCP 适配器**——在你现有 `Services.SKILLS` 之上加一个只读 MCP 适配层，把技能目录以 MCP 工具形式暴露给子 agent / 外部工具消费。内核自身仍走现有 `skill` 元工具 + `index_block` 注入，**不回灌 MCP**。本轮范围限 `list + view` 最小可用。

**为什么选路线2（而非路线1 直接接 FastSkills）**：

- **功能等价**：list/view 两个动作在两条路线下对模型/外部 agent 完全一致；
- **路线2 更优**：单一真相源（复用 `Services.SKILLS`）、进 `sys/guard.py` 审计、内部仍享 `index_block` 注入体验；
- **路线1 代价**：需独立进程 + 装 `fastmcp/httpx` 依赖（本机无 uv、Windows 起子进程有 `CREATE_NEW_PROCESS_GROUP`/`CHILD_SPAWN_OPTIONS` 坑）、与现有目录双份管理、不进审计；
- 故路线2 几乎不比路线1 多写代码，却免掉进程/依赖/审计/双目录代价。

**暴露的工具（最小可用，均为只读）**：

| 工具名          | 入参          | 内部实现                                                                                 | 对应 FastSkills |
| ------------ | ----------- | ------------------------------------------------------------------------------------ | ------------- |
| `skill_list` | `q?`（可选关键词） | `catalog().search(q)` → `[{name, description, risk, source}]`（不返回 `content`，控 token） | `list_skills` |
| `skill_view` | `name`（必填）  | `load(name)` → `render_content()`（含 `{baseDir}` 替换）返回正文                              | `view`        |

> `bash_tool` / `file_create` / `str_replace` / `search_cloud_skills` / `install_cloud_skill` **本轮不做**：前三者由你已有 `run` 字段承担（技能自己执行脚本的能力），后两者属社区直装（后置，见方向 E 旁注）。

**依赖与挂载**：

- 复用你现有 `core/mcp_client.py:187` 唯一 stdio spawn 点作为本仓内部 server 的桥；不引入新进程治理成本。
- 启动开关：在 `config_schema` 加 `mcp.skills_adapter` 节（`enabled: bool`，默认 `false`），与现有 MCP 开关同口径；不启用时零影响。
- **涉及文件**：新增 `agent/run/skills_mcp_adapter.py`（只读 FastMCP server）、`run/chat.py`（条件拉起）、`config_schema`（开关）。
- **改动量**：小–中（仅 1 个新文件 + 挂载 + 开关）。
- **风险**：低。适配器单向依赖 `Services.SKILLS`，不 import `sys/tasks`/`plugins.react`，零耦合（由 §8 S0 契约测试守住）。

### 方向 E：技能质量评估与自动优化（大，建议后置）

- **目标**：`fastskill eval` 式触发率评测 + 文本梯度自动精修 SKILL.md。
- **改动量**：大。
- **风险**：高、ROI 待验证。建议作为后续独立专项。

---

## 4. 建议落地顺序与未决项

**推荐首发组合：方向 A + 方向 C（治理部分）**——直接补强你"管理 + 企业级安全"两条主线，且与现有解耦架构无冲突。方向 B 可紧随（低风险增强），方向 D/E 视需求与 ROI 后做。

**请你拍板：**

1. 本轮做哪些方向？（建议 A+C，或 A+B+C）
2. 方向 A 的 manifest 用 TOML 还是沿用 JSON（你现有 `skills_state.json` 是 JSON）？
3. 方向 C 的 quarantine/digest 是否纳入本轮，还是仅先做"skill 的 run 复用 guard"这一小步？
4. 方向 B 的 embedding provider 用哪家（OpenAI / 自建 / 暂不做）？
5. 方向 D 当前是否有跨 agent 共享技能的真实需求？

> 注：v1 仅出分方向方案，未改生产代码。v2 进一步收敛为「reactor 层统一、零耦合」的统一能力面设计与分步实施计划（§5–§7）。**本次仍仅出方案，未改动任何生产代码**，待 §7 拍板后编码。

---

## 5. Skill⇄MCP/Tool 统一能力面设计（reactor 层统一、零耦合）

### 5.1 核心原则

- **发现层统一、治理层统一、管理层统一；执行层保持分叉。**
- 技能本质是「知识/过程注入 + 可选 `run`」；工具本质是「schema 驱动 + 副作用」。二者本质不同，**不该硬并**，只在「披露/策略/管理」三处汇合。
- **统一逻辑只进 reactor**（`agent/plugins/react/loop.py` 及 reactor 辅助）。`sys/skills.py` 与 `sys/tasks.py` 互不 import，依赖图保持 acyclic：reactor → 各子系统。
- 统一治理 = 两者**共依赖同一第三方策略服务** `sys/guard.py`（收敛，非耦合）：skill→guard、tool→guard，guard 不反向依赖任一方。

### 5.2 四层落地设计

**Layer 1 — 统一发现（Unified Index）｜最小档、零耦合**

- 现状：技能走 `SkillsCatalog.index_block()`（`loop.py:604` 拼接），工具走 `visible_tools(ctx)`（`loop.py:229/248`），两份清单。
- 做法：新增 `tool_index_block(ctx, max_chars)` —— 枚举 `ctx.tasks.catalog()`（`tasks.py:295`），每条 `tool.<name> — <_slim_description>` 单行（复用 `loop.py:220` 的 `_slim_description`，120 字上限），预算截断。
- 系统提示 = `技能索引 + "\n" + 工具索引`，模型看到**一份**能力清单。
- **耦合度：零新增**。只是把 reactor 已有的两份只读清单拼接；`sys/skills.py`/`sys/tasks.py` 无任何新 import。

**Layer 2 — 统一加载（single meta-tool）｜路由壳在 reactor**

- 现状：技能经 `skill` 元工具（`loop.py:850-857` 拦截 → `SkillsCatalog.handle_meta`）；工具经 lazy 路径 `tool_load`（`loop.py:1648` 定义、`1620` 调用）。
- 做法：把 `SkillsCatalog.meta_tools()` 返回的工具升级为统一的 `capability` 元工具（动作扩展）：
  - `action=load_skill, name=skill.x` → 现有逻辑，正文注入 `task.skill_docs` → `reason(system_note=...)`（`loop.py:769`）。
  - `action=load_tool, name=tool.y` → 复用 `_tool_load` 把完整 schema 加进本 run loaded 集。
- 模型用**一个动词**按需拉取技能正文或工具 schema → token 有界，披露体验一致。
- **路由壳位于 `loop.py:850-857`**：`name=="capability"` 时 `load_skill`→`SkillsCatalog.handle_meta`、`load_tool`→`_tool_load`。壳在 reactor 内，子系统干净。兼容期可保留 `skill` 别名。

**Layer 3 — 统一治理（Unified Governance）**

- 现状：技能 `run` 的 risk 校验在 `sys/skills.py:handle_meta` **内联手写**（plan 模式拒写），与工具走 `sys/guard.py` 是**双轨**。
- 做法：技能 `run` 改为调用与 `ctx.tasks.invoke`（`tasks.py:208`）**同一套 `sys/guard.py`** 谓词，传入 `skill.risk` / `skill.owner`。一处策略、两路执行。
- FastSkill `managed` 的「按 digest 隔离（quarantine）」搬入：MCP 工具与插件型技能注册时记内容哈希，可疑项一键门禁——直接契合 G 端「AI+水利」安全诉求（作为可选子项）。

**Layer 4 — 统一管理（Unified Management）**

- 新增 `GET /api/capabilities`（聚合 skill + tool 清单）：返回 `{type: skill|tool, name, risk, owner, source, enabled, group}`；技能可 CRUD，工具只读（由插件/MCP 管理）。
- 前端合并为单一 **Capabilities** 面板（替代 Skills / Plugins 分置；可渐进：先并排，后合并）。
- **子 agent 复用**：统一 `index_block` + `capability` 元工具经现有委派 ContextVar（`current_run_id` 等）透传，子 agent 也能发现技能——比「起 MCP server 暴露技能」（方向 D）风险更低、不引入新进程。

### 5.3 无耦合规则（机器锁死）

- **规则 1**：统一逻辑只进 `agent/plugins/react/`（reactor 层）。`sys/skills.py` 不 import `sys/tasks`，反之亦然。
- **规则 2**：技能 `run` 与工具 `invoke` 经 `sys/guard.py`（第三方策略服务）汇合，非相互依赖。
- **规则 3（契约测试）**：在 `test_services_contract.py`（现有 AST 守卫）加断言：
  1. `sys/skills.py` 不得 import `sys.tasks`；`sys/tasks.py` 不得 import `sys/skills`；
  2. 若实现 `capability` 路由壳，其定义必须位于 `agent/plugins/react/` 之下；
  3. `capability` 动作分派表不得被 `sys/skills.py` / `sys/tasks.py` 引用。  
     → 把「文档承诺 > 实现」用 CI 守住，后续谁改出跨子系统依赖立刻红。

### 5.4 什么**不动**

- MCP 工具仍经 `ctx.tasks.register("mcp.<server>.<name>")`（`playwright_mcp/__init__.py:157`、`windows_mcp/__init__.py:144`），统一索引只是**读**它们。
- 技能执行（注入 + `run`）与工具 `ctx.tasks.invoke` 各自保留，仅在治理入口汇合。
- `lazy_tools` / `visible_tools` / `_partition_meta` 现有行为不变，新层是**叠加**非替换。


## 7. 待拍板（v3 收敛：MCP 适配器先行）

**已拍板（v3）**：本轮落地 **方向 D 路线2**（原生只读 MCP 适配器，`skill_list` / `skill_view` 最小可用）。以下为仍需你确认/后续轮处理的项：

1. **本轮是否**只**做 MCP 适配器（路线2）？** 建议：是。其余方向 A/B/C/E 与 §5 统一能力面留作下一轮（避免一次性改动面过大）。
2. **`{baseDir}` 替换是否随本轮做？** `skill_view` 若要正确返回含脚本路径的正文，需在 `SkillsCatalog.render_content()` 实现 `{baseDir}` 替换（低风险、纯函数）。若本轮技能均不含占位符，可后置；建议**顺手做**以免 `skill_view` 返回无效路径。
3. **统一能力面（§5）是否并进本轮？** 不并进。§5 的 `capability` 元工具 / 统一 index / 治理统一属更大改动，待 MCP 适配器验证后再议。
4. **方向 A/B/C/E 是否进下一轮？** 待 MCP 适配器上线后评估（A 可复现安装 / C 治理统一 与 G 端安全诉求最相关）。
5. **MCP 框架选型**：适配器用 `FastMCP`（与 FastSkills 同款，轻量）还是复用你现有 `core/mcp_client` 手写 stdio 协议？建议先 `FastMCP`（实现最短），若不想引依赖则走手写 stdio 桥。

> 行号均为当前代码快照（v3 编写时），编码前以 `git` 实际位置再核对一次。

---

## 8. MCP 增量适配器实施方案（路线2，可逐步交付）

> 目标：在你现有 `Services.SKILLS` 之上叠加一个**只读** MCP 暴露层；内核 `skill` 元工具 / `index_block` 注入 / `sys/skills.py` / `sys/tasks.py` 均**不变**。适配器单向依赖 `Services.SKILLS`，零耦合。

### S0 — 契约护栏（先于一切，必做）

- 在 `test_services_contract.py` 加 AST 断言：新建的 `agent/run/skills_mcp_adapter.py` 只可 import `agent.api`（`Services`）、`agent.sys.skills_loader`、MCP 框架（`fastmcp` 或 stdio 桥）；**不得 import `agent.sys.tasks` / `agent.plugins.react`**。
- 产出：`test_no_mcp_adapter_coupling`，固化零耦合边界。绿了再动代码。

### S1 — 适配器骨架（1 个新文件）

- 新增 `agent/run/skills_mcp_adapter.py`：起只读 server，注册两个工具。
- 获取 catalog：`ctx.root.get(Services.SKILLS)`（与 `run/skills_routes.py` 同构，适配器运行于 run 层能拿到 `root`）。
- `skill_list(q?)`：
  - `items = cat.search(q) if q else cat.catalog()`；
  - 返回精简 list：`[{name, description, risk, source, enabled}]`，**不含 `content`**（控 token，对应 FastSkills `list_skills` ~100 token/技能）。
- `skill_view(name)`：
  - `s = cat.load(name)`；无则抛「未找到技能 `<name>`」；
  - 返回 `s.render_content()`（若 S2 已做 `{baseDir}` 替换则路径已解析）。

### S2 — `{baseDir}` 替换（顺手做，低风险）

- `SkillsCatalog.render_content(self)`：`content` 中 `{baseDir}` 替换为该技能文件所在目录（`parse_skill_file` 已记 `basedir`，见 Agent Skills 对齐方案阶段2）。
- 纯函数、单测易覆盖；不做则 `skill_view` 仅对不含占位的技能有效。

### S3 — 挂载与开关

- 在 `run/chat.py` `_register_routes` 旁新增 `_maybe_start_skills_mcp(deps)`：
  - 读 `config.mcp.skills_adapter.enabled`（默认 `false`）；为真则用现有 `core/mcp_client` stdio 桥拉起本仓内部 server（与 `playwright_mcp` 同形态），注册为 `mcp.skills.<tool>`；
  - 不启用时**不 import MCP 框架、不起进程、零影响**。
- `config_schema` 加 `mcp.skills_adapter` 节（`enabled: bool`，严格节需同步登记，避免设置页保存失败）。

### S4 — 复用现有管理面

- 技能 CRUD 仍走 `run/skills_routes.py`（`/api/skills`）；适配器**只读消费**，不另立管理端点（避免双份管理）。

### S5 — 验证

- **单测** `test_skills_mcp_adapter.py`（用 fake catalog）：
  - `skill_list` 无参返回全部、带 `q` 过滤正确；
  - `skill_view` 返回 `render_content()` 结果、`{baseDir}` 已替换；
  - 未知 `name` 抛「未找到」；
  - S0 断言：适配器 import 清单不含 `sys.tasks`/`plugins.react`。
- **真机**：启用开关后，用 MCP client 调 `skill_list` 确认返回；`curl --noproxy '*' http://127.0.0.1:87xx/api/skills` 仍正常（管理面未动）。
- **回归**：`test_services_contract.py`、`test_skill_plugin.py` 仍绿。

### 交付粒度建议

- 先交 **S0+S1+S2+S5（单测）+S3（开关关闭态）** 即可合并评审；S3 启用态与真机拉起可单独一提交，降低 review 负担。

> 本方案未改动任何生产代码，待你 review 通过后再按 S0→S5 编码。
