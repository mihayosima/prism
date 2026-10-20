# Agent

[![CI](https://github.com/mihayosima/prism/actions/workflows/ci.yml/badge.svg)](https://github.com/mihayosima/prism/actions/workflows/ci.yml)

slim Cordis 运行时 + 系统层插件 + 可管理目录插件（everything-is-a-plugin）。独立可离线自举，零外部引擎依赖。

- 内核：`agent/`（`agent.api` 唯一出口）
- 设计文档（`docs/`，**总索引见 `docs/README.md`**）：《agent内部架构-开发文档.md》《agent记忆架构-方案.md》《记忆插件-能力缺口分析.md》《agent通用能力-完善路线.md》《ReAct插件-设计方案.md》《审批插件-设计方案.md》《B5B6-流式与取消-设计方案.md》
- `docs/` 按**主题域**（`architecture` `plugins` `memory` `context` `sandbox` `spec` `roadmap`）+ **`reports/`（带日期的评估快照）** 组织；早期 `doc/` 归档已清空删除。整理说明见 [`docs/文档迁移清单-2026-10-20.md`](docs/文档迁移清单-2026-10-20.md)

**已完成**：内核（`agent.api`）· 系统插件（events / loop / llm / tasks / storage / budget / guard）· 双驱动（`loop` 任务驱动 + `chat` 会话驱动）· 真实 DeepSeek provider · 目录插件管理（引擎式 desired/actual/delta，插件页可启用 / 停用 / 卸载）· 记忆插件 `plugins.memory`（LLM 提取[过 guard/budget] + 打分衰减 + recall/context 注入；**目录插件形态，默认启用**）· **ReAct 多步循环插件 `plugins.react`**（Think→Act+Observe→收敛 + `max_steps`/时延/预算三把闸 + 工具结果截断；**目录插件形态，默认启用**）· MCP 工具桥 `plugins.windows_mcp`。· **隐式契约加固**（事件名 / 配置键 / 接线守卫三层 fail-fast）· **B5 流式输出（SSE）** · **B6 取消中断**。**639 项离线测试全绿**（另含 ruff lint 与覆盖率门禁）。CI（`.github/workflows/ci.yml`）在每次推送时自动跑：后端全量 pytest（含 `ruff` lint + 覆盖率 ≥85% 门禁 + pip-audit）+ 前端契约自检/单元测试/生产构建 + 镜像构建与 `/health` 冒烟。

**ReAct 多步循环（`plugins.react`）**：`chat` 检测到 `ctx.has("react")` 即**委托**给 ReAct 循环（未装配自动回退单步「规划→执行→解说」，chat 契约未改）。收敛判据 = 模型**不再返回工具调用**；工具一律经 `ctx.tasks.invoke`（guard/schema/审计照生效），模型一律经 `ctx.llm`（guard/budget 照生效）——**无特权通道**。工具异常/参数错/被拒都转成观察回灌让模型自纠（同一工具连拒超 `max_rejects` 才终止）；触顶时如实标注（durable `agent/react/exhausted`）并再答一次。四组件（reasoner/actor/observer/finalizer）是**可换接缝**：`ctx.provide("react.actor", ...)` 即可替换。停用插件 → 回到单步路径。

**工具调用（tool calling）**：任务注册表提供 `catalog()` 动态工具目录；chat 新增 `tool_call` 意图——规划器从工具目录里选工具、执行仍过 guard 白名单。前端有"🛠️ 工具"视图。

**windows-mcp-server 封装**：`agent/sys/mcp_client.py`（通用 MCP stdio 客户端，零依赖）+ `agent/plugins/windows_mcp/`（**可管理插件**：插件页停用/启用/卸载）。**已在本机装好并实测连通**：默认命令 `uvx windows-mcp serve`（自动用 Python 3.14 起子进程），20 个工具（App/PowerShell/FileSystem/Screenshot/Click/Type/Registry/…）自动挂进工具目录，可经对话调用。首次启动需下载依赖，`timeout_s` 默认 120s 可配。

> ⚠️ 这些是**高危工具**（任意 PowerShell/注册表/删文件/杀进程/键鼠）。工具调用仍过 guard 白名单，默认 open；建议在 `config["agent"]["permissions"]["task_deny"]` 里按需收紧 `mcp.windows.PowerShell` / `Registry` / `Process` 等。

**短期记忆（会话历史）**：`agent_messages` 转写（session_id + role/content 时序），每轮把最近 ~6 轮历史 + 长期记忆召回块一起喂给模型——问"我上一句说了什么"这类问题能正确答出；网页顶部"新会话"清空当前会话。

**流式输出与取消（B5 / B6）**：`POST /api/chat/stream` 以 SSE 逐帧推送本轮过程（`start` → `progress`（步骤 / 工具）→ `final`）；`POST /api/chat/cancel {run_id}` 按 run_id 取消在跑的那轮——整轮包成 `asyncio.Task`，`cancel()` 在**任意 await 点**即时生效（可打断 90s 的 LLM 调用与 120s 的 MCP 子进程等待）。取消既非成功也非失败：如实落 `CHAT_DONE(cancelled=true)` 并向前端发 `cancelled` 帧；断连自动取消，不留孤儿任务。内核侧 `agent/sys/progress.py` 只做"订阅内核事件 → 队列"，**与 HTTP 无关**（可换 CLI / WebSocket）；`agent/core/` **零改动**，`chat.send` / `react.run` 签名不变。详见《B5B6-流式与取消-设计方案.md》。

> 当前流的是**过程**（步骤卡片与工具调用），**回复文字仍一次性出现**——文字级"打字机"效果属 **B5.2（token 级流式）**，尚未实施。

**隐式契约加固**：把"改名 / 拼错 → 运行期静默失效"变成"启动即报错"，三层正交互补——① 事件名单一真相源（`Events` 常量）；② 配置 schema 校验（未知键 / 类型不符装配期 `ConfigError`）；③ 接线真发生（capability 接缝 `agent/llm/pre-call`、`agent/tasks/pre-execute` 必须有实时监听，装配期校验 + 运行期 waterfall 守卫**同一开关同源**）。

> 前端**不特化领域**：是一个通用 agent harness（会话 / 事件 / 权限 / 插件 / 记忆 / 工具 视图 + step 轨迹）；领域能力通过**挂载领域插件或工具**注入（现成示例：windows-mcp 工具桥）。

运行：
```bash
python -m agent.run.chat                 # 网页 harness（默认 deepseek，127.0.0.1:8720）
python -m agent.run.chat --fixture       # 离线夹具（不动真 API）
python -m agent.run.chat --once "你好"    # 无头跑一次后退出
```

凭据：真实 key 放 `local_secrets.json`（已在 `.gitignore`，绝不入库）；或 `env DEEPSEEK_API_KEY`。线上支持模型名 `deepseek-flash` / `deepseek-v4-pro`（`deepseek_v4_flash` 为 harness 内部名，线上 API 不收）。

## 容器化部署

前置：Docker（含 Compose v2）。

```bash
# 全栈（agent 服务 + Redis）。**必须提供 PRISM_TOKEN**（生产加固默认开启）：
PRISM_TOKEN=$(openssl rand -hex 16) DEEPSEEK_API_KEY=sk-xxx docker compose up -d --build
# 访问 http://127.0.0.1:8720（请求需带 Authorization: Bearer <PRISM_TOKEN>）

# 只起 Redis（agent 在宿主机上直接跑）
docker compose up -d redis
python -m agent.run.chat
```

**Redis 不是可选项。** 它承载 `work_memory` 的 run 快照，是**跨进程 / 跨重启续接**的前提。
不可用时插件会降级为进程内记忆（启动打 WARNING），**断点续传静默失效** —— 代码在位、能力不生效，这类落差最难发现。因此：

- `docker-compose.yml` 对 prism 显式设了 `PRISM_REDIS_REQUIRED=1`：Redis 连不上就**启动失败**（fail-fast），不静默降级。
- 容器内用服务名连接 `redis://:${REDIS_PASSWORD}@redis:6379/0`（`PRISM_REDIS_URL`）；宿主机直连用 `redis://127.0.0.1:6379/0`。
  ⚠️ compose 已把 Redis 绑到 `127.0.0.1:6379`（loopback only）并设 `--requirepass`。宿主侧若直连，请用 `127.0.0.1` 而非 `localhost` —— Windows 上 `localhost` 会优先解析到 IPv6 `::1`，只绑 IPv4 时会连接超时并触发降级。
- **生产加固默认开启**（2026-10-05 起）：prism 默认 `--hardened --token`（deny-by-default + 强制鉴权），token 经 `PRISM_TOKEN` 注入，缺则 compose 报错、进程 fail-fast。需要无鉴权 dev 形态请直接在宿主机跑 `python -m agent.run.chat`。

也可只用镜像（默认 CMD 已含 `--hardened`，须提供 `PRISM_TOKEN`）：

```bash
docker build -t prism .
docker run --rm -p 8720:8720 -v "$PWD/data:/app/data" \
  -e PRISM_TOKEN=$(openssl rand -hex 16) \
  -e DEEPSEEK_API_KEY=... \
  -e PRISM_REDIS_URL=redis://host.docker.internal:6379/0 \
  prism
```

## 环境变量（运维侧）

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `PRISM_TOKEN` | API 访问令牌（生产加固必需）；启用后仅保护 `/api/*` | 无 |
| `PRISM_HARDENED` | `1/true/on/yes` ⇒ deny-by-default + 强制 token（镜像/compose 默认开） | 关 |
| `DEEPSEEK_API_KEY` | LLM 密钥（亦可放 `local_secrets.json`） | 无 |
| `PRISM_REDIS_URL` | `work_memory` 的 Redis 连接串（优先于插件配置） | `redis://localhost:6379/0` |
| `PRISM_REDIS_REQUIRED` | `1/true/yes/on` ⇒ Redis 不可用时**启动失败**，不降级 | 关 |
| `REDIS_PASSWORD` | compose 里 Redis 的 `requirepass`（同时拼进 `PRISM_REDIS_URL`） | `prism-local` |
| `PRISM_METRICS_SESSION_LABELS` | `/metrics` 输出 per-session 标签（注意标签基数） | 关 |