# Agent

slim Cordis 运行时 + 系统层插件 + 可管理目录插件（everything-is-a-plugin）。独立可离线自举，零外部引擎依赖。

- 内核：`agent/`（`agent.api` 唯一出口）
- 设计文档（工程根）：《agent内部架构-开发文档.md》《agent记忆架构-方案.md》《记忆插件-能力缺口分析.md》《agent通用能力-完善路线.md》
- 历史归档（**不作为工作文件参考**）：`doc/` —— 专用领域设计 / 接口接入设计 / 旧计划与路线图，说明见 `doc/README.md`

**已完成**：内核（`agent.api`）· 系统插件（events / loop / llm / tasks / storage / budget / guard）· 双驱动（`loop` 任务驱动 + `chat` 会话驱动）· 真实 DeepSeek provider · 目录插件管理（引擎式 desired/actual/delta，插件页可启用 / 停用 / 卸载）· 记忆插件 `plugins.memory`（LLM 提取[过 guard/budget] + 打分衰减 + recall/context 注入；**目录插件形态，默认启用**）· MCP 工具桥 `plugins.windows_mcp`。**68 项离线测试全绿。**

**工具调用（tool calling）**：任务注册表提供 `catalog()` 动态工具目录；chat 新增 `tool_call` 意图——规划器从工具目录里选工具、执行仍过 guard 白名单。前端有"🛠️ 工具"视图。

**windows-mcp-server 封装**：`agent/sys/mcp_client.py`（通用 MCP stdio 客户端，零依赖）+ `agent/plugins/windows_mcp/`（**可管理插件**：插件页停用/启用/卸载）。**已在本机装好并实测连通**：默认命令 `uvx windows-mcp serve`（自动用 Python 3.14 起子进程），20 个工具（App/PowerShell/FileSystem/Screenshot/Click/Type/Registry/…）自动挂进工具目录，可经对话调用。首次启动需下载依赖，`timeout_s` 默认 120s 可配。

> ⚠️ 这些是**高危工具**（任意 PowerShell/注册表/删文件/杀进程/键鼠）。工具调用仍过 guard 白名单，默认 open；建议在 `config["agent"]["permissions"]["task_deny"]` 里按需收紧 `mcp.windows.PowerShell` / `Registry` / `Process` 等。

**短期记忆（会话历史）**：`agent_messages` 转写（session_id + role/content 时序），每轮把最近 ~6 轮历史 + 长期记忆召回块一起喂给模型——问"我上一句说了什么"这类问题能正确答出；网页顶部"新会话"清空当前会话。

> 前端**不特化领域**：是一个通用 agent harness（会话 / 事件 / 权限 / 插件 / 记忆 / 工具 视图 + step 轨迹）；领域能力通过**挂载领域插件或工具**注入（现成示例：windows-mcp 工具桥）。

运行：
```bash
python -m agent.run.chat                 # 网页 harness（默认 deepseek，127.0.0.1:8720）
python -m agent.run.chat --fixture       # 离线夹具（不动真 API）
python -m agent.run.chat --once "你好"    # 无头跑一次后退出
```

凭据：真实 key 放 `local_secrets.json`（已在 `.gitignore`，绝不入库）；或 `env DEEPSEEK_API_KEY`。线上支持模型名 `deepseek-flash` / `deepseek-v4-pro`（`deepseek_v4_flash` 为 harness 内部名，线上 API 不收）。