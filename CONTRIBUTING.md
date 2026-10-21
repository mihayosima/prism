# 贡献指南

感谢你愿意给 Prism 提代码。这篇说明**怎么用本仓库的开发约定跑起来、改代码、提 PR**。

## 开发环境

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m agent.run.chat --fixture # 离线跑通，不碰真实 API
cd web && npm install              # 前端（仅在改前端时需要）
```

Python ≥ 3.10，CI 用 3.12。

## 目录分层与依赖铁律

这是本项目**最容易踩**的约束，改动前务必理解：

| 目录 | 定位 |
| --- | --- |
| `agent/core/` | 内核机制，**无特权** |
| `agent/sys/` | 系统层，**有特权**（事件 / 任务 / LLM / 存储 / 预算 / 护栏） |
| `agent/plugins/` | 业务插件，可管理目录插件 |
| `agent/run/` | 适配启动层（chat / loop / HTTP 路由） |
| `agent/api.py` | **唯一出口**，各层一律经此导入内核符号 |

三条硬约束，有守卫测试（`agent/tests/test_agent_purity.py`、`test_decouple_guards.py`）在 CI 里拦：

1. **`agent/api.py` 不得 `import agent.sys`**；插件与 run 层只经 `agent.api` 取用内核对象。
2. **推论**：凡要被插件或传输层构造的对象，必须落在 `agent/core/`。
3. 新增 `Services.*` 键必须同时加进 `Services.ALL`，否则 `test_services_contract` 会红。

> 注意别把这条铁律用歪了："插件只能经 api ⇒ 要构造的对象落 core" **只对机制成立**；业务特性不得据此入核。约束链冲突时先问"哪一环可以挪"，而不是挪内核边界。

## 配置改动

`agent` 节是**严格校验**的：会经 `_merge_cfg` 落进 `agent.X` 的配置必须在 `_SCHEMA["agent"]` 登记（`_any` 或逐键），否则装配期直接 `ConfigError`、服务起不来。`permissions` / `session` / `llm` / `loop` 是严格节；MCP 桥的 config 用 `_any` 宽松节。

## 测试

```bash
python -m pytest                      # 全量（888 项）
python -m pytest agent/tests/test_x.py -q
ruff check agent
```

- Windows 下全量 pytest 请设 `CODEBUDDY_SAFE_DELETE_SANDBOX=0`，并用独立的 `--basetemp`（多批共用同一 basetemp 会触发安全删除守卫误报）。
- 新增行为必须有测试。改了内核接缝或分层结构，请确认 `test_agent_purity` / `test_decouple_guards` / `test_services_contract` 仍绿。
- **注意**：少数测试会**扫描源文件文本**（例如找 `gov.ledger` 这类接缝字样）。改注释时若测试突然变红，先查是不是把被扫描的字样删掉了。

## 注释风格

本仓库注释的口径：**只描述这段代码实际在干什么**。

- 不写 markdown 加粗、`★`、`§` 这类装饰标记；
- 不写日期、里程碑标签、迁移纪律清单；
- 不引 `docs/*.md` 章节号或跨模块"口径对齐"；
- 不在注释里用中文全角括号做嵌套补充，承载后果或约束的内容用逗号并入正文；
- 只改注释时，**不能碰运行期字符串**（日志格式串的占位符、工具描述里给模型看的硬约束）——改动后请核对非 docstring 字符串与改动前逐字相同。

## 提交与 PR

- 提交信息用 Conventional Commits：`feat:` `fix:` `docs:` `refactor:` `test:` `chore:`，例如
  `fix(react): 空响应时不再吞掉 finalizer 异常`。
- 一个 PR 只做一件事。描述里写清**改了什么、为什么、怎么验证**。
- PR 会触发 CI（后端 pytest + ruff + 覆盖率 ≥85% + pip-audit，前端构建）。全绿才会被合。
- 涉及安全相关的改动（权限、护栏、沙箱、凭据），请在 PR 里单独说明风险面。

## 文档

设计文档放 `docs/`：**活文档**进主题域目录（`architecture/` `plugins/` `memory/` `context/` `sandbox/` `spec/` `roadmap/`），**带日期的评估 / 复核 / 审计快照**进 `docs/reports/<主题>/`。文档头部请带编制时间，多次修订的附更新记录表。

---

编制时间：2026-10-22 01:09（GMT+8）

| 版本 | 时间 | 变更摘要 |
| --- | --- | --- |
| v1.0 | 2026-10-22 01:09 | 初版：分层铁律、配置登记、测试纪律、注释口径、提交与 PR 约定 |
