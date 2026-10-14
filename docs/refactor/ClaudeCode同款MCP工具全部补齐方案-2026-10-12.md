# Claude Code 同款 MCP 工具「全部补齐」实施方案

编制时间：2026-10-12 00:43（GMT+8）
更新记录：

| 时间 | 版本 | 变更摘要 |
| --- | --- | --- |
| 2026-10-12 00:43 | v1 | 结合实际代码复核现状（含纠正一处此前的误判），给出 Follow 三件套 + 阶段 3/4/5 的分批实施方案、护栏设计与验收清单 |
| 2026-10-14 01:50 | v1.1 | **路径勘误（实施前必读）**：本文所引 `agent/run/skills_mcp_adapter.py` 已归位 `agent/plugins/skills/adapter.py`；`agent/core/skill.py` / `skills_catalog.py` / `skills_loader.py` 已归位 `agent/plugins/skills/` 下 `model.py` / `catalog.py` / `loader.py`（详见 `agent内部架构-开发文档.md` 2026-10-14 更新记录）。后续按本文施工时，所有旧路径一律按新路径执行；方案的技术判断（工具面、护栏、验收）不受影响 |
| 2026-10-14 01:45 | v1.2 | **① P0 已实施**（提交 `fed1dcf`，2026-10-13）：skills 桥注册改传 risk，`skill_list`/`skill_view` 按 `_READ_ONLY_TOOLS` 白名单标 `read`、其余沿用 `write`（§1.3/§2.3/P0 节已同步标注）。**② 正文路径与行号按归位后代码全面校正**（适配器/目录/loader 新路径 + 真实行号）。**③ §2.4/§5 沙箱状态勘误**：`enforcement` 不再恒 `none`——分级沙箱 P4/P5 已落地（wsl2 进程级 / container 容器级后端），强度由真实后端驱动、off 档才是 `none`（详见 `分级沙箱对齐ClaudeCode方案-2026-10-13.md` v1.6）；本方案"路径不逃逸 + 默认关闭 + risk 审批"的宿主侧护栏设计不受影响，但**不得**再宣称"无任何隔离实现"。**④ §4 契约断言行改写**：适配器已与技能目录同包（包内相对导入合法），禁限是跨子系统直连（`sys.tasks`/`plugins.react`），不再是"只走 agent.api" |

| 2026-10-14 15:44 | v1.3 | **B1–B3 已实施（策略 A，用户拍板）**：`allow_exec` 默认 **true**、`skills_mcp` 桥 `enabled` 默认 **true** ⇒ 开箱即用完全权限（直接操作电脑，无路径 jail）；`skill_view` 增 `view_range`、`index_block` 改 Claude Code 风 ``- name — 描述``；新增 Follow 三件套 `skill_bash`(danger)/`skill_file_create`(write)/`skill_str_replace`(write)，受 `SKILLS_ALLOW_EXEC` 门控（关 ⇒ 工具根本不出现，fail-closed）；`Skill.resources` + loader 资源扫描（深度2/最多50/正斜杠）+ `skill_view` 返回资源。**B4 社区直装搁置**（供应链风险）。命令黑名单机制保留、默认空。**§2.2「路径不逃逸」约束作废**（按用户"完全权限"诉求），§6 待拍板项全部收敛。 |

> 前情：已提交 `2bcd35f`（Skill 插件 + 只读 MCP 适配器 + 格式对齐阶段 0/1/2）。
> 本方案在此基础上补 **FastSkills MCP server 的完整工具面** 与 **对齐阶段 3/4/5**。
> 相关文档：`Skill与MCP-Tool更新方案-2026-10-11.md`、`Skill对齐AgentSkills标准-基于FastSkills代码研究-2026-10-11.md`。

---

## 0. 先纠正一处误判（重要）

上一轮我说「`bash` / `file_create` / `str_replace` 的等价物本仓已经有了（fs/shell 工具）」——**这句是错的，已核实证伪**。

实际工具面（`grep -rn "register(ctx" agent`，排除测试）：

| 工具 | 来源 |
| --- | --- |
| `agent.ping` | `sys/tasks.py:329` |
| `approval.*`（ask / plan） | `plugins/approval/__init__.py:113,284` |
| `agent.delegate` | `plugins/subagent/__init__.py:54` |
| `todo.write` / `todo.list` | `plugins/todo/__init__.py:41,60` |
| `mcp.playwright.*`(25) / `mcp.windows.*`(20) / `mcp.skills.*`(5) | 三个 MCP 桥（skills 现含 skill_list/view + Follow 三件套） |

**结论：本仓无任何内置 shell / 文件读写工具**。命令执行与文件编辑目前只能借道 `windows_mcp`（外部 MCP，20 个工具）。
因此 Follow 三件套是**真增量**，不是"重复造轮子"——这一点影响下面的工作量评估与护栏设计。

---

## 1. 现状与缺口（全部经代码核实）

### 1.1 适配器现状
`agent/plugins/skills/adapter.py`：`_HANDLERS`(:103) 仅 `skill_list` / `skill_view`；`_tools()`(:106) 只声明这两个。

### 1.2 缺口清单

| # | FastSkills 工具 | 现状 | 缺口性质 |
| --- | --- | --- | --- |
| A | `view` 的 `view_range` 分段读 | ✅ 已实施（2026-10-14） | 只读增强 |
| B | `skill_bash`（超时 120s，完全权限） | ✅ 已实施（2026-10-14，risk=danger） | **执行能力** |
| C | `skill_file_create` | ✅ 已实施（2026-10-14） | 写能力 |
| D | `skill_str_replace` | ✅ 已实施（2026-10-14） | 写能力 |
| E | 阶段 3：索引块格式对齐（Claude Code 风） | ✅ 已实施（2026-10-14，`catalog.index_block`） | 索引可读性 |
| F | 阶段 4：管理面展示捆绑资源 | ✅ 已实施（2026-10-14，`Skill.resources`+loader 扫描） | B3 资源发现 |
| G | 阶段 5：社区技能直装 | ⏸ 本轮搁置（B4，供应链风险） | 无 `/api/skills/install` |
| — | `search_cloud_skills` / `install_cloud_skill`（云注册表） | ❌ | **本方案不做**（外部依赖、运维增强，与"同款工具"正交） |

### 1.3 顺带发现的真实缺陷：MCP 工具风险级全被标成 `write`

- `sys/tasks.py:186` `register(..., risk: str = "write")` 默认写级；
- 三个 MCP 桥注册时**都没传 `risk`**（`plugins/skills_mcp/__init__.py:190`、`playwright:159`、`windows:146`）。

后果：`mcp.skills.skill_list` 这种纯读工具风险级也是 `write` ⇒ 在 guard 的 `risk.ask` 策略下**读技能也要问人**，plan 模式下会被裁掉。这是"只读适配器"名不副实的漏洞，**必须在补执行能力前先修**（否则新增的 `skill_bash` 与 `skill_list` 风险级相同，护栏形同虚设）。

> ✅ **已修（P0，提交 `fed1dcf`，2026-10-13）**：skills 桥注册处改传 risk——`_READ_ONLY_TOOLS` 白名单内的工具标 `read`、其余沿用 `write`（保守：不因猜错而放行）。playwright / windows 两桥**仍未修**（各自 25/20 个工具，逐一判定读写语义前不敢批量标 read，列为待办）。

`RISKS = ("read", "write", "danger")`（`sys/tasks.py:97`）。

---

## 2. 设计主线

### 2.1 执行放哪：适配器子进程内执行（推荐），护栏留在宿主侧

链路是这样走的（已核实 `sys/tasks.py:207-229`）：

```
模型 → mcp.skills.skill_bash（注册的 task）
     → TaskRegistry.invoke：durable 落 pre_execute
     → events.waterfall(TASKS_PRE_EXECUTE) ⇒ sys/guard 裁决 risk/域/targets
     → plugins/approval 真问人（如需）
     → handler → McpStdioClient.call_tool → 适配器子进程执行
```

**关键**：准入判决发生在**宿主侧 task 调用前**，子进程只是"干活的手"。所以把执行放进适配器**不会绕过** guard / approval。反过来若放在桥插件 handler 里，反而要自己重建一套裁决。

⇒ 采用：适配器内 `asyncio.create_subprocess_shell`，cwd 锁定技能目录。

### 2.2 三道约束（执行类工具，2026-10-14 按策略 A 收口）

> ⚠️ **重大变更**：原"路径不逃逸"硬约束**已作废**——用户明确要求"完全权限、直接操作电脑"，
> 故 Follow 三件套**不受 workdir jail 限制**（shell `cd` 本就可逃逸，jail 仅是假象）。安全靠
> 分层：risk 分级（skill_bash=danger）+ 语义护栏（超时/截断/不静默覆盖/唯一替换）+ 可选命令黑名单，
> 而非路径围栏。

1. **执行默认开启（策略 A）**：`allow_exec` 与 `skills_mcp.enabled` **均默认 true** ⇒ 开箱即用完全权限。
   关闭时 `tools/list` **不返回** B/C/D 三个工具（fail-closed：不可见即不可调用，不靠 guard 拦）。
   桥经环境变量 `SKILLS_ALLOW_EXEC=1` + `SKILLS_WORKDIR=<用户主目录>` + `SKILLS_EXEC_TIMEOUT_S` + `SKILLS_BASH_DENY` 注入子进程。
2. **`workdir` 仅作初始目录、不构成 jail**：默认 = `Path.home()`（用户主目录）；shell `cd` 可逃逸，符合"直接操作电脑"诉求。
3. **超时 + 截断**：`SKILLS_EXEC_TIMEOUT_S` 默认 120（可配）；stdout+stderr 合并后截断到 20000 字符并附提示。
4. **命令黑名单（机制在、默认空）**：`SKILLS_BASH_DENY` 逗号分隔；默认空 ⇒ 无拦截，完全权限；需收紧时往配置加命令片段即可。

### 2.3 风险级标注（修 1.3 缺陷）

- 适配器 `_tools()` 每个工具加自定义字段 **`"risk"`**（不在 `inputSchema` 内 ⇒ 标准 MCP 客户端会忽略，零兼容成本）。
  - `skill_list` / `skill_view` → `"read"`
  - `skill_bash` → `"danger"`
  - `skill_file_create` / `skill_str_replace` → `"write"`
- 桥插件 `plugins/skills_mcp/__init__.py` 注册处取 risk：`risk="danger" if name in _DANGER_TOOLS else ("read" if name in _READ_ONLY_TOOLS else "write")`（✅ 已实施 2026-10-14：`_DANGER_TOOLS={"skill_bash"}`，Follow 三件套落地、`skill_bash` 标 `danger`）。
- **本轮只改 skills 桥**；playwright / windows 桥的同款问题另开议题（属既有行为变更，需单独回归）。

### 2.4 沙箱的诚实说明

`plugins/sandbox/__init__.py` 的隔离强度由**真实后端**驱动（分级沙箱 P4/P5 后）：`backend=off`（当前默认）⇒ `enforcement=none`；`wsl2`（bwrap+netns，partial）/ `container`（Docker，full）⇒ 按后端真实能力兑现，`status()`/审计如实报 `backend`/`enforced_by`/`portable`（详见 `分级沙箱对齐ClaudeCode方案-2026-10-13.md` v1.6）。

故本方案的安全边界表述分两层：**宿主侧**「路径不逃逸 + 默认关闭 + risk=danger 走审批」始终成立；**进程隔离**则取决于沙箱配置——默认 off 下不得宣称"沙箱化执行"，配了 wsl2/container 才可宣称对应强度。文档与前端文案都不得越过配置的诚实边界。

---

## 3. 分批实施计划

每批独立可验收、可回滚；前一批绿了再开下一批。

### P0 —— 风险级标注（✅ 已实施，提交 `fed1dcf`，2026-10-13）
| 落点 | 改动 |
| --- | --- |
| `plugins/skills_mcp/__init__.py:190`（注册处） | 注册时传 risk：`risk="read" if name in _READ_ONLY_TOOLS else "write"`（白名单 `skill_list`/`skill_view`） |
| `plugins/skills_mcp/__init__.py`（`_READ_ONLY_TOOLS`） | 新增只读工具白名单常量（附判定依据注释） |
| `agent/tests/test_skills_mcp_adapter.py` | 断言 `_tools()` 含 risk；断言桥注册时传了 risk（monkeypatch `ctx.tasks.register`） |

验收（已过）：`/api/tools` 里 `mcp.skills.skill_list` 风险级 = read；plan 模式下仍可调。

### P1 —— 只读增强 + 阶段 3（索引块格式）✅ 已实施（2026-10-14）
| 落点 | 改动 |
| --- | --- |
| `_tools()` / `_HANDLERS` | `skill_view` 增 `view_range: [start, end]`（1-based，含端点；`-1` 表示到末尾，对齐 FastSkills `view` 语义） |
| `plugins/skills/catalog.py:187 index_block` | 输出改为 Claude Code 风 `- name — <description 多行折叠为单行，_slim 截断>`；保留「读/写」标记与预算截断（不改 `max_chars` 语义） |
| `plugins/skills/catalog.py:47 _slim` | 复用，但索引里 **description 允许更长**（触发说明是命中依据）⇒ 新增参数 `limit`，索引用 200、列表用 120 |

验收：`code-review` 技能索引行显示为 `code-review — 对改动做评审…（含 Use when 触发语）`；`skill_view` 传 `view_range=[1,3]` 只回前三行。

### P2 —— Follow 三件套（核心批次）✅ 已实施（2026-10-14，策略 A）
| 落点 | 改动 |
| --- | --- |
| `plugins/skills/adapter.py` | 新增 `_tool_bash` / `_tool_file_create` / `_tool_str_replace`，注册进 `_HANDLERS`；`_tools()` 按 `SKILLS_ALLOW_EXEC` 决定是否返回这三个 |
| 同上 | 新增 `_resolve_in(base: Path, candidate: str) -> Path`：resolve + `is_relative_to` 校验，逃逸抛 `ValueError` |
| 同上 | 新增 `_run_cmd(cmd, cwd, timeout_s)`：`asyncio.create_subprocess_shell`；超时按 FastSkills 语义**返回部分输出 + isError**（不静默成功）；输出解码 utf-8 失败回落 `mbcs`（本机 Windows 控制台 GBK 的既有坑） |
| `plugins/skills_mcp/__init__.py:_spawn`(:140) | 注入 `SKILLS_ALLOW_EXEC` / `SKILLS_WORKDIR` / `SKILLS_EXEC_TIMEOUT_S` |
| `core/config_schema.py` | 无需改键（`skills_mcp` 节登记为 `_any`），只需在实现里给默认值 |

工具签名（草稿）：
```
skill_bash(command: str, cwd?: str, timeout_s?: int)            # cwd 默认 = 该技能 basedir；未指定技能时 = workdir
skill_file_create(path: str, content: str)                       # 父目录自动建；已存在则 isError（不静默覆盖）
skill_str_replace(path: str, old: str, new: str, count?: int)    # old 必须唯一命中（count 缺省时），否则 isError
```
验收：临时 `data/skills/echo/SKILL.md`，真子进程跑 `skill_bash(command="echo hi")` 返回 `hi`；`cwd="../../"` 被拒；超时用例返回 isError；`skill_list` 仍不含正文。

### P3 —— 阶段 4：管理面展示捆绑资源 ✅ 已实施（2026-10-14，B3）
| 落点 | 改动 |
| --- | --- |
| `plugins/skills/model.py` | `Skill` 增 `resources: list[str] = field(default_factory=list)`（相对 basedir 的相对路径） |
| `plugins/skills/loader.py:138 discover_skills` | 目录式技能扫 `scripts/ reference/ templates/ examples/ assets/` 一级文件（限深度 2、限条数 50，防大目录拖慢启动） |
| `plugins/skills/catalog.py:89 _card` | 增 `basedir` / `resources` |
| `run/skills_routes.py:42 _list_skills` | 透传（无需改签名）；`_get_skill`(:50) 已返回整卡 |
| `web/src/views/SkillsView.vue` + `skills.logic.ts` | 详情区展示「捆绑资源」列表；**改完必须 `npm run build` 重建 `web/dist` 并查页面**（既有教训：不重建则页面看不到新产物） |

### P4 —— 阶段 5：社区技能直装 ⏸ 本轮搁置（B4，供应链风险，用户拍板）
| 落点 | 改动 |
| --- | --- |
| `run/skills_routes.py` | 新增 `POST /api/skills/install {url, name?}`：下载（stdlib `urllib`）→ 解包（zip/tar）→ 校验根下存在 `SKILL.md` → 落到 `data/skills/<name>/` → 重载目录 |
| 同上 | SSRF 面收敛：仅 `https`；host 白名单默认 `github.com` / `raw.githubusercontent.com` / `codeload.github.com`（可配 `install_allow_hosts`）；解包做 **Zip-Slip** 校验（成员路径不得逃逸） |
| 默认关闭 | 配置项 `agent.skills.install_enabled`（默认 false） |
| 前端 | SkillsView 增「从 URL 安装」入口（P3 之后做） |

不做：SkillsMP 云搜索（`search_cloud_skills`）——外部依赖 + 运维增强，与"同款工具"正交。

---

## 4. 测试与回归

新增 / 修改用例（挂在 `agent/tests/`）：

| 文件 | 新增要点 |
| --- | --- |
| `test_skills_mcp_adapter.py`（现 18 项） | risk 字段；`view_range` 切片；路径穿越 3 例（`../`、绝对路径、workdir 外）；命令超时；输出截断；`allow_exec=false` 时 `tools/list` 不含三件套；真子进程 E2E（临时 `data_dir`） |
| `test_skill_plugin.py`（现 20 项） | `resources` 发现；`index_block` 新格式；`_slim(limit)` 参数 |
| `test_services_contract.py` | AST 守卫不变：适配器（`plugins/skills/adapter.py`）不得 import `sys.tasks` / `plugins.react`（跨子系统直连）；包内相对导入（`skills.catalog` 等）合法——技能系统内部相依是设计意图，不在禁限 |
| 新增 `test_skills_install.py`（P4） | Zip-Slip 拒绝；非白名单 host 拒绝；缺 `SKILL.md` 拒绝；正常解包落盘 |

回归纪律（沿用既有约定）：
- 全量 pytest 必设 `CODEBUDDY_SAFE_DELETE_SANDBOX=0`，`--basetemp` 写 Windows 路径、独立目录；
- 分批跑（每批 4 文件左右），避免 MemoryError；
- 每批结束真机：`uvicorn` 起服务 + `/api/tools` 确认 `mcp.skills.*` 工具数与风险级；改过前端则重建 `dist` 并查页面。

已知基线失败（与本次无关，勿重复排查）：`test_react_user_reject_not_counted`、`test_state_key_literals_only_in_state_keys`、`test_budget_*`×2、`test_work_memory`×1（均为 HEAD 基线既有，2026-10-14 全量回归复核确认）。

---

## 5. 风险与边界

- **不宣称沙箱**：隔离强度由 `backend` 配置驱动（off ⇒ `none`；wsl2 ⇒ partial；container ⇒ full，详见分级沙箱方案 v1.6）；宿主侧安全靠路径约束 + 默认关 + `risk=danger` 审批，两层边界都不得越过配置的诚实边界对外表述。
- **Windows 编码**：子进程输出解码 utf-8 → 失败回落 `mbcs`，禁止 `UnicodeDecodeError` 冒泡把子进程打死（适配器 `main()` 是逐行循环，异常要就地兜住）。
- **不放宽既有两个桥**：playwright / windows 桥的 `risk` 缺失问题本轮不动。
- **`data/` 与 `web/dist` 不入版本库**：`data/agent_plugin_state.json`（enabled 列表）、示例技能、`web/dist` 都是每机独立的；新机需自行补，方案文档与提交信息已注明。
- **云注册表不做**：避免引入外部服务依赖与账号体系。

---

## 6. 待你拍板（已全部收敛，2026-10-14）

1. **P2 默认开关** → **策略 A**：`allow_exec` 默认 `true`、`skills_mcp.enabled` 默认 `true`，开箱即用完全权限（直接操作电脑）。
2. **`workdir` 默认值** → 用户主目录 `Path.home()`，**不构成 jail**（shell `cd` 可逃逸），契合"完全权限"。
3. **`skill_bash` 命令黑名单** → 机制保留（`SKILLS_BASH_DENY`），**默认空**（无拦截）；需收紧时往配置加命令片段。
4. **P4 社区直装** → **搁置**（供应链风险：下载即执行，本轮不做）。
5. **P0 覆盖 playwright / windows 桥 risk** → 本轮未做（既有行为变更，需单独回归；列为待办）。
