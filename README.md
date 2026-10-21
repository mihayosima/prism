# Prism

[![CI](https://github.com/mihayosima/prism/actions/workflows/ci.yml/badge.svg)](https://github.com/mihayosima/prism/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-888%20passed-brightgreen.svg)](agent/tests)

插件化 Agent 运行时：**everything-is-a-plugin**。内核极小，系统能力（事件 / 循环 / LLM / 任务 / 存储 / 预算 / 护栏）与领域能力（记忆 / ReAct / 审批 / 沙箱 / MCP 工具桥）都是插件，可在运行时启用、停用、卸载。独立可离线自举，**零外部引擎依赖**（不依赖 LangChain / LlamaIndex 等）。

- 内核：`agent/`，`agent.api` 是**唯一出口**
- 设计文档：见 [`docs/README.md`](docs/README.md)（按主题域组织，评估报告另放 `docs/reports/`）

---

## 特性

| 能力 | 说明 |
| --- | --- |
| 分层内核 | `core`（无特权机制）/ `sys`（有特权系统层）/ `plugins`（业务插件）/ `run`（适配启动层）/ `api`（唯一出口）。包边界即依赖边界 |
| 双驱动 | `loop` 任务驱动 + `chat` 会话驱动 |
| 目录插件管理 | 引擎式 desired / actual / delta，网页插件页可启用、停用、卸载 |
| ReAct 多步循环 | `plugins.react`：Think → Act + Observe → 收敛，`max_steps` / 时延 / 预算三把闸，工具结果截断 |
| 记忆插件 | `plugins.memory`：LLM 提取（过 guard / budget）+ 打分衰减 + recall / context 注入 |
| 工具调用 | 任务注册表提供 `catalog()` 动态工具目录，执行一律过 guard 白名单 |
| MCP 工具桥 | 通用 MCP stdio 客户端（零依赖），已封装 `windows_mcp` 等桥 |
| 流式与取消 | `POST /api/chat/stream` SSE 逐帧推送过程；`POST /api/chat/cancel` 按 `run_id` 在任意 await 点即时中断 |
| 隐式契约加固 | 事件名单一真相源 + 配置 schema 校验 + 接线守卫，把"改名 / 拼错 → 运行期静默失效"变成"启动即报错" |
| 沙箱 | 进程级（WSL2 bwrap）/ 容器级（Docker）两档，后端能力真实驱动，不可达时诚实报 off 而非假装健康 |

**前端不特化领域**：是一个通用 agent harness（会话 / 事件 / 权限 / 插件 / 记忆 / 工具视图 + step 轨迹），领域能力通过挂载插件或工具注入。

---

## 快速开始

### 环境要求

- Python ≥ 3.10（CI 用 3.12）
- Node.js ≥ 18（仅前端开发需要；运行时直接托管 `web/dist` 构建产物）
- Redis（可选但**强烈建议**，见下）

### 安装

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"            # dev 含 pytest / pytest-cov / fakeredis
```

### 运行

```bash
python -m agent.run.chat                 # 网页 harness（默认 127.0.0.1:8720）
python -m agent.run.chat --fixture       # 离线夹具，不碰真实 API
python -m agent.run.chat --once "你好"    # 无头跑一次后退出
```

打开 <http://127.0.0.1:8720>。

凭据放 `local_secrets.json`（已在 `.gitignore`，**绝不入库**），或用环境变量 `DEEPSEEK_API_KEY`。

> ⚠️ 默认不带 token 且权限默认开放（`default=open`），**仅供本机开发**。部署到网络前请加 `--token --hardened`。

---

## 容器化部署

前置：Docker（含 Compose v2）。

```bash
# 全栈（agent 服务 + Redis）。生产加固默认开启，必须提供 PRISM_TOKEN：
PRISM_TOKEN=$(openssl rand -hex 16) DEEPSEEK_API_KEY=sk-xxx docker compose up -d --build
# 访问 http://127.0.0.1:8720（请求需带 Authorization: Bearer <PRISM_TOKEN>）

# 只起 Redis（agent 在宿主机上直接跑）
docker compose up -d redis
python -m agent.run.chat
```

**Redis 不是可选项。** 它承载 `work_memory` 的 run 快照，是跨进程 / 跨重启续接的前提；不可用时插件降级为进程内记忆并打 WARNING，**断点续传静默失效** —— 代码在位、能力不生效，这类落差最难发现。因此：

- `docker-compose.yml` 对 prism 显式设 `PRISM_REDIS_REQUIRED=1`：Redis 连不上就启动失败（fail-fast），不静默降级。
- 容器内用服务名连接 `redis://:${REDIS_PASSWORD}@redis:6379/0`；宿主机直连用 `redis://127.0.0.1:6379/0`。
  ⚠️ compose 把 Redis 绑到 `127.0.0.1:6379` 并设 `--requirepass`。宿主侧直连请用 `127.0.0.1` 而非 `localhost` —— Windows 上 `localhost` 会优先解析到 IPv6 `::1`，只绑 IPv4 时会连接超时并触发降级。
- 生产加固默认开启：`--hardened --token`（deny-by-default + 强制鉴权），token 经 `PRISM_TOKEN` 注入，缺失则 compose 报错、进程 fail-fast。

只用镜像（默认 CMD 已含 `--hardened`，须提供 `PRISM_TOKEN`）：

```bash
docker build -t prism .
docker run --rm -p 8720:8720 -v "$PWD/data:/app/data" \
  -e PRISM_TOKEN=$(openssl rand -hex 16) \
  -e DEEPSEEK_API_KEY=... \
  -e PRISM_REDIS_URL=redis://host.docker.internal:6379/0 \
  prism
```

### 环境变量

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `PRISM_TOKEN` | API 访问令牌（生产加固必需）；启用后仅保护 `/api/*` | 无 |
| `PRISM_HARDENED` | `1/true/on/yes` ⇒ deny-by-default + 强制 token（镜像 / compose 默认开） | 关 |
| `DEEPSEEK_API_KEY` | LLM 密钥（亦可放 `local_secrets.json`） | 无 |
| `PRISM_REDIS_URL` | `work_memory` 的 Redis 连接串（优先于插件配置） | `redis://localhost:6379/0` |
| `PRISM_REDIS_REQUIRED` | `1/true/yes/on` ⇒ Redis 不可用时**启动失败**，不降级 | 关 |
| `REDIS_PASSWORD` | compose 里 Redis 的 `requirepass`（同时拼进 `PRISM_REDIS_URL`） | `prism-local` |
| `PRISM_METRICS_SESSION_LABELS` | `/metrics` 输出 per-session 标签（注意标签基数） | 关 |

---

## 项目结构

```
agent/
  api.py        唯一出口：各层一律经此导入内核符号
  core/         内核机制（无特权）
  sys/          系统层（有特权：事件 / 任务 / LLM / 存储 / 预算 / 护栏）
  plugins/      业务插件（可管理目录插件）
  run/          适配启动层（chat / loop / HTTP 路由）
  tests/        测试（888 项）
web/            前端（Vue 3 + TypeScript + Vite）
docs/           设计文档（按主题域 + reports/ 评估快照）
```

分层铁律与贡献约定见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

---

## 开发

```bash
python -m pytest                 # 全量测试（888 项）
ruff check agent                 # lint
pip install -e ".[dev]"

cd web && npm install && npm run dev   # 前端开发服务器
```

CI（`.github/workflows/ci.yml`）在每次推送与 PR 时自动跑：后端全量 pytest（含 `ruff` lint + 覆盖率 ≥85% 门禁 + pip-audit）+ 前端契约自检 / 单元测试 / 生产构建 + 镜像构建与 `/health` 冒烟。

---

## 文档

- [`docs/README.md`](docs/README.md) —— 文档总索引
- 主题域：`architecture/`（内核与系统层）、`plugins/`（各插件设计）、`memory/`、`context/`、`sandbox/`、`spec/`、`roadmap/`
- `docs/reports/` —— 带日期的评估 / 复核 / 审计快照（只读，不再回改）

---

## 贡献

欢迎 Issue 与 PR，流程与代码约定见 [`CONTRIBUTING.md`](CONTRIBUTING.md)。

## 安全

风险面与漏洞报告方式见 [`SECURITY.md`](SECURITY.md)。**本项目可挂载高危工具**（任意命令执行 / 注册表 / 文件删除），默认配置仅供本机开发。

## 许可证

[MIT](LICENSE) © 2026 mihayosima

---

编制时间：2026-10-22 01:09（GMT+8）

| 版本 | 时间 | 变更摘要 |
| --- | --- | --- |
| v1.0 | 2026-10-22 01:09 | 按开源仓库标准重写：对齐名称 Prism、补徽章与目录结构、修正测试数 888 |
