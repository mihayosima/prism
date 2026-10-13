# 分级沙箱对齐 Claude Code 方案（设计稿 v1.3 · P0/P2/P3 已实施）

编制时间：2026-10-13 21:58（GMT+8）
适用范围：Agent Harness / Prism 沙箱组件重构
更新记录：

| 版本 | 时间 | 变更摘要 |
| --- | --- | --- |
| v1.0 | 2026-10-13 21:58 | 初稿：调研结论 + 现状核实 + 三轴架构 + 分 backend 落地 + 分批计划 |
| v1.1 | 2026-10-13 22:40 | 用户裁定：① 新增「非沙箱 off」为一等档位，进程级运行时不满足时 Sandbox Runtime 默认 off（优雅降级不报错）；② 网络隔离（代理式域名白名单）纳入本轮，补 `network` 策略轴与每 backend 诚实能力/gap |
| v1.2 | 2026-10-13 22:47 | 用户确认 WSL2 已修复（runtime 可用；开发侧 Bash 工具仍黑名单 wsl.exe）；进程级在 Windows 显式拆为 win-token 与 wsl2 两个 backend，`auto` 优先级 wsl2→win-token→off；5 档部署模型落地 |
| v1.3 | 2026-10-13 23:20 | **代码复核 + 首批实施**：修正 §4 接缝契约冲突（真实形状 `(session_key)->dict`、桥只读 cwd/env）；P0/P2/P3 完成并回归绿（118 项）；裁定 strict=false、P1 spike 允许；新增 §11.7「strict ≠ 拒绝 spawn」诚实边界 |

---

## 0. 背景与目标

用户要求把工作中心回调到沙箱，目标是**对齐 Claude Code 的分级沙箱**，达到生产级标准。

Claude Code（以下简称 CC）把隔离分成三档（已联网核实官方文档 `code.claude.com/docs/en/sandbox-environments`），外加一个**非沙箱（off）** 基准态：

| 部署 backend（用户视角的"档"） | CC 概念档 | 机制 | 本机可部署？ |
| --- | --- | --- | --- |
| **off（非沙箱，基准态）** | off | 无隔离，等同直接跑（CC `dangerously-disable` 日常态） | ✅ 现状，一等档位 |
| **win-token（Windows 受限令牌）** | 进程级 | 受限令牌 + DACL 限写，仅文件/进程维度，`partial` | ✅ 待 P1 spike 验证非管理员可用性 |
| **wsl2（WSL2 内 Linux 进程级）** | 进程级 | WSL2 内 `bwrap`+`seccomp`+Landlock + 网络代理（最接近 CC Sandbox Runtime） | ✅ **2026-10-13 用户确认 WSL2 已修复**，runtime 可用；开发侧 Bash 工具仍黑名单 wsl.exe，WSL2 backend 须 Python 级（runtime/subprocess）验证 |
| **container（Docker 容器级）** | 容器级 | Docker 加固（`--network none`+seccomp+只读+非 root）+ 代理 socket | ❌ 本机 Docker 仍起不来，仅代码+配置预留 |
| **cloud（VM/云会话级）** | 云/VM 级 | `claude --cloud`/Firecracker 外部编排 | ❌ 本地无编排，仅 stub |

★ **概念分组**：用户视角的 5 档里，**win-token 与 wsl2 同属 CC 的"进程级"档**——只是 Windows 上两套机制（原生令牌 vs 借 WSL2 的 Linux 原语）。`backend=auto` 在 Windows 上的探测优先级：**wsl2 → win-token → off**（wsl2 最接近 CC 完整隔离；令牌是原生轻量兜底；都不可得则 off）。二者均可被用户显式点名（`backend=wsl2` / `backend=win-token`）。

本仓目标：**把 off + 这（至多）三概念档（进程/容器/云）抽象成一套统一、可扩展、解耦、诚实降级 的沙箱后端框架**，并按本机实际能力分阶段落地——**进程级在本机有两个可运行 backend：wsl2（优先，最接近 CC）与 Windows 受限令牌（原生兜底）**；容器/云档做成代码+配置+接口预留。

★ **用户裁定（2026-10-13）**：off 是一等档位（不是失败态）；当进程级运行时不满足（如原生 Windows 无 WSL2、或非管理员下令牌不可用）时，`Sandbox Runtime` **默认 off**（优雅降级为 none，不报错）。这对应 `backend=auto` 回落 `NullBackend` 的语义，但须把"off"与"请求了隔离却得不到"明确区分展示。

---

## 1. Claude Code 三档调研结论（已联网核实）

### 1.1 进程级 Sandbox Runtime（重点参考对象）
- **范围**：包住**整个 CC 进程**，不只是 per-command。file tools、MCP servers、hooks 都进边界。这是与"只沙箱 Bash"的本质区别。
- **机制（跨平台适配器层 `@anthropic-ai/sandbox-runtime`）**：
  - **macOS**：`sandbox-exec -p` 套 Seatbelt SBPL 规则——`(deny default)` 起手，再 allow `file-read*`/`file-write*` 仅限项目根 + 临时目录，`network*` 默认 deny（除非显式放行）。
  - **Linux**：`bwrap`（mount/pid/network 命名空间，只读根 + 可写 bind-mount）+ `seccomp` BPF（只拦 `openat`/`openat2` 等热点 syscalls，read/write 放行，开销 3–10µs/次）；较新内核用 Landlock 做文件系统白名单。**内核不够新时 fail-closed（拒绝启动，不静默降级）**。
  - **网络**：用户态代理做域名白名单（不终止 TLS，故宽域名=潜在外泄路径，属已知限制）。
- **模式（policy）**：`read-only` / `workspace-write` / `dangerously-disable`。
- **★ 关键真相**：CC 的"沙箱化 Bash 工具"**不支持原生 Windows**，原生 Windows 上只用容器/VM 档。⇒ 本仓若要在 Windows 上做"进程级"，CC 在 Windows 也是借 WSL2 的 Linux 原语做进程级；据此本仓进程级在 Windows 走两条路：优先 **WSL2 内 Linux 原语**（与 CC 同构，最接近完整隔离，且能借 WSL2 netns + 代理做到接近 CC 的网络隔离），兜底 **Windows 受限令牌（partial，仅文件/进程、不含网络）**。

### 1.2 容器级
- `docker run --cap-drop ALL --security-opt no-new-privileges --security-opt seccomp=… --read-only --tmpfs /tmp --network none --memory 2g --pids-limit 100 --user 1000:1000 -v /code:/workspace:ro -v proxy.sock:/var/run/proxy.sock`。
- 容器内可再叠 CC 的 Bash 沙箱（OS 级限制 + 容器边界双重保障）。
- 用于 `--dangerously-skip-permissions` 或不可信代码。

### 1.3 VM 级
- `claude --cloud`：Anthropic 托管，每会话独立 VM，网络代理强制白名单，GitHub Token 留在沙箱外。
- 自托管选项：云实例 / 本地 Hypervisor / Firecracker microVM（Docker Desktop 的 sandbox 即 microVM）。

---

## 2. 本仓现状核实（代码级，非印象）

| 项 | 现状 | 证据 |
| --- | --- | --- |
| 隔离强度 | `enforcement` **恒 `none`**，无任何进程级隔离 | `plugins/sandbox/__init__.py:216` `_ENFORCEMENT_CEILING` 把三档全映射到 `none` |
| 唯一"软"缓解 | 落点收拢：`CHILD_SPAWN_OPTIONS` 把 MCP 子进程 cwd + 临时目录重定向到 `data/sandbox/<session>`，**非强制**（绝对路径仍可写别处） | `plugins/sandbox/__init__.py:181` `workspace_for` + `core/services_catalog.py:48` |
| 子进程 spawn | `McpStdioClient.start()` 仅设 `CREATE_NEW_PROCESS_GROUP`（进程组，非安全隔离） | `core/mcp_client.py:188` |
| 桥消费接缝 | `playwright_mcp` / `windows_mcp` 已接 `CHILD_SPAWN_OPTIONS`；**`skills_mcp` 未接**（不一致） | grep 结果 + `plugins/skills_mcp/__init__.py:99` `_spawn` 只注入 `SKILLS_DATA_DIR` |
| 受限令牌 | 零实现；仅 MEMORY 记"配方实测成立，⚠ 未实施" | grep 无 `CreateProcessAsUser`/`restricted_token` |
| 分层 | `api.py` 不 import `sys`；`CHILD_SPAWN_OPTIONS` 是中性 core 接缝（桥经 getter 消费） | `core/services_catalog.py` + `services_contract` 守卫 |
| 配置 | `sandbox` 节严格（mode/targets/default/allow/deny/risk），新增键须登记否则装配期 `ConfigError` | `core/config_schema.py:103` |
| 分工 | guard=策略唯一持有者；approval=唯一真问人；sandbox=只报隔离档位、恒放行 | `sys/guard.py` + `plugins/sandbox/__init__.py` |

**结论**：当前是"诚实的 none"——至少不谎报。我们要做的是把"none"变成"能兑现的 partial/full"，且**继续诚实**（fail-closed）。

---

## 3. 设计原则

1. **四轴正交，不压成一维阶梯**（沿用现有 `mode`/`enforcement` 思路并升级）：
   - **state 轴 `backend`**：隔离档位/机制（`off` / `win-token` / `wsl2` / `container` / `cloud`），`auto` 默认按平台能力探测（Windows：wsl2→win-token→off）。其中 **`off` 是一等档位**（= CC 的非沙箱基准态），不是失败态。
   - **file-policy 轴 `mode`**：文件系统限制到什么（`none` / `read-only` / `workspace-write`）。
   - **network-policy 轴 `network`**：网络限制（`allow` / `deny` / `proxy` 域名白名单）。**本轮纳入**。
   - **scope 轴 `targets`**：对哪些工具生效（默认 `["mcp.*"]`）。
2. **`enforcement`（报告强度）= backend.strength ∩ backend.can_enforce(mode, network)`**，不再硬编码。承诺了但兑现不了 ⇒ `gap=True` 可见（含"文件维度"与"网络维度"分别计 gap）。
3. **off 是默认安全态 + 优雅降级**：
   - `backend=auto`（默认）：若探测不到可用进程级/容器/云后端（如原生 Windows 无 WSL2、非管理员下令牌不可用、Docker 坏）⇒ **Sandbox Runtime 默认 off**（回落 `NullBackend`），报告 `enforcement=none, sandbox_runtime=off`，**不报错**。
   - 用户**显式**要求某一 backend（如 `backend=process`）但平台确实不满足 ⇒ **可见告警 + 回落 off + 审计落 `backend-error` 计数**，**不静默**、也**默认不硬拒绝**（避免把正常操作打断）；若运营需要"要不到隔离就宁可不让跑"，可由 `strict=true` 开关切到硬拒绝。
4. **解耦优先**：backend 接口落 `agent/core/`（`api.py` re-export，桥经 `CHILD_SPAWN_OPTIONS` 消费），`api.py` 不 import `agent.sys`。沙箱插件只是 backend 的**提供方**，可替换。
5. **软硬分离**：`CHILD_SPAWN_OPTIONS`（落点收拢）作为 backend 的默认产出之一，与"强制隔离"叠加，不互相替代。

---

## 4. 架构：四轴 + backend 策略接口

```
   config: backend(state) × mode(file-policy) × network(policy) × targets(scope)
                                   │
                                   ▼
        plugins.sandbox 选 backend（config.backend == "auto" ⇒ 平台能力探测；不可得 ⇒ off）
                                   │
                                   ▼
        backend.spawn_options(base, policy, session_key) → SpawnOptions
                                   │   （core 中性契约；插件 to_dict() 后经 CHILD_SPAWN_OPTIONS 出接缝）
                                   ▼
        McpStdioClient.start(spawn_options) / skill_bash(...) 消费
                                   │
                                   ▼
        durable: agent/sandbox/confinement {backend, mode, network, strength, gap, enforced_by, cwd}
```

`SpawnOptions` 数据类（core 定义，跨平台字段按需填充）：

```python
@dataclass
class SpawnOptions:
    cwd: str | None                 # 落点收拢（已有）
    env: dict[str,str]              # 重定向后的环境（已有）
    mode: str                       # 声明的文件策略
    backend: str                    # 实际生效的后端名
    strength: str                   # 实际强度（none/partial/full，**诚实值**）
    enforced_by: str                # none / 具体后端 / backend-error
    net_policy: str                 # "allow" | "deny" | "proxy"
    proxy_addr: str | None          # 网络代理地址（proxy 档生效；子进程被迫经此出网）
    netns: bool                     # 是否禁网络命名空间（Linux netns / 容器默认）
    token: int | None               # Windows 受限令牌句柄（进程级 backend，P1）
    creationflags: int | None       # Windows 进程标志（含/不含令牌，P1）
    preexec: Callable | None        # Linux: unshare/prctl/landlock 闭包（P4）
    seccomp_fd: int | None          # Linux seccomp filter fd（P4）

    def to_dict(self) -> dict: ...  # ★ 转接缝字典（见下方契约纪律）
```

★ **契约纪律（2026-10-13 代码复核得出，勿改）**——这一条比上面所有字段都重要：

```
Services.CHILD_SPAWN_OPTIONS 的真实形状（core/services_catalog.py:42-48）：
    (session_key) -> {"cwd":..., "env":{...}} | None
```

- 桥**只用一个参数**调用它：`fn(current_session_key(ctx))`（`playwright_mcp:126` / `windows_mcp:112`），
  且**只消费 `cwd` / `env` 两个键**（`playwright_mcp:99-104`）。
- ⇒ `SpawnOptions` 是**内核内部类型**，由插件侧 `to_dict()` 成字典后经接缝返回；
  **新增键向后兼容**（桥不读），但**绝不可**把接缝改成三参或返回 dataclass——那会直接改崩两个桥。
- ⇒ **per-target 粒度在接缝层不可得**（接缝不传 `target`）⇒ policy 用**插件级**
  （与现状 `workspace_for(_config(ctx), sk)` 一致）。逐工具作用域仍由 `targets` 在 `_pre_task` 审计侧体现。

`ConfinementBackend` 协议（core）：

```python
class ConfinementBackend(Protocol):
    name: str
    strength: Literal["none","partial","full"]
    def capability_probe() -> dict: ...        # {"available": bool, "reason": str}
    def can_enforce(self, mode: str) -> bool: ...
    def spawn_options(self, base: SpawnOptions, policy: dict,
                      session_key: str = "") -> SpawnOptions: ...
    def describe(self) -> dict: ...
```

> **已修正（v1.2）**：初稿写的是 `spawn_options(target, policy, session_key)`。
> 复核真代码后发现与接缝单参契约冲突 ⇒ 改为「插件先算落点 `base`，后端在其上**叠加**隔离信息」，
> `target` 不进接缝。

---

## 5. 分层落地（文件 / 函数落点，满足铁律）

| 文件 | 动作 | 说明 |
| --- | --- | --- |
| `agent/core/sandbox_backend.py` | **新建** | backend 协议 + `SpawnOptions` + 6 个 backend 实现 + `select_backend()`。落 core ⇒ api re-export，桥/run 可消费且不破 `api 不 import sys`。 |
| `agent/api.py` | 改 | re-export `ConfinementBackend`/`SpawnOptions`/`select_backend`（同 SkillsCatalog 下沉做法）。 |
| `agent/plugins/sandbox/__init__.py` | **重构** | 删 `_ENFORCEMENT_CEILING` 硬编码；`_config` 增 `backend`；`apply()` 据 `backend` 实例化并在 `CHILD_SPAWN_OPTIONS` 回调里调 `backend.spawn_options`（保留落点收拢为默认叠加）；`describe()`/`status()`/`confinement` 审计改用 backend 真实 strength + gap。 |
| `agent/core/mcp_client.py` | 扩 | `McpStdioClient.start()` 接受 `SpawnOptions`：Windows 有 `token` ⇒ 走 ctypes `CreateProcessAsUserW`（见 §6.2）；Linux 有 `preexec`/`seccomp_fd` ⇒ 经线程化 `Popen` 应用；否则沿用现有 `create_subprocess_exec`。 |
| `agent/plugins/skills_mcp/__init__.py` | 小修 | `_spawn` 接 `CHILD_SPAWN_OPTIONS`（与另两桥一致），补上落点收拢。低风险。 |
| `agent/core/config_schema.py` | 改 | `sandbox` 严格节增 `backend: str`（默认可填 auto/null/process/container/cloud）。 |
| `agent/sys/tasks.py` | 小修（P0 顺带） | `register` 允许传 `risk`；三桥注册纯读工具时传 `risk="read"`，消除"读技能也要问人"漏洞。 |

**既有接缝不破坏**：`CHILD_SPAWN_OPTIONS` 名称/值不变，`McpStdioClient` 旧调用方（不传 SpawnOptions）行为不变 ⇒ 不劣化。

---

## 6. 各 backend 实现要点

### 6.1 NullBackend（现状，诚实 none）
- `strength="none"`，`can_enforce` 恒 False。仅产出落点收拢。配置 `backend=null` 或 `auto` 探测不到可用后端时回落。

### 6.2 Windows 受限令牌（**partial**，Windows 原生兜底 backend）★ 推荐首做
- 机制：ctypes 调 `advapi32.CreateRestrictedToken`（去掉特权、加 restricting SID）⇒ `CreateProcessAsUserW` 拉起 MCP 子进程。对 `data/sandbox/<session>` 施加 DACL（`(WD)+(RC)` 成对，配方 MEMORY 已记"实测成立"）让受限令牌可写、其余受限。作为 **Windows 原生兜底**：当 wsl2 不可用时它是唯一可落地的进程级真隔离；wsl2 可用时它是轻量降级路径。
- **强度定位 `partial`**：仅限**文件写 + 进程令牌**，**不含网络隔离**（令牌不控网络）。故 `workspace-write` + Windows 令牌仍报 `partial` + 网络 gap 可见。
- **fail-closed**：令牌创建失败 ⇒ 拒绝该子进程 spawn，`enforced_by="backend-error"`，桥照常走"启动失败"路径报错，不静默以全特权起进程。
- **实现难点（须 spike）**：`asyncio.create_subprocess_exec` 不支持令牌；需 ctypes 拿可继承管道句柄 + `CreateProcessAsUserW`，再把句柄接回 asyncio 读写。建议先做最小原型验证读取 stdout/stderr 不丢、子进程树能整棵杀（复用现有 `_kill_tree` 思路，令牌子进程仍可用 taskkill /T）。
- **前置**：本机非管理员——受限令牌通常需要一定权限；需实测非管理员下 `CreateRestrictedToken` 是否可用（MEMORY 记"实测成立"但未注明是否管理员）。若非管理员不可用 ⇒ 该 backend 在本机也 fail-closed，只能等提权或走容器档。

### 6.3 WSL2/Linux Landlock + seccomp + 命名空间（partial/full，**本机可运行**）★ 进程级首选
- `unshare(CLONE_NEWNS|CLONE_NEWPID|CLONE_NEWNET)` + Landlock ABI v4+ 文件系统白名单 + seccomp BPF（仅拦 `openat`/`openat2`）。
- **2026-10-13 用户确认 WSL2 已修复**：runtime（Python subprocess/ctypes 拉起子进程）可经 WSL2 执行，**本 backend 从"仅代码"升级为"本机可运行"**；网络维度可借 WSL2 netns + 代理做到接近 CC 的 `network=deny`/`proxy`（比 win-token 更完整）。
- ⚠ **开发侧验证限制**：本 agent 的 Bash 工具仍把 `wsl.exe` 列入 Program Blacklist，故 WSL2 backend 的 spike/单测须以 **Python 脚本经 subprocess 调 `wsl`**（runtime 路径）或由用户在沙箱外执行，不能从我的 Bash 工具直接跑。
- 内核不足（Landlock ABI 过低）⇒ `capability_probe()` 返回不可用 ⇒ `auto` 不选它，配置显式要求则 fail-closed。

### 6.4 macOS Seatbelt（同理，代码写、本机不跑）
- 生成 SBPL 规则（`(deny default)` + 项目根/临时目录 allow + `network*` deny），`sandbox-exec -p` 包裹。

### 6.5 Container（Docker 模板，本机不可部署）
- 产出一份 `docker run` 模板（§1.2 加固参数）+ 把 `SpawnOptions` 映射成挂载/网络/seccomp 配置。
- 本机 Docker WSL2 起不来 ⇒ 该 backend `capability_probe()` 返回不可用，`auto` 不选；配置显式要求 ⇒ fail-closed 并给出"请先修复 Docker"的明确指引。
- **仍要写出来**：一是接口齐备、二是将来换机/CI 可直接用、三是符合"对齐 CC 三档"的架构完整性。

### 6.6 Cloud / VM（stub）
- 仅定义 `name="cloud"` + `capability_probe()` 恒不可用 + 抛出"需外部编排器"的明确错误；config 预留 `backend=cloud` 开关，真集成（远程执行 glue）留作扩展点，不在此轮实现。

### 6.7 网络隔离（代理式域名白名单，本轮纳入）★ 新增
参考 CC：网络隔离靠**用户态代理**做域名白名单（不终止 TLS，故宽域名=潜在外泄路径，属已知限制），而非内核级封堵。分两层能力：

1. **容器级（`backend=container`）**：`--network none` + 挂载代理 unix socket，子进程**只能**经代理出网 ⇒ 网络隔离最完整（内核级无网 + 应用层白名单）。这是 CC 推荐做法，也是本仓网络隔离的"完整形态"。
2. **进程级（分两个 backend，网络能力不同）**：
   - **win-token（原生 Windows）**：仅**应用层（env 代理）**——注入 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY` 指向本仓自研 asyncio 域名白名单代理；受限令牌**不控任意端口的原始 socket** ⇒ 不走 env 代理的裸 socket 仍可能出网。故 win-token 下 `network=deny` **不可兑现**（无内核 netns），只能 `proxy` 应用层，`net_gap` 对 `deny` 恒为 true。
   - **wsl2（WSL2 内 Linux）**：兼具应用层代理 **+ 内核层 `CLONE_NEWNET` netns** ⇒ `network=deny` 可在内核级真正断网（子进程进独立网络命名空间无路由），`network=proxy` 叠应用层白名单。故 wsl2 进程级可做到**接近 CC 的完整网络隔离**，`net_gap` 在 deny/proxy 下均可为 false（除非 WSL2 内核不支持 netns，探测时如实报 gap）。
   - 应用层代理：backend 给子进程注入代理 env，凡**遵守代理环境变量的工具**（curl/git/npm/python requests 等）出网都过白名单 ⇒ 覆盖绝大多数数据外泄路径。
   - 诚实边界：`SpawnOptions.net_policy="proxy"` 在 win-token = 仅应用层；在 wsl2 = 应用层 + 内核 netns；完整无例外断网（`deny` 对所有裸 socket）仅在**容器档**（`--network none`）可得。
3. **off / null 档**：`net_policy` 默认 `allow`（与现状一致，不劣化）。

**新增组件 `agent/core/net_proxy.py`（或 `run/net_proxy.py`，落点同 core 解耦纪律）**：
- 一个 asyncio 域名白名单代理（CONNECT 隧道 + 普通 HTTP 转发），配置 `network.allow_domains` 列表；不在白名单 ⇒ 拒连 + 计数。
- 由沙箱插件在 `mode!=none` 且 `network=proxy` 时拉起（或复用宿主侧单例），把 `proxy_addr` 注入 `SpawnOptions.env`。
- 审计：每次被拒连接落 `agent/sandbox/net_denied`（domain / target / session）。

**诚实边界（对齐 CC）**：代理按域名粒度生效，不 inspect TLS；`allow_domains` 里写 `github.com` 这类宽域名会同时放行其全部接口，可能成为外泄通道。文案上须明示此限制，不能宣称"网络已绝对隔离"。

---

## 7. 配置与降级语义

```yaml
agent:
  sandbox:
    backend: auto                # state：auto|off|win-token|wsl2|container|cloud（默认 auto；Windows 探测优先级 wsl2→win-token→off）
    mode: workspace-write        # file-policy：none|read-only|workspace-write
    network: allow               # network-policy：allow|deny|proxy（proxy=域名白名单）
    network.allow_domains: []    # proxy 档的域名白名单（空=全拒，需显式开）
    strict: false                # true ⇒ 显式要求 backend 不可得时硬拒绝而非回落 off
    targets: ["mcp.*"]           # scope
```
- **off 一等档位**：`backend=off` 或 `auto` 探测不到可用后端 ⇒ `enforcement=none, sandbox_runtime=off`，**不报错**（对齐用户裁定：进程级运行时不满足时默认 off）。
- `describe()` 输出示例（Windows + 令牌可用 + 代理开）：`{backend:"process", mode:"workspace-write", network:"proxy", strength:"partial", file_gap:false, net_gap:true(进程级仅应用层代理), enforced_by:"windows-restricted-token"}`。
- `gap` 分两个维度分别计：`file_gap`（文件限制未兑现）与 `net_gap`（网络限制未兑现，如进程级 `network=deny` 不可兑现、或 `proxy` 仅应用层）。两维任一为 true ⇒ 整体 `gap=true`。
- **显式要求但不可得**（如 `backend=process` 强制、令牌不可用）：默认**可见告警 + 回落 off + 审计 `backend-error` 计数**（不静默、默认不硬拒绝）；`strict=true` 时改为**硬拒绝该子进程 spawn**（对齐 DSH `denialSignatures` vs `runnerFailureRules`，不把基础设施故障误报成策略结果）。

---

## 8. 与 Follow 三件套（skill_bash 等）的协同

此前"Claude Code 同款 MCP 工具全部补齐方案"里的 `skill_bash`/`skill_file_create`/`skill_str_replace` 是**执行类**工具，其执行也走子进程。它们应**复用同一套 `SpawnOptions` 契约**（cwd=技能 basedir + backend 隔离），与 MCP 子进程享受同一隔离边界。⇒ 沙箱重构与 Follow 三件套是**同一隔离骨架的两个消费者**，先立骨架（本方案）再接执行工具更顺。

---

## 9. 分批实施计划

| 批次 | 内容 | 本机可跑？ | 风险 |
| --- | --- | --- | --- |
| **P0** | 骨架：`core/sandbox_backend.py`（协议+NullBackend+select_backend，含 off 一等档位）+ `SpawnOptions`（含 `net_policy`/`proxy_addr`）；`api.py` re-export；`sandbox` 插件接 backend 驱动 enforcement；config_schema 增 `backend`/`network`/`strict`；删硬编码天花板；审计/状态改读 backend。 | ✅ | 低（核心重构，须全量回归） |
| **P1** | Windows 受限令牌 backend + `mcp_client.py` 令牌 spawn 路径（先做 spike 验证 asyncio+令牌管道）。 | ⚠ 取决于非管理员是否可用 `CreateRestrictedToken` | 中（ctypes 复杂，须原型） |
| **P2** | `skills_mcp` 桥接 `CHILD_SPAWN_OPTIONS`（小修，一致性）。 | ✅ | 低 |
| **P3** | 顺带修 MCP 工具 risk 缺陷（P0 前置已记）：三桥纯读工具注册传 `risk="read"`。 | ✅ | 低 |
| **P4** | **wsl2 backend**（代码+单测+本机 runtime 验证；Linux Landlock+seccomp+ns，最接近 CC）；macOS Seatbelt backend（代码写、本机不跑）。 | ⚠ wsl2 本机可跑（runtime）；macOS 仅代码 | 中（wsl2 须 Python 级验证，不能走 Bash 工具） |
| **P5** | Container backend（Docker 模板+映射，含 `--network none`+代理 socket）；Cloud stub。 | ❌ 本机 Docker 坏 | 低（不本地跑） |
| **P6** | 前端沙箱状态页增强：展示 backend/strength/file_gap/net_gap/enforced_by（复用现有 status 字段）。 | ✅ | 低 |
| **P7** | **网络隔离（本轮纳入）**：`net_proxy.py` 域名白名单代理 + `SpawnOptions` 注入代理 env（进程级应用层）+ 容器档 `--network none`+代理 socket；审计 `agent/sandbox/net_denied`。 | ⚠ 代理本机可跑；容器档网络 none 依赖 Docker | 中（代理需稳，防误拦合法出网） |

**建议实施顺序**：P0 → P2 → P3（低风险骨架+一致性+risk 修复，立即可见）→ P1（win-token spike，原生兜底）→ P4（wsl2 runtime 验证，进程级首选）→ P7（网络代理：先 wsl2/netns 断网 + 应用层白名单，再 win-token 应用层）→ P5/P6。

**实施状态（2026-10-13 23:20 更新）**：

| 批次 | 状态 | 落地摘要 |
| --- | --- | --- |
| **P0** | ✅ 已完成 | 新建 `core/sandbox_backend.py`（`SpawnOptions` + `ConfinementBackend` + `NullBackend` + `select_backend`/`describe_backend`）；`api.py` re-export；sandbox 插件删除 `_ENFORCEMENT_CEILING` 硬编码、改由后端驱动 `enforcement`；`config_schema` 登记 `backend`/`network`/`strict`；审计与 `status()` 增 `backend`/`enforced_by`/`backend_error`。新增 `test_sandbox_backend.py`（18 项）。 |
| **P2** | ✅ 已完成 | `skills_mcp` 接 `CHILD_SPAWN_OPTIONS`（新增 `_workspace()`，与另两桥同款），`_spawn` 传 `cwd` + 合并重定向 env。 |
| **P3** | ✅ 已完成（部分） | `skills_mcp` 的 `skill_list`/`skill_view` 注册传 `risk="read"`（`_READ_ONLY_TOOLS` 白名单）；测试桩补 `risk` 形参 + 新增 P3 回归用例。⚠️ **playwright/windows 两桥未动**——它们各有 25/20 个工具，逐一判定读写语义前不敢批量标 `read`（误标=放行危险工具），列为待办需逐工具审计。 |

---

## 10. 测试与回归

- `test_sandbox_backend.py`：每个 backend 的 `capability_probe`/`can_enforce`/`spawn_options` 纯函数断言；Windows 令牌 backend 在非管理员/不可用时 fail-closed 断言。
- `test_sandbox_plugin.py`：更新为断言 backend 驱动 enforcement、gap 可见、显式要求不可用 backend 时 spawn 被拒（或落 backend-error）。
- `test_services_contract.py`：确保 backend 在 core、`api` 不 import `sys`（既有守卫）。
- 全量 pytest 纪律照旧（`CODEBUDDY_SAFE_DELETE_SANDBOX=0`、basetemp 独立）。

---

## 11. 诚实风险与未决项（必读）

1. **网络隔离是 partial（进程级）**：本轮已纳入代理式域名白名单（P7），但**进程级 backend（Windows 令牌/Linux Landlock）只做应用层代理**（env 注入），**不控裸 socket** ⇒ `net_gap=true` 须如实暴露，不能宣称"网络已封死"。完整网络隔离只在**容器档**（`--network none`+代理 socket，内核级无网）可得。代理不 inspect TLS，宽域名=潜在外泄路径（对齐 CC 已知限制）。
2. **本机 Docker 起不来** ⇒ 容器/VM 档在本机**不可部署**，仅代码+配置+stub，换机/CI 方可用。
3. **管理员权限**：Windows 受限令牌可能需管理员；本机非管理员，P1 前须先 spike 验证 `CreateRestrictedToken` 可用性，否则该档也 fail-closed。
4. **对齐 CC 的口径**：CC 在 Windows 也是借 WSL2 的 Linux 原语做进程级 ⇒ 本仓 **wsl2 backend 与 CC 同构**，可宣称"对齐 CC 进程级沙箱"；**Windows 令牌是 native 兜底（partial，仅文件/进程、不含网络）**，不能宣称等同 CC。文案上**严禁**宣称"沙箱执行/进程级隔离"除非对应 backend 真兑现。
5. **asyncio + Windows 令牌 spawn** 是技术难点，须先做最小原型，不能直接合进主链路。
6. **WSL2 开发侧验证限制**：本 agent Bash 工具仍黑名单 `wsl.exe`，WSL2 backend 的 spike 与回归须以 Python subprocess 调 `wsl`（runtime 路径）或在沙箱外执行；不能依赖我的 Bash 工具直接验证。
7. **`strict=true` 不等于"拒绝 spawn"（代码复核所得）**：`CHILD_SPAWN_OPTIONS` 接缝的返回值只有"给不给落点"两态（dict / None），**没有"拒绝"通道**；桥拿到 `None` 仍会照常起进程。故本轮 `strict=true` 的实际效果是**抛 `BackendUnavailable` + 计数 `agent_sandbox_backend_error_total` + 告警 + 回落无隔离**，**不是**硬阻断子进程。真正的 spawn 级拒绝需桥配合改造（P1+ 待办），文档与文案**不得**宣称"strict 会阻止执行"。

---

## 12. 待确认事项（用户已裁定部分已落实）

**已裁定（2026-10-13）**：
- ✅ **off 是一等档位**：进程级运行时不满足时 `Sandbox Runtime` 默认 off（优雅降级不报错）。已写入 §0/§3/§7。
- ✅ **网络隔离纳入本轮**：代理式域名白名单（P7）已写入 §6.7/§7/§9/§11。

**已裁定（2026-10-13）**：
- ✅ **off 是一等档位**：进程级运行时不满足时 `Sandbox Runtime` 默认 off（优雅降级不报错）。已写入 §0/§3/§7。
- ✅ **网络隔离纳入本轮**：代理式域名白名单（P7）已写入 §6.7/§7/§9/§11。
- ✅ **WSL2 已修复 + 5 档模型**：进程级拆为 `wsl2`（首选）/ `win-token`（兜底），`auto` 优先级 wsl2→win-token→off。已写入 §0/§1.1/§3/§6.3/§6.7/§7。

**✅ 已按推荐裁定（2026-10-13 23:20，用户授权"待定事项按推荐进行"）**：
1. **首做批次 = P0+P2+P3** —— 低风险骨架 + 一致性小修 + risk 缺陷修复。已完成（见 §9 实施状态）。
2. **P1 前置 spike = 允许** —— 令牌最小原型**不并入主代码**，仅验证本机非管理员下 `CreateRestrictedToken` 可用性；结论决定 P1 是否落地。待做（排在 P0/P2/P3 之后）。
3. **`strict` 默认 = `false`** —— 显式要求后端不可得时：可见告警 + 回落 off + 审计 `backend-error`，不硬拒绝（避免打断正常操作）。已落地；⚠️ 其"不等于拒绝 spawn"的边界已写入 §11.7。
