# 审批插件与 Claude Code 权限模型对照复核

| 文档属性 | 内容 |
|---|---|
| 文档性质 | **对照复核报告**（只读快照；不改代码、不改被复核文档） |
| 编制时间 | 2026-10-20（北京时间） |
| 复核对象 | `agent/plugins/approval/`（`__init__ / policy / providers / pending / options`）+ `agent/sys/guard.py` + `agent/profiles.py` |
| 对照基准 | Claude Code 权限模型（官方文档，2026 当前版，见 §7） |
| 结论 | 机制骨架与 CC **同构**，但**模式命名与语义相对 CC 几乎整体反了**，且**默认姿态与 CC 相反**（本项目"先放行"、CC"先询问"）；另有双套规则系统、规则粒度、记忆持久化三处结构性差异 |

## 更新记录

| 时间 | 版本 | 变更摘要 |
| --- | --- | --- |
| 2026-10-20 | v1.0 | 首版：基于逐行读码 + CC 官方文档的逐维度对照；差异 D-1..D-7 |

---

## 0. 结论先行

1. **对得上的**：三态 `allow/deny/ask` + deny 优先、`plan` 只读+软硬两层、`bypass` 不持久化、fail-safe 默认拒绝、澄清问询工具 —— 与 CC 语义一致（§5）。
2. **最刺眼的"不对味"**：**模式命名与行为相对 CC 整体对调**（§4 D-1）——
   项目的 `default` 实际行为 ≈ CC 的 `auto`（不问），项目的 `auto` ≈ CC 的 `default`（写类都问）；且 CC 的 `acceptEdits` 在本项目**没有对应档**。
3. **第二刺眼**：**默认姿态相反**（D-2）——CC 默认（Manual）对写类/Bash/网络**都问**；本项目默认（`approval.allow=["*"]` + `guard.default="open"`）**开箱什么都不问**，整套 HITL 装置空转。
4. **结构性差异**：CC 只有**一套**规则表，本项目把同义三态**拆到 guard 与 approval 两处**（D-3）；CC 规则到**参数**级，本项目只到**工具名**（D-4）；CC 的"不再问"按**仓库+命令**持久化，本项目只记**工具名**且**仅会话内存**（D-5）。

---

## 1. 对照基准：Claude Code 权限模型（官方文档）

**六个模式**（config 值 → 不问就放行的范围）：

| CC 模式 | 不问就放行 | 定位 |
| --- | --- | --- |
| `default`（CLI 里叫 **Manual**） | **仅只读**（工作目录内） | 逐项审阅、敏感操作 |
| `acceptEdits` | 只读 + **文件编辑 + 常用文件命令**（mkdir/touch/mv/cp） | 改代码 |
| `plan` | 只读 +（有 auto 时）分类器批准的命令 | 先探索后改 |
| `auto` | **全部**（后台分类器模型复核） | 长任务、减少打断 |
| `dontAsk` | 工作目录内只读 + 预批准工具；**本该问的一律拒绝** | 锁死 CI / 脚本 |
| `bypassPermissions` | 全部 | 隔离容器 / VM |

**规则系统**：
- 三张名单 `allow / ask / deny`，**按键 = 工具 + 参数**（`Bash(git diff:*)`、`Read(./.env)`）；**deny > ask > allow**。
- 按工具类型分层：只读=不问；Bash=除内置只读命令外都问；文件修改=问；WebFetch=除预批文档域外都问；WebSearch=问。
- **"Yes, and don't ask again"**：Bash 按 **仓库 + 命令** 持久化、文件修改仅到**会话末**、WebFetch 按 **仓库+域**持久化；写入 `.claude/settings.local.json`。
- 提示框含 **"Yes, and switch to auto mode"** 一键升档。

> 一句话：**CC 的默认姿态是"先问"，用户主动选择少问。**

---

## 2. 本项目 approval 插件机制

**四个模式**（`policy.py:24` `MODES`）：

| 项目模式 | 打扰策略（`policy.decide`，`policy.py:32-42`） |
| --- | --- |
| `default` | 命中 `deny` → 拒；否则仅命中 `ask` 名单才问（**名单默认空**）→ 命中 `allow` 放行 → 未命中走 `default`（出厂 `allow=["*"]` ⇒ 放行） |
| `auto` | 追加规则：`risk ∈ {write,danger}` → **问** |
| `plan` | `risk != read` → **硬拒**（`policy.py:36`） |
| `bypass` | 不打扰（`__init__.py:145`），**guard 硬规则仍生效** |

**双闸与默认**：
- `guard`（sys，`prepend` 第一道，`guard.py:204-205`）+ `approval`（plugin，`append` 第二道，`__init__.py:106`）。
- 默认：`approval.allow=["*"]`、`ask=[]`（`__init__.py:41-48`）；`guard` 默认 `default="open"`、`task_allow=["*"]`、`risk={}`（`guard.py:34-41`、`profiles.py:70`）。
- `guard` 判 `ask` 时**不调 `next()`，直接短路为拒绝**（`guard.py:198-201`）；`ask` 的"真执行"只归 approval。

---

## 3. 逐维度对照表

| 维度 | Claude Code | 本项目 | 一致性 |
| --- | --- | --- | --- |
| 三态判定 | allow / ask / deny，deny 优先 | allow / ask / deny，deny 优先 | ✅ 一致 |
| 规则系统 | **一套**（allow/ask/deny） | **两套**（guard + approval 各一组） | ❌ 见 D-3 |
| 规则粒度 | 工具 **+ 参数**（`Bash(git status:*)`） | **仅工具名**（fnmatch） | ❌ 见 D-4 |
| 默认姿态 | **先问**（Manual 下写类/Bash/网络都问） | **先放**（allow=["*"]，空转） | ❌ 见 D-2 |
| 模式命名 | default/acceptEdits/plan/auto/dontAsk/bypass | default/plan/auto/bypass | ❌ 见 D-1 |
| 默认模式语义 | `default` = 只放行只读 | `default` = 只问名单内（默认啥都不问） | ❌ 见 D-1 |
| plan 收敛 | 退出 plan 回 Manual/acceptEdits | 批准后切 `auto`（`__init__.py:335`） | ⚠️ 语义近、命名乱 |
| 只读自动放行 | 是（工作目录内） | 是（`risk=="read"` 在 plan 下；default 下靠 allow=["*"]） | ✅ 近似 |
| "不再问"粒度 | 仓库+命令 / 域，可选持久化 | 工具名，仅会话内存 | ❌ 见 D-5 |
| 被拒语义 | 提示用户"改做法" | `denied_by` ∈ {approval,user}，user 拒绝不计连拒 | ✅ 好设计 |
| fail-safe | 超时/中断 = 拒绝 | 超时/无人/异常 = 拒绝 | ✅ 一致 |
| bypass | 显式开启、不落盘 | 不持久化（`__init__.py` + 设计稿 §7） | ✅ 一致 |
| 一键升档 | 提示框 "switch to auto mode" | 仅 per-tool `always` | ❌ 见 D-6 |
| 澄清问询 | `AskUserQuestion` 工具 | `approval.ask_user`（含多选/规格） | ✅ 一致 |

---

## 4. 差异清单（"不对味"之处）

### D-1　模式命名与语义相对 CC **整体对调** ⚠️ 核心

| 直觉 / CC | 本项目实际（`policy.py:32-42`） |
| --- | --- |
| `default` = 常规、**写类都问** | `default` = **只问 `ask` 名单内**（默认空 ⇒ 什么都不问） |
| `auto` = 少问、近乎全放行 | `auto` = **写类全问** |
| `acceptEdits` = "自动接受编辑" | **无此档**；`auto` 名字像它、行为却相反 |

⇒ **项目 `default` ≈ CC `auto`（不问）；项目 `auto` ≈ CC `default`（写类都问）**。名字与行为对调。
设计稿 §15 自称「Claude Code 的 default / plan / acceptEdits / bypassPermissions（按语义对齐，非照搬）」，但**实现用语义相反的 `auto` 替换了 `acceptEdits`**——这是"不对味"的根源。

### D-2　默认姿态与 CC **相反**：CC 先问，本项目先放 ⚠️ 核心

- CC：Manual（默认）下写类 / Bash / 网络 **都问**。
- 项目：`default` 模式下 `approval.allow=["*"]` + `guard.default="open"` ⇒ **开箱什么都不问**。`mcp.skills.skill_bash`（danger）在 `default` 模式直接放行。
- ⇒ 本项目审批插件是"**opt-in 到严格**"，CC 是"**opt-in 到宽松**"——哲学层面镜像。

### D-3　**双套规则系统**，CC 只有一套

- 项目把同义三态拆到两处：`guard.permissions.{task_allow,task_deny,risk.*}` 与 `approval.{allow,deny,ask}`；设计稿明说两者"**不应同时使用**"（`审批插件-设计方案.md:38`）。
- guards 的 `ask` **短路成拒绝**（`guard.py:198-201`），approval 的 `ask` **真问人** ⇒ 同一个词"ask"在两处语义不同。
- CC 只有一套、只评一次、`ask` 永远是真提示。

### D-4　规则粒度只到**工具名**，CC 到**参数**

- CC：`Bash(git diff:*)`、`Read(./.env)`——可表达"允许 `git status`、问 `rm`"。
- 项目：`ask=["Write","Bash","mcp.windows.*"]` 是**工具名 fnmatch**；设计稿 §1.2 把"参数级规则"列为**非目标**。
- ⇒ 缺 CC 权限系统的**主要威力所在**。

### D-5　"总是允许"粒度与持久化更弱

- CC：Bash 按 **仓库 + 命令前缀**持久化、可入 settings；文件修改到会话末。
- 项目：`session_allow/session_deny` 只记**工具名**、仅**会话内内存**（`__init__.py:99`，重启即清）。⇒ 反复重问，且只能整工具放行。

### D-6　缺 `dontAsk` 与"一键升档"

- CC 有 `dontAsk`（本该问的一律拒）——项目对应物是 **provider 级** `auto-deny`（全局开关，非模式）。
- CC 提示框有 "switch to auto mode"；项目只有 per-tool `always`。

### D-7　`plan` 批准后去向命名混乱

- 项目 plan 批准 → `auto`（`__init__.py:335`）；但 `auto` 会问写类，**实际等价 CC 的 normal**，只是名叫 auto。CC 是"退出 plan 回到 Manual/acceptEdits"。

---

## 5. 对齐得好的地方（非全盘否定）

- 三态 `allow/deny/ask` + **deny 优先**——与 CC 语义一致。
- `plan` = 只读子集 + **软硬两层**（可见集裁剪 + 写类硬拒，`policy.py:36`）；"软约束必须配硬约束"的自觉很好。
- `bypass` **不持久化**（设计稿 §7）——与 CC `bypassPermissions` 需显式开启一致。
- **fail-safe**：无人应答 / 超时 / provider 异常 ⇒ 一律拒（`__init__.py:19`）——与 CC"超时/中断即拒绝"一致。
- **被拒语义**：`denied_by` 区分"用户拒"（不计连拒、应换方案）与"策略拒"——比 CC 更细。
- `ask_user` 澄清工具 ≈ CC `AskUserQuestion`（且带多选/分组规格，超出 CC 基线）。
- 工具 `risk` 元数据（read/write/danger）——与 CC 按工具类型分层同构。

---

## 6. 对齐建议方向（仅建议，未实施）

| # | 方向 | 成本 | 说明 |
| --- | --- | --- | --- |
| A-1 | **对齐模式命名与语义** | 低 | 项目 `auto` 改名 `manual`/`normal`（其真实语义）；`default` 直接改为"写类都问"以贴合 CC Manual |
| A-2 | **补 `acceptEdits`** | 低-中 | 文件编辑/常用文件命令免问、Bash 仍问——编码场景主力档 |
| A-3 | **默认姿态收紧** | 低（需拍板） | 与《安全与质量优化方案-2026-10-20》**O-3** 同一决策点，勿重复讨论 |
| A-4 | **合并 guard / approval 规则为单一 allow/ask/deny** | 大 | 消除双闸与"ask 两义"；属结构改 |
| A-5 | **参数级规则** | 大 | 向 CC 看齐的硬骨头；需注册表/匹配器扩展 |
| A-6 | **"总是允许"持久化 + 到参数摘要** | 中 | 至少落到 `data/` 下 settings；粒度到 `工具 + 参数摘要` |

> A-1/A-2 是"低成本、直接消除不对味"的两项，建议优先；A-4/A-5 属大改，另立方案。

---

## 7. 附：依据与来源

**本项目（逐行读码）**：
- `agent/plugins/approval/__init__.py`（模式挂点 `:106`、`_pre_task` `:142-221`、`ask_user` `:341-407`、默认配置 `:41-48`、plan 批准切 auto `:335`）
- `agent/plugins/approval/policy.py:24,32-42`（`MODES` 与判定顺序）
- `agent/plugins/approval/providers.py`（auto-deny/auto-allow/web/cli）
- `agent/plugins/approval/pending.py`（会话分桶的待答表）
- `agent/sys/guard.py:34-41,129-163,198-201`（默认策略、三态、ask 短路）
- `agent/profiles.py:70`（默认 `permissions.default="open"`）
- `docs/plugins/approval/审批插件-设计方案.md`（v1.5，§3 模式定义、§15 参考、§16 解耦契约）
- `docs/reports/plugins/人在循环-审批插件进度调研-2026-09-27.md`（挂点与 ask 归属的复核）

**Claude Code（官方文档，2026 当前版）**：
- 权限模型与规则：<https://docs.claude.com/en/docs/claude-code/permissions>
- 权限模式（六档表）：<https://docs.claude.com/en/docs/claude-code/permission-modes>
- 设置与优先级：<https://docs.claude.com/en/docs/claude-code/settings>

> 注：CC 侧为其官方文档功能描述；本项目侧为代码实测。两者版本时点不同（CC 迭代较快），具体默认值以后续官方文档为准。
