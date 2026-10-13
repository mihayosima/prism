# Skill 对齐 Claude Code / Agent Skills 开放标准（基于 FastSkills 源码研究）

编制时间：2026-10-11 23:12（GMT+8）
更新记录：

| 时间 | 版本 | 变更摘要 |
| --- | --- | --- |
| 2026-10-11 23:12 | v1 | 基于 matthewlee0102/FastSkills 源码逐段研究，给出"如何让本仓 skill 插件成为 Claude Code 同款（Agent Skills 开放标准兼容）"的对齐方案，含差距对照与代码级实施步骤 |
| 2026-10-12 00:26 | v1.1 | **阶段 0/1/2 已实施并真机验证**：目录式布局 `skills/<name>/SKILL.md`（扁平兼容）、`_parse_meta` 支持 YAML 块标量（多行 `description`）、**去掉强制 `skill.` 前缀**、`{baseDir}` 替换、目录式写回不分裂、`skills_routes` 放开前缀校验。注：实施时 loader 已随分层修复迁至 `agent/core/skills_loader.py`。阶段 3/4/5 未做 |
| 2026-10-14 01:45 | v1.2（勘误） | **路径归位记录（正文保留历史原貌，未回改）**：v1.1 所述「loader 在 `agent/core/skills_loader.py`」已再次归位——技能三模块（`Skill`/`SkillsCatalog`/loader）现位于 `agent/plugins/skills/{model,catalog,loader}.py`，只读 MCP 适配器现位于 `agent/plugins/skills/adapter.py`。依据：架构文档 §三层容器语义（`core/` 零业务词；`plugins.memory` 即 system 层住插件层的先例），`agent.api` 不再导出任何技能符号。详见 `agent内部架构-开发文档.md` 2026-10-14 更新记录 |

---

## 0. 一句话结论

你要的"Claude Code 同款的工具"，本质是 **Agent Skills 开放标准**：`skills/<name>/SKILL.md`（目录化、可捆绑脚本与参考文档）+ `name`/`description` frontmatter（description 是命中触发词）+ **渐进式披露（Scan→Match→Read→Follow→Deliver）**。

FastSkills（matthewlee0102 版）就是把这个标准抽成一个独立 **FastMCP server**，让任何 MCP 客户端都能用。但它只是个"搬运工"——**我们已有的 skill 插件在披露机制上比它更优雅**（索引直接注入 system prompt、正文经 `system_note` 注入上下文，而非让模型再调一次 `view` 读回来）。所以对齐目标是：**用目录化布局 + 标准 frontmatter + `{baseDir}` 替换，使本仓能直接消费 Claude Code / Anthropic 社区的技能文件**，同时保留我们更优的注入通道。

> 注意区分两个 FastSkills：
> - **matthewlee0102/FastSkills**（本研究对象）：FastMCP 引擎，实现 Claude Code 式技能披露。
> - **gofastskill/fastskill**（Rust CLI）：技能的"包管理器"（manifest/lockfile/语义检索），上一版方案已覆盖，与本次"同款工具"正交。

---

## 1. FastSkills 源码研究（逐段，已读 `src/mcp_server.py` 等）

### 1.1 技能存储：每技能一个目录
`skills/` 下每个技能是**一个文件夹**，内含 `SKILL.md` + 可选 `scripts/`、`reference/`、`templates/`、`examples/`、`assets/`（见仓库 `skills/` 目录：`duckduckgo-websearch/`、`pptx/scripts/office/...`、`pdf/reference.md`、`internal-comms/examples/` 等）。
- 发现逻辑 `list_skills()`（`mcp_server.py:110-168`）：`for skill_dir in _skills_dir.iterdir()` → 找 `skill_dir/"SKILL.md"` → 解析 frontmatter。
- 即"丢一个带 `SKILL.md` 的文件夹即自动发现"，社区技能零改造接入。

### 1.2 Frontmatter：`name` + `description`（description 是命中信号）
- 解析 `_parse_frontmatter`（`mcp_server.py:80-103`）：正则取 `name:` 与**多行** `description:`。
- 标准写法（看 `skills/duckduckgo-websearch/SKILL.md`）：
  ```yaml
  ---
  name: duckduckgo-websearch
  description: Search the web using DuckDuckGo. Use when the user wants to search
    the internet, find current information... Triggers include requests like "search for X"...
  ---
  ```
  **关键点**：`description` 是一段"什么时候该用我"的触发说明（可跨多行），这是渐进式披露里模型做 **Match** 的唯一依据。我们现有 `description` 只是 ≤120 字首行摘要，不够"同款"。

### 1.3 渐进式披露（Scan→Match→Read→Follow→Deliver）
| 阶段 | FastSkills 工具/机制 | 代码位置 |
| --- | --- | --- |
| Scan | `list_skills()` 返回 `name + description + path`（~100 token/技能） | `mcp_server.py:110-168` |
| Match | 模型自行判断哪个技能匹配 | （模型决策，无代码） |
| Read | `view(path=...)` 读完整 `SKILL.md`（支持 `view_range`） | `mcp_server.py:418-516` |
| Follow | `bash_tool`（跑捆绑脚本，`cwd=workdir`，超时 120s）、`file_create`、`str_replace` | `mcp_server.py:523-637` |
| Deliver | （产出文件） | — |

### 1.4 `{baseDir}` 占位符（关键可移植特性）
`SKILL.md` 正文用 `{baseDir}` 指代自身目录，例如（duckduckgo 示例）：
```
uv run --directory {baseDir} python scripts/search.py "query"
# {baseDir} 指 /.../skills/duckduckgo-websearch
```
宿主在加载时需把 `{baseDir}` 替换成技能文件夹真实路径。FastSkills 的 `view` 只返回原文、由模型脑内替换；Claude Code 在注入时自动替换。**我们若对齐，应在注入前替换。**

### 1.5 它是 MCP 包裹层，不改宿主 system prompt
FastSkills 是独立 FastMCP server，宿主**自己**调 `list_skills` 才看到技能——索引不在 system prompt 里。我们的 `index_block()` 直接注入 system prompt，模型**始终**能看到技能索引，这比 FastSkills 更省一轮 tool call、更不容易漏触发。

### 1.6 云注册表（可选，本次不纳入）
`search_cloud_skills` / `install_cloud_skill`（`mcp_server.py:230-411`）对接 SkillsMP 云目录，从 GitHub 拉取技能目录。属"运维增强"，与"同款工具"标准无关，留作后续。

---

## 2. Claude Code / Agent Skills 标准要点（README + 生态）

- 布局：`.claude/skills/<name>/SKILL.md`（Copilot 用 `.github/skills/`，Codex/Cursor 同格式）。
- frontmatter：`name` + `description`（description = 触发说明）。
- 技能可捆绑 `scripts/`、`references/`、`templates/`、`examples/`、`assets/`。
- **可移植**："write once, use everywhere"——Claude Code / Copilot / Codex / Cursor / OpenClaw / nanobot 共用同一格式；可取自 [anthropics/skills](https://github.com/anthropics/skills)、SkillsMP 或社区。
- 命中后，模型**跟随 SKILL.md 指令**，用自身原生工具（Read/Bash/Write）执行——这正契合我们"content 注入 + 模型照做 + 可选 run"的模型。

---

## 3. 与你现有 skill 插件的差距对照

已核实你代码（`agent/core/skill.py`、`agent/sys/skills_loader.py`、`agent/sys/skills.py`、`agent/plugins/react/loop.py`）：

| 维度 | Claude Code / 标准 | 你现状 | 是否需改 |
| --- | --- | --- | --- |
| 布局 | `skills/<name>/SKILL.md` + 捆绑目录 | 扁平 `data/skills/<name>.md` 单文件 | **需** |
| frontmatter | `name` + 多行 `description`（触发说明） | 极简：`name/description/risk/version/tags`，description 仅首行 | **需**（多行 description） |
| 名称前缀 | 文件夹名（如 `duckduckgo-websearch`） | `parse_skill_file` 强制加 `skill.` 前缀（`skills_loader.py:61-62`） | **需**（允许原名，避免社区技能改名） |
| `{baseDir}` | 支持（加载时替换） | 不支持 | **需** |
| 索引注入 | `list_skills` 工具（按需调） | `index_block()` 注入 system prompt（更优） | **保留** |
| 正文加载 | `view()` 返回文本 | `skill load` → `reason(system_note=...)` 注入（更优） | **保留** |
| 执行 | 模型用原生 Bash/Write 跟随 | 同（content 注入 + 可选 `run`） | **保留** |
| 管理面 | 无 | `GET/POST/PUT/DELETE /api/skills` + 前端面板 | **保留并扩展** |

**结论**：披露与执行机制我们已"同款且更优"；差异集中在**文件布局、frontmatter 语义、名称前缀、`{baseDir}`** 四点。改这四点即可直接消费 Anthropic/社区技能。

---

## 4. 对齐实施计划（代码级，指向具体文件/函数）

### 阶段 0 —— 兼容目录布局（`skills_loader.py`）✅ 已实施
> 实施补记：名称解析改为 `frontmatter.name or 目录名 or 文件名`；两布局**重名时告警跳过**
> （目录式优先），刻意不让重名把容器装配拖崩——`SkillsCatalog.register` 的显式同名冲突
> 仍照旧抛 `ServiceConflictError`。
- `discover_skills`（`skills_loader.py:77`）：现有 `d.glob("*.md")`。改为：
  1. 先 `d.glob("*/SKILL.md")`（目录式技能，优先）；
  2. 仍兼容扁平 `*.md`（旧文件型技能不失效）。
- `Skill` dataclass（`skill.py:16`）：新增字段 `basedir: str | None = None`（技能文件夹路径；扁平文件为 `data/skills` 或 None）。
- `parse_skill_file`（`skills_loader.py:45`）：解析目录式时 `basedir = p.parent`；扁平式 `basedir = d`。
- `write_skill_file`（`skills_loader.py:91`）：目录式写入 `skills/<name>/SKILL.md`（保留扁平 `*.md` 作为兼容写回路径）。

### 阶段 1 —— frontmatter 语义对齐（`skills_loader.py`）✅ 已实施
> 实施补记：`_parse_meta` 改为**带块标量的状态机**（`|` 保留换行 / `>` 折叠），而非单纯
> 折叠续行——因为社区 SKILL.md 用的是标准 YAML 块写法。破坏实验证明：旧实现会把
> `description` 解析成字面量 `"|"`，并把 `Use when:`、`Triggers include:` **污染成假键**。
> `skills_routes.py` 的前缀强加（`name.startswith("skill.")`）已同步删除，改为名称合法性
> 校验（防路径穿越）。
- `_parse_meta`（`skills_loader.py:31`）：当前只取首行 `v.strip()`。增强为支持**折叠多行 YAML 值**（Key 后换行 + 缩进的行并入 value），以正确解析 Claude Code 式多行 `description`。
- 名称前缀：`parse_skill_file`（`skills_loader.py:61-62`）去掉强制 `skill.` 前缀——frontmatter `name` 或文件夹名即最终 `name`。内部 `skill` 元工具的 `load` 动作直接按此 `name` 路由（前缀仅作旧数据迁移兼容，不再强制）。
- 保留 `risk`/`version`/`tags` 作为我们扩展字段（标准只要求 name/description）。

### 阶段 2 —— `{baseDir}` 替换（`skills.py` + `skills_loader.py`）✅ 已实施
- `SkillsCatalog` 新增 `render_content(skill)`：`skill.content.replace("{baseDir}", skill.basedir or "")`。
- `handle_meta` 的 `load` 分支（注入 `task.skill_docs` 处，`loop.py:769` 经 `system_note`）改用 `render_content(skill)` 而非原始 `content`。

### 阶段 3 —— 索引块格式对齐（`skills.py`）⏳ 未做（下一轮）
- `index_block()` 输出改为 Claude Code 风：`name — <description（多行折叠为一行，截断）>`（沿用 `loop.py:220` 的 `_slim_description` 截断）。保持预算截断。

### 阶段 4 —— 管理面兼容（`skills_routes.py` + `SkillsView.vue`）⏳ 部分完成
> 已完成：放开 `skill.` 前缀强加 + 名称合法性校验；`write_skill_file` 对**目录式技能写回原
> `SKILL.md`**（否则管理面一编辑就会分裂出扁平副本、下次扫描重名）。
> 未做：展示技能捆绑资源（`scripts/` / `reference/` 等目录清单）。

### 阶段 5（可选，后续）—— 社区技能直装 ⏳ 未做
- API 与前端天然按 `name` 操作，无需大改；仅需支持"技能可带 `basedir`/捆绑目录"的展示（如列出 `scripts/`、`reference/`）——属增量，非阻断。

### 阶段 5（可选，后续）—— 社区技能直装
- 参考 FastSkills `install_cloud_skill`（`mcp_server.py:341-411`）的思路，加 `POST /api/skills/install {url}` 从 GitHub 拉取技能目录解包到 `data/skills/`。**本轮不做**，列为后续。

---

## 5. 风险与边界

- **不破坏既有**：扁平 `*.md` 技能继续可用（阶段 0/1 兼容写回）；`skill.` 前缀仅作旧数据兼容。
- **多行 description 解析**：用稳健的 YAML 子集解析；解析失败回落单行（不要因社区技能格式怪异拖垮启动，延续现有 `try/except` 跳过策略）。
- **`{baseDir}` 安全**：替换值来自本地技能目录绝对路径，非外部输入；无需额外消毒。
- **我们更优的部分保留**：索引注入 system prompt、正文经 `system_note` 注入——不退回 FastSkills 的"再调 `view`"模式。

---

## 6. 待拍板

1. **布局切换策略**：目录式优先 + 扁平兼容，还是一步到位只支持目录式（旧扁平文件迁移脚本）？
2. **`skill.` 前缀**：完全去掉（社区技能原名），还是保留为可选命名空间（同名冲突时再前缀）？
3. **多行 description** 是否用于 `index_block` 的匹配展示（更长但更易命中），还是仍截断到首句？
4. **阶段 5 社区直装**是否进本轮，还是先只做"能消费本地 Anthropic 格式技能"？
5. 是否顺便把 `Risk`/`allowed-tools` 等 Claude Code 扩展 frontmatter 也建模进 `Skill`（影响 `run` 调用与审计）？

> 行号均为研究时快照（FastSkills main 分支 + 本仓当前代码），实施前 `git` 复核实际位置。
