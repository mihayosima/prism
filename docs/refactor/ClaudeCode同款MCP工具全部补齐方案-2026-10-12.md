# Claude Code 同款 MCP 工具「全部补齐」实施方案

编制时间：2026-10-12 00:43（GMT+8）
更新记录：

| 时间 | 版本 | 变更摘要 |
| --- | --- | --- |
| 2026-10-12 00:43 | v1 | 结合实际代码复核现状（含纠正一处此前的误判），给出 Follow 三件套 + 阶段 3/4/5 的分批实施方案、护栏设计与验收清单 |
| 2026-10-14 01:50 | v1.1 | **路径勘误（实施前必读）**：本文所引 `agent/run/skills_mcp_adapter.py` 已归位 `agent/plugins/skills/adapter.py`；`agent/core/skill.py` / `skills_catalog.py` / `skills_loader.py` 已归位 `agent/plugins/skills/` 下 `model.py` / `catalog.py` / `loader.py`（详见 `agent内部架构-开发文档.md` 2026-10-14 更新记录）。后续按本文施工时，所有旧路径一律按新路径执行；方案的技术判断（工具面、护栏、验收）不受影响 |

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
| `mcp.playwright.*`(25) / `mcp.windows.*`(20) / `mcp.skills.*`(2) | 三个 MCP 桥 |

**结论：本仓无任何内置 shell / 文件读写工具**。命令执行与文件编辑目前只能借道 `windows_mcp`（外部 MCP，20 个工具）。
因此 Follow 三件套是**真增量**，不是"重复造轮子"——这一点影响下面的工作量评估与护栏设计。

---

## 1. 现状与缺口（全部经代码核实）

### 1.1 适配器现状
`agent/run/skills_mcp_adapter.py`：`_HANDLERS`(:97) 仅 `skill_list` / `skill_view`；`_tools()`(:100) 只声明这两个。

### 1.2 缺口清单

| # | FastSkills 工具 | 现状 | 缺口性质 |
| --- | --- | --- | --- |
| A | `view` 的 `view_range` 分段读 | ❌ | 只读增强（大 SKILL.md 无法切片） |
| B | `bash_tool`（cwd=`{baseDir}`，超时 120s） | ❌ | **执行能力**（社区技能跑 scripts/ 全靠它） |
| C | `file_create` | ❌ | 写能力 |
| D | `str_replace` | ❌ | 写能力 |
| E | 阶段 3：索引块格式对齐 | ⏳ | `skills_catalog.py:182 index_block` |
| F | 阶段 4：管理面展示捆绑资源 | ⏳ | `Skill` 无 `resources` 字段 |
| G | 阶段 5：社区技能直装 | ⏳ | 无 `/api/skills/install` |
| — | `search_cloud_skills` / `install_cloud_skill`（云注册表） | ❌ | **本方案不做**（外部依赖、运维增强，与"同款工具"正交） |

### 1.3 顺带发现的真实缺陷：MCP 工具风险级全被标成 `write`

- `sys/tasks.py:186` `register(..., risk: str = "write")` 默认写级；
- 三个 MCP 桥注册时**都没传 `risk`**（`plugins/skills_mcp/__init__.py:134`、`playwright:157`、`windows:144`）。

后果：`mcp.skills.skill_list` 这种纯读工具风险级也是 `write` ⇒ 在 guard 的 `risk.ask` 策略下**读技能也要问人**，plan 模式下会被裁掉。这是"只读适配器"名不副实的漏洞，**必须在补执行能力前先修**（否则新增的 `skill_bash` 与 `skill_list` 风险级相同，护栏形同虚设）。

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

### 2.2 三道硬约束（执行类工具）

1. **路径不逃逸**
   - `skill_bash(cwd?)`：默认值 = 该技能 `basedir`；传入值 `Path(cwd).resolve()` 后必须 `is_relative_to(workdir)`，否则 `isError`。
   - `skill_file_create` / `skill_str_replace(path)`：同上，必须落在 `workdir` 内。
   - `workdir` = 配置 `workdir`，默认 `<data_dir>/skills_workspace`（新建），**不**默认指向整个 data 目录。
2. **执行默认关闭**：配置 `allow_exec: false`（默认）。关闭时 `tools/list` **不返回** B/C/D 三个工具（比"返回但拒绝"更省 token、更不易误触）；开启后桥插件经环境变量 `SKILLS_ALLOW_EXEC=1` + `SKILLS_WORKDIR=<path>` 注入子进程。
3. **超时 + 截断**：`timeout_s` 默认 120（可配 `exec_timeout_s`，上限 600）；stdout+stderr 合并后 `clip_text` 到 `output_max_chars`（默认 20000），并带回 `truncated` 标记。

### 2.3 风险级标注（修 1.3 缺陷）

- 适配器 `_tools()` 每个工具加自定义字段 **`"risk"`**（不在 `inputSchema` 内 ⇒ 标准 MCP 客户端会忽略，零兼容成本）。
  - `skill_list` / `skill_view` → `"read"`
  - `skill_bash` → `"danger"`
  - `skill_file_create` / `skill_str_replace` → `"write"`
- 桥插件 `plugins/skills_mcp/__init__.py:134` 注册处改为 `risk=t.get("risk") or "write"`，并把 `t["risk"]` 一并写进 `description` 前缀便于排查。
- **本轮只改 skills 桥**；playwright / windows 桥的同款问题另开议题（属既有行为变更，需单独回归）。

### 2.4 沙箱的诚实说明

`plugins/sandbox/__init__.py` 已明示：**无任何进程级隔离实现，所有档位 `enforcement` 恒 `none`**。
故本方案**不宣称**"沙箱化执行"——隔离强度仍为 `none`，安全边界是「路径不逃逸 + 默认关闭 + risk=danger 走审批」，不是进程隔离。文档与前端文案都不得写成"沙箱执行"。

---

## 3. 分批实施计划

每批独立可验收、可回滚；前一批绿了再开下一批。

### P0 —— 风险级标注（先修，约 0.5 批）
| 落点 | 改动 |
| --- | --- |
| `run/skills_mcp_adapter.py:_tools()`(:100) | 两个工具加 `"risk": "read"` |
| `plugins/skills_mcp/__init__.py:134` | `register(..., risk=t.get("risk") or "write")` |
| `agent/tests/test_skills_mcp_adapter.py` | 断言 `_tools()` 含 risk；断言桥注册时传了 risk（monkeypatch `ctx.tasks.register`） |

验收：`/api/tools` 里 `mcp.skills.skill_list` 风险级 = read；plan 模式下仍可调。

### P1 —— 只读增强 + 阶段 3（索引块格式）
| 落点 | 改动 |
| --- | --- |
| `_tools()` / `_HANDLERS` | `skill_view` 增 `view_range: [start, end]`（1-based，含端点；`-1` 表示到末尾，对齐 FastSkills `view` 语义） |
| `core/skills_catalog.py:182 index_block` | 输出改为 Claude Code 风 `- name — <description 多行折叠为单行，_slim 截断>`；保留「读/写」标记与预算截断（不改 `max_chars` 语义） |
| `core/skills_catalog.py:42 _slim` | 复用，但索引里 **description 允许更长**（触发说明是命中依据）⇒ 新增参数 `limit`，索引用 200、列表用 120 |

验收：`code-review` 技能索引行显示为 `code-review — 对改动做评审…（含 Use when 触发语）`；`skill_view` 传 `view_range=[1,3]` 只回前三行。

### P2 —— Follow 三件套（核心批次）
| 落点 | 改动 |
| --- | --- |
| `run/skills_mcp_adapter.py` | 新增 `_tool_bash` / `_tool_file_create` / `_tool_str_replace`，注册进 `_HANDLERS`；`_tools()` 按 `SKILLS_ALLOW_EXEC` 决定是否返回这三个 |
| 同上 | 新增 `_resolve_in(base: Path, candidate: str) -> Path`：resolve + `is_relative_to` 校验，逃逸抛 `ValueError` |
| 同上 | 新增 `_run_cmd(cmd, cwd, timeout_s)`：`asyncio.create_subprocess_shell`；超时按 FastSkills 语义**返回部分输出 + isError**（不静默成功）；输出解码 utf-8 失败回落 `mbcs`（本机 Windows 控制台 GBK 的既有坑） |
| `plugins/skills_mcp/__init__.py:_spawn`(:99) | 注入 `SKILLS_ALLOW_EXEC` / `SKILLS_WORKDIR` / `SKILLS_EXEC_TIMEOUT_S` |
| `core/config_schema.py` | 无需改键（`skills_mcp` 节登记为 `_any`），只需在实现里给默认值 |

工具签名（草稿）：
```
skill_bash(command: str, cwd?: str, timeout_s?: int)            # cwd 默认 = 该技能 basedir；未指定技能时 = workdir
skill_file_create(path: str, content: str)                       # 父目录自动建；已存在则 isError（不静默覆盖）
skill_str_replace(path: str, old: str, new: str, count?: int)    # old 必须唯一命中（count 缺省时），否则 isError
```
验收：临时 `data/skills/echo/SKILL.md`，真子进程跑 `skill_bash(command="echo hi")` 返回 `hi`；`cwd="../../"` 被拒；超时用例返回 isError；`skill_list` 仍不含正文。

### P3 —— 阶段 4：管理面展示捆绑资源
| 落点 | 改动 |
| --- | --- |
| `core/skill.py` | `Skill` 增 `resources: list[str] = field(default_factory=list)`（相对 basedir 的相对路径） |
| `core/skills_loader.py:135 discover_skills` | 目录式技能扫 `scripts/ reference/ templates/ examples/ assets/` 一级文件（限深度 2、限条数 50，防大目录拖慢启动） |
| `core/skills_catalog.py:84 _card` | 增 `basedir` / `resources` |
| `run/skills_routes.py:40 _list_skills` | 透传（无需改签名）；`_get_skill`(:48) 已返回整卡 |
| `web/src/views/SkillsView.vue` + `skills.logic.ts` | 详情区展示「捆绑资源」列表；**改完必须 `npm run build` 重建 `web/dist` 并查页面**（既有教训：不重建则页面看不到新产物） |

### P4 —— 阶段 5：社区技能直装
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
| `test_services_contract.py` | AST 守卫不变：适配器不得 import `sys.tasks` / `plugins.react`；新增断言 `agent/run/skills_mcp_adapter.py` 只走 `agent.api` |
| 新增 `test_skills_install.py`（P4） | Zip-Slip 拒绝；非白名单 host 拒绝；缺 `SKILL.md` 拒绝；正常解包落盘 |

回归纪律（沿用既有约定）：
- 全量 pytest 必设 `CODEBUDDY_SAFE_DELETE_SANDBOX=0`，`--basetemp` 写 Windows 路径、独立目录；
- 分批跑（每批 4 文件左右），避免 MemoryError；
- 每批结束真机：`uvicorn` 起服务 + `/api/tools` 确认 `mcp.skills.*` 工具数与风险级；改过前端则重建 `dist` 并查页面。

已知基线失败（与本次无关，勿重复排查）：`test_react_user_reject_not_counted`、`test_state_key_literals_only_in_state_keys`。

---

## 5. 风险与边界

- **不宣称沙箱**：`enforcement` 恒 `none`；安全靠路径约束 + 默认关 + `risk=danger` 审批。
- **Windows 编码**：子进程输出解码 utf-8 → 失败回落 `mbcs`，禁止 `UnicodeDecodeError` 冒泡把子进程打死（适配器 `main()` 是逐行循环，异常要就地兜住）。
- **不放宽既有两个桥**：playwright / windows 桥的 `risk` 缺失问题本轮不动。
- **`data/` 与 `web/dist` 不入版本库**：`data/agent_plugin_state.json`（enabled 列表）、示例技能、`web/dist` 都是每机独立的；新机需自行补，方案文档与提交信息已注明。
- **云注册表不做**：避免引入外部服务依赖与账号体系。

---

## 6. 待你拍板

1. **P2 默认开关**：`allow_exec` 默认 `false`（推荐，需显式开）还是默认 `true`（开箱即用，但一装上就带执行面）？
2. **`workdir` 默认值**：`<data_dir>/skills_workspace`（推荐，独立目录）还是整个 `data` 目录（更方便但对 data 全局可写）？
3. **`skill_bash` 是否需要命令黑名单**（如 `rm -rf` / `format` / `del /s`）？还是只靠 `risk=danger` + approval 问人？
4. **P4 社区直装**是否本轮做（涉及网络出站与解包，风险面最大）？
5. **P0 是否顺手覆盖 playwright / windows 桥**（统一修 `risk`）？
