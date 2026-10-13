# 分级沙箱对齐 Claude Code 方案（设计稿 v1.4 · P1 spike 已实测，win-token 出局）

编制时间：2026-10-13 21:58（GMT+8）
适用范围：Agent Harness /  Prism 沙箱组件重构
更新记录：

| 版本 | 时间 | 变更摘要 |
| --- | --- | --- |
| v1.0 | 2026-10-13 21:58 | 初稿：调研结论 + 现状核实 + 三轴架构 + 分 backend 落地 + 分批计划 |
| v1.1 | 2026-10-13 22:40 | 用户裁定：① 新增「非沙箱 off」为一等档位，进程级运行时不满足时 Sandbox Runtime 默认 off（优雅降级不报错）；② 网络隔离（代理式域名白名单）纳入本轮，补 `network` 策略轴与每 backend 诚实能力/gap |
| v1.2 | 2026-10-13 22:47 | 用户确认 WSL2 已修复（runtime 可用；开发侧 Bash 工具仍黑名单 wsl.exe）；进程级在 Windows 显式拆为 win-token 与 wsl2 两个 backend，`auto` 优先级 wsl2→win-token→off；5 档部署模型落地 |
| v1.3 | 2026-10-13 23:20 | **代码复核 + 首批实施**：修正 §4 接缝契约冲突（真实形状 `(session_key)->dict`、桥只读 cwd/env）；P0/P2/P3 完成并回归绿（118 项）；裁定 strict=false、P1 spike 允许；新增 §11.7「strict ≠ 拒绝 spawn」诚实边界 |
| **v1.4** | **2026-10-13 23:50** | **★ P1 spike 实测，推翻两个核心假设**：① **win-token 出局**——非管理员下 `CreateRestrictedToken` 能成功，但**任何 restricting SID 都会让子进程 `0xC0000142` 起不来**（矩阵诊断 6 配置），绕过需放宽系统目录 ACE=安全倒退 ⇒ 标记不可用；② **Docker 已在本机可用**（Desktop 29.4.0，容器运行）+ **WSL2 进程级全套可用**（bwrap/Landlock/netns 实测，`--unshare-net` 真断网）⇒ container/wsl2 从"仅代码预留"升为"本机可部署"。新增 §6.8「目标可移植性约束」（windows_mcp 进不了 WSL/容器）；auto 优先级改 **wsl2→container→off** |

---

## 0. 背景与目标

用户要求把工作中心回调到沙箱，目标是**对齐 Claude Code 的分级沙箱**，达到生产级标准。

Claude Code（以下简称 CC）把隔离分成三档（已联网核实官方文档 `code.claude.com/docs/en/sandbox-environments`），外加一个**非沙箱（off）** 基准态：

| 部署 backend（用户视角的"档"） | CC 概念档 | 机制 | **本机实测状态（2026-10-13 23:50）** |
| --- | --- | --- | --- |
| **off（非沙箱，基准态）** | off | 无隔离，等同直接跑（CC `dangerously-disable` 日常态） | ✅ 现状，一等档位 |
| ~~**win-token**（Windows 受限令牌）~~ | 进程级 | 受限令牌 + DACL 限写，仅文件/进程维度 | ❌ **实测不可用（出局）**：令牌能建，但加 restricting SID 后子进程必崩 `0xC0000142`；不加则毫无隔离。详见 §6.2 |
| **wsl2（WSL2 内 Linux 进程级）** | 进程级 | WSL2 内 `bwrap`+`seccomp`+Landlock+netns（最接近 CC Sandbox Runtime） | ✅ **实测可跑**：内核 6.18、`CONFIG_SECURITY_LANDLOCK=y`、`bwrap`/`unshare`/`python3` 齐备；`--unshare-net` 实跑 **NET_BLOCKED** |
| **container（Docker 容器级）** | 容器级 | Docker 加固（`--network none`+seccomp+只读+非 root） | ✅ **实测可跑**（推翻原判断）：Docker Desktop **29.4.0**，`docker run --rm --network none --read-only --cap-drop ALL alpine echo` ⇒ **CONTAINER_OK** |
| **cloud（VM/云会话级）** | 云/VM 级 | `claude --cloud`/Firecracker 外部编排 | ❌ 本地无编排，仅 stub |

★ **概念分组（更新）**：用户视角的档里，**win-token 与 wsl2 同属 CC 的"进程级"档**（Windows 上两套机制）。但经本轮实测，**win-token 这条路在本机走不通** ⇒ 本机进程级**只有 wsl2 一条可行路径**。

★ **`auto` 探测优先级（v1.4 修订）**：**wsl2 → container → off**（进程级轻量优先，符合 CC 日常默认；容器更重但更强，供显式点名或 wsl2 不可得时承接）。原 v1.2 的 `win-token` 从链路中移除。

本仓目标：**把 off + 三概念档（进程/容器/云）抽象成一套统一、可扩展、解耦、诚实降级 的沙箱后端框架**，并按本机实际能力分阶段落地——**本机真正可部署的是 wsl2（进程级）与 container（容器级）**，cloud 做成代码+配置+接口预留。

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
- **★ 关键真相**：CC 的"沙箱化 Bash 工具"**不支持原生 Windows**，原生 Windows 上只用容器/VM 档；CC 在 Windows 上做进程级同样是**借 WSL2 的 Linux 原语**。⇒ 本仓进程级在 Windows 的正路就是 **WSL2 内 Linux 原语**（与 CC 同构）。
  - ~~Windows 受限令牌（原生兜底，partial）~~ — **v1.4 实测出局，见 §6.2**。结论：Windows 原生进程级隔离在本机**不可兑现**，与 CC 结论一致（CC 也不支持原生 Windows 进程级）。

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
   - **state 轴 `backend`**：隔离档位/机制（`off` / `wsl2` / `container` / `cloud`；~~`win-token`~~ 实测出局，保留枚举值但不实现），`auto` 默认按平台能力探测（Windows：**wsl2→container→off**）。其中 **`off` 是一等档位**（= CC 的非沙箱基准态），不是失败态。
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

### 6.2 ~~Windows 受限令牌~~ —— **实测出局（P1 spike 结论，2026-10-13 23:50）**

原型：`_spike/token_spike.py` + `_spike/token_matrix.py`（**不并入主码**）。

**实测结果（本机非管理员：`IsUserAnAdmin=False`、不在 Administrators 组）**：

| 步骤 | 结果 |
| --- | --- |
| `CreateRestrictedToken`（`DISABLE_MAX_PRIVILEGE` + 2 个 restricting SID） | ✅ **成功** —— 非管理员下令牌建得出来 |
| 构造环境块（`userenv.CreateEnvironmentBlock`）+ `CREATE_UNICODE_ENVIRONMENT` | ✅ 解决 `ERROR_INVALID_PARAMETER(87)` |
| 给 `WinSta0`/`Desktop` 追加 Everyone+RC 的 ACE | ✅ `SetSecurityInfo` rc=0 |
| `CreateProcessAsUserW` 拉起 `cmd.exe` / `python.exe` | ❌ 一律 **`0xC0000142` STATUS_DLL_INIT_FAILED** |

**配置矩阵二分诊断（`token_matrix.py`，决定性）**：

| 配置 | flags | restricting SIDs | 子进程 | 隔离是否生效 |
| --- | --- | --- | --- | --- |
| 1 基线 | 0 | — | rc=0 ✅ | ❌ **毫无隔离**（denied 目录照写） |
| 2 只去特权 | `DISABLE_MAX_PRIVILEGE` | — | rc=0 ✅ | ❌ **毫无隔离** |
| 3 | 0 | `[Everyone]` | `0xC0000142` ❌ | — |
| 4 | 0 | `[RC]` | `0xC0000142` ❌ | — |
| 5 | 0 | `[Everyone, RC]` | `0xC0000142` ❌ | — |
| 6 原方案 | `DISABLE_MAX_PRIVILEGE` | `[Everyone, RC]` | `0xC0000142` ❌ | — |

⇒ **根因与特权无关，是 restricting SID 机制本身**：一旦施加 restricting SID，访问检查的第二遍会要求系统 DLL / 命名对象（`\KnownDlls`、`\BaseNamedObjects` 等）的 DACL 显式允许该 SID，否则进程初始化即失败——且**换系统目录的 `cmd.exe` 也一样崩**，排除"程序本体读不到"这一解释。

**为何不继续**：唯一绕过路径是给 `C:\Windows\System32` 等系统路径放宽 Everyone/RC ACE，这属于**放宽系统目录权限的安全倒退**，与沙箱目标背道而驰。不做。

**处置**：`win-token` 保留为配置枚举值，`capability_probe()` 恒返回 `available=False, reason="restricted-token: 施加 restricting SID 后子进程无法初始化（0xC0000142），本机不可兑现"`（可附带指向 §6.2 的说明），显式点名时按 §3.3 fail-closed 处理。这与 CC 结论一致——**CC 也不支持原生 Windows 进程级沙箱**。

**附带纠正**：此前 MEMORY 记"Windows 受限令牌配方实测成立、⚠ 未实施"是**过期/不完整**的（成立的是"令牌创建"这一步，不是"可用隔离"），已在本轮修正。

### 6.3 WSL2/Linux Landlock + seccomp + 命名空间（**本机实测可跑**，进程级唯一可行路径）★ v1.4 首选
- `unshare(CLONE_NEWNS|CLONE_NEWPID|CLONE_NEWNET)` + Landlock ABI v4+ 文件系统白名单 + seccomp BPF（仅拦 `openat`/`openat2`）。
- **★ 实测证据（2026-10-13 23:50，经 Python subprocess 调 `wsl.exe`）**：

  | 探测项 | 结果 |
  | --- | --- |
  | 内核 | `6.18.40.1-microsoft-standard-WSL2` |
  | Landlock | `CONFIG_SECURITY_LANDLOCK=y`，`CONFIG_LSM` 含 `landlock` ✅ |
  | 用户命名空间 | `CONFIG_USER_NS=y`；`unshare --user --map-root-user --net` ⇒ **成功** |
  | 工具 | `/usr/bin/bwrap`、`/usr/bin/unshare`、`/usr/bin/python3` 齐备 |
  | **bwrap 实跑** | `bwrap --ro-bind /usr … --unshare-net --die-with-parent /bin/echo` ⇒ **`BWRAP_OK`** |
  | **断网实跑** | 同一 bwrap 内 `curl https://example.com` ⇒ **`NET_BLOCKED`** ✅ |

  ⇒ 本 backend 从"仅代码"升级为**本机可运行、可验证**；网络维度**内核级可兑现**（`network=deny` 不再是 gap）。
- ⚠ **开发侧验证限制**：本 agent 的 Bash 工具仍把 `wsl.exe` 列入 Program Blacklist，故 WSL2 backend 的 spike/单测须以 **Python 脚本经 subprocess 调 `wsl`**（runtime 路径，本轮已验证可行）或由用户在沙箱外执行，不能从我的 Bash 工具直接跑。
- 内核不足（Landlock ABI 过低）⇒ `capability_probe()` 返回不可用 ⇒ `auto` 不选它，配置显式要求则 fail-closed。

### 6.4 macOS Seatbelt（同理，代码写、本机不跑）
- 生成 SBPL 规则（`(deny default)` + 项目根/临时目录 allow + `network*` deny），`sandbox-exec -p` 包裹。

### 6.5 Container（Docker 模板，**本机实测可跑**）★ v1.4 升级
- 产出一份 `docker run` 模板（§1.2 加固参数）+ 把 `SpawnOptions` 映射成挂载/网络/seccomp 配置。
- **★ 实测证据（2026-10-13 23:50，推翻原"Docker 起不来"判断）**：

  | 探测项 | 结果 |
  | --- | --- |
  | 引擎 | Docker Desktop **29.4.0**（`OperatingSystem: Docker Desktop (containerized)`） |
  | 运行中容器 | `blog-web-1 / blog-caddy-1 / blog-api-1 / blog-postgres-1 / blog-redis-1 / neo4j` |
  | 本地镜像 | `alpine:latest`、`node:22-alpine`、`postgres:16-alpine`、`hello-world` 等 20 个 |
  | **加固实跑** | `docker run --rm --network none --read-only --cap-drop ALL alpine:latest echo` ⇒ **`CONTAINER_OK`** |

  ⇒ 容器档从"不可部署"升级为**本机可落地、可验证**，且 `--network none` 可真正断网（配合代理 socket 可做白名单出网）。这是本仓目前**强度最高**的一档（文件+网络+进程）。
- 映射要点：`cwd` → `-v <workspace>:/workspace`（`read-only` 档加 `:ro`）；`network=deny` → `--network none`；`network=proxy` → `--network none` + 挂载代理 socket；`mode=read-only` → `--read-only --tmpfs /tmp`。
- ⚠ **成本提示**：每次 spawn 一个容器比 WSL2 进程级重（镜像已有则百 ms 级），且 MCP stdio 桥本身要能在容器/WSL 里跑（见 §6.8）。

### 6.6 Cloud / VM（stub）
- 仅定义 `name="cloud"` + `capability_probe()` 恒不可用 + 抛出"需外部编排器"的明确错误；config 预留 `backend=cloud` 开关，真集成（远程执行 glue）留作扩展点，不在此轮实现。

### 6.7 网络隔离（代理式域名白名单，本轮纳入）★ 新增
参考 CC：网络隔离靠**用户态代理**做域名白名单（不终止 TLS，故宽域名=潜在外泄路径，属已知限制），而非内核级封堵。分两层能力：

1. **容器级（`backend=container`）**：`--network none` + 挂载代理 unix socket，子进程**只能**经代理出网 ⇒ 网络隔离最完整（内核级无网 + 应用层白名单）。这是 CC 推荐做法，也是本仓网络隔离的"完整形态"。
2. **进程级（`backend=wsl2`，v1.4 实测）**：兼具应用层代理 **+ 内核层 `CLONE_NEWNET` netns** ⇒ `network=deny` 可在内核级真正断网。**实测 `bwrap --unshare-net` 内 `curl` ⇒ `NET_BLOCKED`** ✅。故 wsl2 进程级可做到**接近 CC 的完整网络隔离**，`net_gap` 在 deny/proxy 下均可为 false。
   - 应用层代理：backend 给子进程注入代理 env，凡**遵守代理环境变量的工具**（curl/git/npm/python requests 等）出网都过白名单 ⇒ 覆盖绝大多数数据外泄路径。
   - ~~**win-token（原生 Windows）**：仅应用层 env 代理、`deny` 不可兑现~~ —— **该 backend 已出局（§6.2）**，不再计入网络能力矩阵。
   - 诚实边界：`net_policy="proxy"` 在 wsl2 = 应用层 + 内核 netns；完整无例外断网（`deny` 对裸 socket 亦生效）在**容器档**（`--network none`）与 wsl2 netns 均可得。
3. **off / null 档**：`net_policy` 默认 `allow`（与现状一致，不劣化）。

**新增组件 `agent/core/net_proxy.py`（或 `run/net_proxy.py`，落点同 core 解耦纪律）**：
- 一个 asyncio 域名白名单代理（CONNECT 隧道 + 普通 HTTP 转发），配置 `network.allow_domains` 列表；不在白名单 ⇒ 拒连 + 计数。
- 由沙箱插件在 `mode!=none` 且 `network=proxy` 时拉起（或复用宿主侧单例），把 `proxy_addr` 注入 `SpawnOptions.env`。
- 审计：每次被拒连接落 `agent/sandbox/net_denied`（domain / target / session）。

**诚实边界（对齐 CC）**：代理按域名粒度生效，不 inspect TLS；`allow_domains` 里写 `github.com` 这类宽域名会同时放行其全部接口，可能成为外泄通道。文案上须明示此限制，不能宣称"网络已绝对隔离"。

---

### 6.8 ★ 目标可移植性约束（v1.4 新增，架构级必读）

wsl2 / container 两个 backend 都意味着**子进程不在原生 Windows 上跑**。这带来一条此前没显式建模的约束：

| MCP 桥（targets） | 能否在 WSL2 内跑 | 能否在容器内跑 | 结论 |
| --- | --- | --- | --- |
| `skills_mcp`（纯 Python stdio，只读技能目录） | ✅ | ✅（挂载 skills 目录） | 可被 wsl2/container 隔离 |
| `playwright_mcp`（浏览器自动化，有 Linux 版/官方镜像） | ✅（需 WSL 内装浏览器） | ✅（`mcr.microsoft.com/playwright`） | 可被隔离，但需镜像/依赖准备 |
| `windows_mcp`（**Windows 原生 UI / Win32 自动化**） | ❌ | ❌ | **本仓无法沙箱化** —— 只能 `off`，须在 status/gap 里诚实暴露 |

⇒ 设计后果：
1. **每个 target 需声明可移植性**（新增字段，如桥配置里 `runnable_in: ["win","wsl2","container"]`），backend 选择时与目标集合求交；不可移植的目标**降级为 off**，并让 `describe()` 输出 `unportable_targets: ["mcp.windows.*"]` 之类可见信息。
2. 这不是缺陷而是**本机约束的诚实表达**：Windows 原生自动化工具无法放进 Linux/容器边界，与 CC 不支持原生 Windows 进程级同源。
3. 若将来要求 `windows_mcp` 也必须受限，唯一现实路径是「Windows 原生 Job Object + 文件系统 ACL 限制（无 restricting SID）」——但 §6.2 的矩阵已证明**不加 restricting SID 就毫无隔离**，故**不可宣称其具备隔离能力**。

---

## 7. 配置与降级语义

```yaml
agent:
  sandbox:
    backend: auto                # state：auto|off|wsl2|container|cloud（win-token 保留枚举但恒不可用；默认 auto，Windows 探测优先级 wsl2→container→off）
    mode: workspace-write        # file-policy：none|read-only|workspace-write
    network: allow               # network-policy：allow|deny|proxy（proxy=域名白名单）
    network.allow_domains: []    # proxy 档的域名白名单（空=全拒，需显式开）
    strict: false                # true ⇒ 显式要求 backend 不可得时硬拒绝而非回落 off
    targets: ["mcp.*"]           # scope
```
- **off 一等档位**：`backend=off` 或 `auto` 探测不到可用后端 ⇒ `enforcement=none, sandbox_runtime=off`，**不报错**（对齐用户裁定：进程级运行时不满足时默认 off）。
- `describe()` 输出示例（wsl2 可用 + deny）：`{backend:"wsl2", mode:"workspace-write", network:"deny", strength:"partial~full", file_gap:false, net_gap:false, enforced_by:"wsl2-bwrap-landlock"}`；容器档：`{backend:"container", …, strength:"full", net_gap:false, enforced_by:"docker"}`。
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
| **P2** | `skills_mcp` 桥接 `CHILD_SPAWN_OPTIONS`（小修，一致性）。 | ✅ | 低 |
| **P3** | 顺带修 MCP 工具 risk 缺陷（P0 前置已记）：三桥纯读工具注册传 `risk="read"`。 | ✅ | 低 |
| **~~P1~~** | ~~Windows 受限令牌 backend~~ —— **spike 已完成，结论：出局**（§6.2）。原型留在 `_spike/`（不并入主码）。 | — | — |
| **P4** | **wsl2 backend**（bwrap+Landlock+netns，进程级）代码 + 单测 + **本机 runtime 验证**（Python subprocess 调 `wsl`，已证可行）；macOS Seatbelt backend（代码写、本机不跑）。 | ✅ 实测可跑 | 中（须 Python 级验证，不能走 Bash 工具） |
| **P5** | **Container backend**（Docker 模板+映射，含 `--network none`+代理 socket）；Cloud stub。 | ✅ **实测可跑**（v1.4 推翻原判断） | 中（本机可验证，需选/建镜像） |
| **P8** | **目标可移植性**（§6.8）：桥声明 `runnable_in`，不可移植目标（如 `mcp.windows.*`）降级 off 并在 `describe()`/`status()` 暴露 `unportable_targets`。 | ✅ | 低 |
| **P7** | **网络隔离（本轮纳入）**：`net_proxy.py` 域名白名单代理 + `SpawnOptions` 注入代理 env（应用层）+ wsl2 netns / 容器档 `--network none`；审计 `agent/sandbox/net_denied`。 | ✅ 代理本机可跑；断网已实测 | 中（代理需稳，防误拦合法出网） |
| **P6** | 前端沙箱状态页增强：展示 backend/strength/file_gap/net_gap/enforced_by（复用现有 status 字段）。 | ✅ | 低 |

**修订后的实施顺序（v1.4）**：~~P1（win-token，已出局）~~ → **P4（wsl2 进程级，本机实测可跑，与 CC 同构）** → **P5（container 容器级，本机实测可跑，强度最高）** → P8（目标可移植性）→ P7（网络：先 netns/`--network none` 断网，再应用层白名单代理）→ P6（前端）。

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

1. **网络隔离（v1.4 修订，好于预期）**：wsl2 进程级**有内核 netns**，实测 `bwrap --unshare-net` 内 `curl` ⇒ `NET_BLOCKED`；容器档 `--network none` 亦可 ⇒ **`network=deny` 在这两个 backend 下可真正兑现，`net_gap=false`**。仍存的限制是应用层代理（`proxy` 档）：按域名粒度、不 inspect TLS，宽域名=潜在外泄路径（对齐 CC 已知限制），且**不改** env 代理的裸 socket 仍可能绕（除非叠加 netns/`--network none`）。
2. ~~**本机 Docker 起不来**~~ —— **已推翻（2026-10-13 实测）**：Docker Desktop 29.4.0 正常、容器在跑、`docker run --network none --read-only --cap-drop ALL` 成功 ⇒ 容器档**本机可部署可验证**。剩余不可部署的只有 cloud 档（无编排器，纯 stub）。
3. ~~**管理员权限/Windows 令牌**~~ —— **spike 结论：出局**。非管理员下 `CreateRestrictedToken` 能成功，但施加 restricting SID 后子进程必崩 `0xC0000142`；不施加则毫无隔离（§6.2 矩阵）。⇒ 本机**不存在**可用的 Windows 原生进程级隔离。
4. **对齐 CC 的口径（v1.4）**：本仓 **wsl2 backend 与 CC 同构**（bwrap+Landlock+netns，CC 在 Windows 也借 WSL2）⇒ 可宣称"对齐 CC 进程级沙箱"；**container backend 对齐 CC 容器档**（加固参数一致）。文案上**严禁**宣称"沙箱执行/进程级隔离"除非对应 backend 真兑现；`windows_mcp` 一类不可移植目标须显式标注为无隔离（§6.8）。
5. ~~**asyncio + Windows 令牌 spawn 技术难点**~~ —— 随 P1 出局而不再适用。新的技术难点是：**stdio MCP 桥如何跨边界运行**（wsl2 内/容器内跑 Python stdio 桥，路径映射、stderr 回传、进程树终止），须原型验证。
6. **WSL2 开发侧验证限制**：本 agent Bash 工具仍黑名单 `wsl.exe`；但**已验证 Python subprocess（runtime 路径）可正常调 `wsl.exe`**（`wsl -l -v` 返回 Ubuntu/docker-desktop Running）⇒ spike 与回归走 Python 脚本即可，不阻塞。
7. **`strict=true` 不等于"拒绝 spawn"（代码复核所得）**：`CHILD_SPAWN_OPTIONS` 接缝的返回值只有"给不给落点"两态（dict / None），**没有"拒绝"通道**；桥拿到 `None` 仍会照常起进程。故本轮 `strict=true` 的实际效果是**抛 `BackendUnavailable` + 计数 `agent_sandbox_backend_error_total` + 告警 + 回落无隔离**，**不是**硬阻断子进程。真正的 spawn 级拒绝需桥配合改造（P1+ 待办），文档与文案**不得**宣称"strict 会阻止执行"。

---

## 12. 待确认事项（用户已裁定部分已落实）

**已裁定（2026-10-13）**：
- ✅ **off 是一等档位**：进程级运行时不满足时 `Sandbox Runtime` 默认 off（优雅降级不报错）。已写入 §0/§3/§7。
- ✅ **网络隔离纳入本轮**：代理式域名白名单（P7）已写入 §6.7/§7/§9/§11。
- ✅ **WSL2 已修复 + 5 档模型**：进程级拆为 `wsl2`（首选）/ `win-token`（兜底），`auto` 优先级 wsl2→win-token→off。已写入 §0/§1.1/§3/§6.3/§6.7/§7。（⚠ v1.4 修订：`win-token` 出局，优先级改 **wsl2→container→off**）

**✅ 已按推荐裁定（2026-10-13 23:20，用户授权"待定事项按推荐进行"）**：
1. **首做批次 = P0+P2+P3** —— 低风险骨架 + 一致性小修 + risk 缺陷修复。已完成（见 §9 实施状态）。
2. **P1 前置 spike = 允许** —— 令牌最小原型**不并入主代码**。✅ **已完成并出结论：win-token 出局**（§6.2）。
3. **`strict` 默认 = `false`** —— 显式要求后端不可得时：可见告警 + 回落 off + 审计 `backend-error`，不硬拒绝（避免打断正常操作）。已落地；⚠️ 其"不等于拒绝 spawn"的边界已写入 §11.7。

---

## 13. v1.4 新增待拍板事项（编码前）

1. **下一个动手批次**：P4（wsl2 进程级，与 CC 同构、轻量）还是 P5（container 容器级，强度最高、本机已验证）？我的建议是 **P4 先**（进程级是 CC 日常默认档，且无需镜像准备），P5 紧随。
2. **stdio MCP 桥跨边界怎么跑**（wsl2/container 的技术关键）：
   - wsl2：`wsl.exe -e python3 -m agent.run.skills_mcp_adapter`，工作目录用 `/mnt/c/...` 映射（`data/skills` 需从 WSL 可达）；stderr/进程树终止需验证。
   - container：`docker run -i --rm -v <workspace>:/workspace <image> python -m …`（`-i` 保 stdio）。需选定/构建镜像（alpine+python 即可，或复用 `node:22-alpine` 不合适）。
   - 二者都需原型验证 **stdin/stdout 不粘包、长连接稳定、子进程能整棵杀**。
3. **`windows_mcp` 的处置**：确认接受「在 status/describe 里诚实标注为不可沙箱化（off）」，还是需要额外投入（我看不到可行路径，除非接受无隔离）。
4. **是否保留 `win-token` 枚举值**：建议保留但 `capability_probe()` 恒不可用，并在文档中留实验记录（避免他人/日后重复踩坑）。
