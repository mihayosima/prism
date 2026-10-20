# Windows 进程级隔离（restricted token）· 方案与可行性核定

编制时间：2026-10-04 01:45（GMT+8）
**★ 最终定论见 §9：写限制已实测成立（四项判定全 PASS）。**
§6/§7/§8 记录的三次"不可行"结论**均已被推翻**，保留作过程记录与教训。
状态：**机制已验证可行；仍需换子进程启动器（`asyncio` 无 token 参数）**（全程未改任何生产代码）
参考：`deepseek-ai/deepseek-harness` 的 `@deepseek-ai/dsh-sandbox-windows-acl`（机制与坑已逐条核对）

---

## ★ 结论摘要（先看这段）

**不要实施。** 探针实测出**两条硬阻塞**，都不是"多写点代码能绕过"的：

| 阻塞 | 实测结论 |
|---|---|
| **① SID 数组封送失败** | `CreateRestrictedToken` 只在 `SidsToEnable=None` 时成功；传任何 SID 数组 ⇒ `ERROR_NONE_MAPPED(534)`。**与掩码（六种全测）、flags（五种全测）全部无关** ⇒ 纯 ctypes 封送不了，需 koffi/FFI |
| **② asyncio 不支持传 token** | `create_subprocess_exec` 无 token 参数 ⇒ 必须 `CreateProcessW`+`STARTUPINFOEX` ⇒ **换子进程启动器** |

⇒ **纯标准库 + 不换 spawn 路径 ⇒ 本机做不出 restricted token。**
DSH 能做是因为它**既有 koffi（Ffi 已就位）又自己实现了 spawn**（`spawn.ts`）—— 两个前提我们都不具备。

**替代方案（低成本，建议改用它）**：只做**环境重定向**（`npm_config_cache` / `PLAYWRIGHT_BROWSERS_PATH` /
`TMP` 指到会话私有目录），**不碰 token**。⚠️ 它不阻止桥乱写，只让桥写到我们指定的地方。
若将来要真隔离，本机 Docker（WSL2 后端）**容器档的收益/代价比优于 restricted token**。

---

## 0. 一句话结论

**机制在本机可行（Win32 API 全部可达、非管理员即可），但当前架构有两个硬阻塞，
使"这一批"做不了**：

| 阻塞 | 性质 | 证据 |
|---|---|---|
| **① `asyncio.create_subprocess_exec` 不支持传 token** | **架构级** | 实测签名只有 `program/args/stdin/stdout/stderr/limit/kwds`。要用 restricted token 必须走 `CreateProcessW`（`STARTUPINFOEX` + `PROC_THREAD_ATTRIBUTE_PARENT_PROCESS`/`hToken`），**等于要自己实现一个子进程启动器** |
| ② `npx` 要写 `%LOCALAPPDATA%\npm-cache` | 策略级（可解） | 实测 `npm config get cache` = `<用户主目录>\AppData\Local\npm-cache`，**不在工作区** ⇒ `workspace-write` 下必失败。解法是把它显式指到会话私有目录 |

**追加发现（阻塞①的放大器）**：DSH 自承"受限 token 进程用 `stdio:'pipe'` spawn 孙进程会 EPERM"。
我们的桥正是 **stdio 管道 + 三层启动链**（`uvx→uv→python` / `npx→node→chromium`）。
主路径（未受限父进程 spawn 受限子进程 + 管道）可行，**但孙进程那一层未实测**。

⇒ 建议：**先做探针（不改生产代码），把"能不能启动"变成事实，再谈实现。**

---

## 1. 可行性探测结果（只读，已实测）

### 1.1 Win32 API：全部可达 ✅

| DLL | 需要的 API | 结果 |
|---|---|---|
| `kernel32` | `CreateProcessW` / `CloseHandle` / `WaitForSingleObject` / `GetExitCodeProcess` / `CreateJobObjectW` / `AssignProcessToJobObject` / `SetInformationJobObject` / `TerminateJobObject` / `SetEnvironmentVariableW` / `GetTempPathW` | **全部 OK** |
| `advapi32` | `CreateRestrictedToken` / `SetTokenInformation` / `GetTokenInformation` / `SetNamedSecurityInfoW` / `GetNamedSecurityInfoW` / `ConvertStringSecurityDescriptorToSecurityDescriptorW` / `LookupAccountSidW` / `GetLengthSid` / `CreateWellKnownSid` / `EqualSid` | **全部 OK** |
| — | `LocalFree` | 缺失（可用 `RtlFreeHeap` 或不释放 —— 一次性调用，泄漏可接受） |

⇒ **不需要 koffi / 第三方包，标准 `ctypes` 足够。** 这比 DSH 的实现环境（TypeScript + koffi FFI）更简单。

### 1.2 权限前提：满足 ✅

DSH 的机制**不需要管理员**：它依赖"目录 owner 隐含的 `WRITE_DAC`"来改 DACL。
本项目工作区 `D:\Desktop\prism` 是当前用户拥有的 ⇒ 满足。
（已核实本机**非管理员**，但这套机制恰好不需要管理员 —— 反而是 AppContainer 那条路要更底层的权限。）

### 1.3 ★ 阻塞①：`asyncio` 不支持传 token

```
asyncio.create_subprocess_exec 参数 = (program, args, stdin, stdout, stderr, limit, kwds)
```

要传 restricted token 只能走 `CreateProcessW` +
`STARTUPINFOEX`（`PROC_THREAD_ATTRIBUTE_HANDLE_LIST`）或 `CreateProcessWithTokenW`。

**代价**：需要自己实现"创建管道 → 组 STARTUPINFOEX → CreateProcessW → 把句柄接进 asyncio 事件循环"。
项目已用 `WindowsProactorEventLoopPolicy`（`ProactorEventLoop` 可用）⇒ **IOCP 管道可用**，
但接起来是**数百行**平台代码，且要重做现有的：
- 进程组整棵收尸（`mcp_client.py:253`，已有 uvx→uv→python 整棵收尸能力，**不能退化**）
- 管道读循环 / stderr 读循环 / wait 循环
- 死亡可见与归因（既有 S4 加固）

⇒ **这不是"加个档位"，是"换子进程启动器"。** 影响面远超沙箱插件本身。

### 1.4 阻塞②：`npx` 写 npm cache

```
npm config get cache = <用户主目录>\AppData\Local\npm-cache
```

playwright 桥默认命令是 `npx -y @playwright/mcp@latest`（`playwright_mcp/__init__.py:51-61`），
`npx` 必然写这个目录 ⇒ **不在工作区** ⇒ `workspace-write` 下直接失败。

**解法**（同 DSH 的做法）：spawn 桥时把环境显式指向会话私有目录 ——
`npm_config_cache` / `PLAYWRIGHT_BROWSERS_PATH` 指向 `data/sandbox/<sid>/`。
这不是绕过策略，而是**把策略要求的目录显式指出来**。

⚠️ 既有契约：`child_env` 的剥除清单（`mcp_client.py:61-66`）要加注入项，且**必须保留 PYTHONPATH**。

---

## 2. DSH 的机制要点（我们要抄的部分，已逐条核实原文）

### 2.1 机制一句话

把调用方 token 复制成 `WRITE_RESTRICTED` token，其 **restricting SID 列表**里放一个
capability SID（`S-1-4-x-y`），而这个 SID 的 Write ACE **只存在于**工作区与会话私有临时目录上。
Windows 只在「调用方正常权限」∧「restricting SID 交集」同时允许时才授予写。

- flags：`WRITE_RESTRICTED` + `DISABLE_MAX_PRIVILEGE` + `LUA_TOKEN`
- 另：token 降到 **Low integrity**，被授予目录带 **Low no-write-up label** + 环境删除 deny
- 平台模块是 `huoyaoyuan/windows-acl-restrict-poc`（`10e4dfb`）的移植

### 2.2 ★ "身份即工作区"（最值得抄的一处设计）

| SID | 派生 | 生命周期 | ACE |
|---|---|---|---|
| **Workspace SID** | `workspaceWriteSid(规范化工作区路径)` —— **从路径确定性派生** | 每工作区每机器只 materialize 一次 | **standing，永不撤销**（跨会话复用缓存） |
| **Temp SID** | `tempWriteSid(path)`，每活动会话一个 | 会话结束**可撤销** | `dispose()` 撤销 |

**为什么重要**：POC 每会话随机 SID ⇒ 每次都要全树传播 DACL（DSH 实测大树几十秒）。
把身份从"会话"移到"工作区"，**这个成本一辈子只付一次**。

⚠️ 附带约束：**一个工作区只有一个写白名单**（write SID **就是**工作区身份）。
一个实例跨两个工作区 ⇒ 两个 grant 一起放宽。⇒ 必须**按工作区路径一实例一个**。

### 2.3 模式 = restricting SID 列表

- `workspace-write` = `(logon SID, Everyone, write SID)` → 工作区 + 会话私有 temp 有 Write grant
- `read-only` = `(logon SID, Everyone)` —— **无 write SID** ⇒ 零 grant
  ★ 注意：**不是"零权限"，是"零 grant"**（keep-alive 组仍在）

keep-alive 组（`logon SID` + `Everyone`）**两种模式都在** ——
缺了会在 DLL 初始化期死（`0xC0000142`）或 CNG 崩 pwsh（`0xE0434352`）。

### 2.4 DSH 实测的边界（我们也要继承）

| 边界 | 原因 |
|---|---|
| **只限写；读/网络/进程可见性完全不限** | `WRITE_RESTRICTED` 只 intersect 写访问 ⇒ `read-only` **无法单靠此机制表达** |
| **控制台隔离不可用** | `CREATE_NO_WINDOW` / `CREATE_NEW_CONSOLE` 子进程在 DLL 初始化期死（`0xC0000142`） |
| `whoami` / token 检查 cmdlet 失败 | 诊断噪声，非功能故障 |
| WMI 静默不可用 | `Authenticated Users` 不在任一列表 ⇒ `0x80041003` |
| **FAT 卷两种模式下都可写** | 无安全描述符 ⇒ 写检查通过（Everyone 在两个列表里）|
| **受限模式下 `pwsh` 跑 ConstrainedLanguage** | `Add-Type`/COM/反射全废 |
| 授予目录必须 caller-owned | owner 隐含 `WRITE_DAC` |
| **每 Win32 调用都查错抛异常** | POC 忽略返回值 ⇒ `CreateRestrictedToken` 失败就**静默全权限跑 = fail-open** |

⇒ **`enforcement` 永远是 `partial`，不是 `full`**（DSH 实测）。

---

## 3. 分阶段方案

### 阶段 P（**建议先做这个**）：探针脚本，零生产代码改动

写 `.workbuddy/probe_restricted_token.py`，只回答"能不能行"：

| # | 探针 | 判据 |
|---|---|---|
| P1 | `CreateRestrictedToken` + `CreateProcessW` 起 `cmd /c echo ok` | 能否用 restricted token 启动进程 |
| P2 | 受限 token 下写工作区内文件 | 应**成功** |
| P3 | 受限 token 下写工作区**外**文件（如 `%TEMP%\x.txt`） | 应**失败**（这是我们要的核心保证）|
| P4 | 受限 token + 管道（stdin/stdout/stderr） | 主路径可行性 |
| P5 | **受限进程 spawn 孙进程 + 管道** | ★ DSH 自承会 EPERM，**决定 `uvx→uv→python` 能否 confined** |
| P6 | `npx -y @playwright/mcp@latest` 在受限 token + 私有 cache 下能否跑起来 | 决定阻塞②的解法是否成立 |
| P7 | 身份即工作区：第二次 provision 命中 ACE skip | 验证"只付一次成本" |

**产出**：一张事实表（能/不能/需绕路）。**不改任何生产代码。**

### 阶段 1：子进程启动器（**只在探针全绿后做**）

新模块 `agent/sys/win_token.py`（放 `sys/` 不放 `core/` —— 它是平台 IO）：
- `RestrictedToken`：token 派生 / 降 integrity / 释放
- `WorkspaceGrant`：workspace SID 派生 + standing ACE + temp SID 撤销
- `spawn_restricted()`：`CreateProcessW` + IOCP 管道接入 asyncio

挂载点：`agent/core/mcp_client.py:187` 的 `create_subprocess_exec`（**唯一能施加档位的地方**）。

⚠️ **必须保住现有能力**（不可退化）：
- 进程组整棵收尸（`:253`）
- 三个读循环（管道 / stderr / wait）
- 死亡可见与归因（S4 加固）
- `on_stderr` 必须接（既有 MCP 桥契约）

### 阶段 2：接进沙箱

- `_ENFORCEMENT_CEILING`：`workspace-write` → `partial`（**不是 `full`**）
- `enforced_by` 填真实后端名
- `probe()` 真实调用 `RestrictedToken.probe()`
- 无后端/探测失败 ⇒ `enforcement=none` + `gap=true`（**不谎报**）

### 阶段 3：MCP 桥逐个接入

先 `playwright_mcp`（纯子进程、可容器化），`windows_mcp` 本机无 `uv`/`uvx` 暂不适用。

---

## 4. 我不推荐现在做阶段 1 的理由

| 理由 | 说明 |
|---|---|
| **会换掉子进程启动器** | 不是"加档位"，是重写 spawn + 收尸 + 三个读循环。**失败面远大于收益面** |
| **DSH 自己在 Windows 上也只是 `partial`** | 收益是"写限制"，不是"隔离"。宣传成"沙箱"会夸大 |
| **P5/P6 未实测** | 孙进程管道与 npx cache 两个坑都可能让方案退化成"只对没有孙进程的桥有效" |
| **本机已有 Docker（WSL2 后端）** | 若目标是"真隔离"，容器档的**收益/代价比**优于 restricted token |

⇒ **建议顺序：阶段 P 探针 → 依据结果重新评估是否值得做阶段 1。**
探针成本很低（不改生产代码），但能避免"改了 500 行才发现根本跑不起来"。

---

## 5. 需要你拍板

1. **先跑阶段 P 探针吗？**（我建议跑 —— 成本低，且能把三个未知数变成事实）
2. **若探针全绿，是否接受"换子进程启动器"这个改动面？**（我倾向先看探针结果再决定）
3. **如果目标只是"别让 MCP 桥乱写系统目录"** —— 有个**代价低得多**的替代：
   只做 **`mode=none` + 环境重定向**（把 `npm_config_cache` 等指到会话私有目录），
   不碰 token。**它不阻止桥乱写，只让桥写到我们指定的地方。**
   如果这个够用，就不需要整套 restricted token。

---

## 6. 探针实测结果（2026-10-04 02:10，定论）

脚本：`.workbuddy/probe_restricted_token.py`（主探针）+ 一次性隔离诊断。

### 6.1 可行的部分（确认）
- Win32 API 全部可达；**不需要 koffi 就能调**；**不需要管理员**（依赖目录 owner 的隐含 `WRITE_DAC`）。
- `ConvertStringSidToSidW` 可用（`S-1-1-0` / `S-1-4-8-8-8-8-8` 都能构造并回读校验）。
- `OpenProcessToken` + `SetNamedSecurityInfoW` 链未报错。

### 6.2 阻塞①：`CreateRestrictedToken` 一律 534

穷举矩阵（每格都试了）：

| 变量 | 取值 | 结果 |
|---|---|---|
| **token 访问掩码** | `0x1` / `0x2` / `0x8` / `0xA` / `0xB` / `0xF` | **全部 534** |
| **flags** | `R` / `R\|DMP` / `R\|LUA` / `R\|WR` / `R\|DMP\|LUA\|WR` | **全部 534** |
| **SidsToEnable** | `None + cnt=0` | ✅ **成功**（`ok=1`） |
| **SidsToEnable** | 裸 `PSID` 数组（1 项 / 2 项） | ❌ 534 |
| **SidsToEnable** | `PSID_AND_ATTRIBUTES` 数组（1 项 / 2 项） | ❌ 534 |

⇒ **唯一的变量是 SID 数组的封送**。掩码与 flags 都无辜。
`ERROR_NONE_MAPPED(534)` 在这里意味着内核不接受这份封送后的数组布局。

**⇒ 纯 ctypes 封送不可用**。DSH 用 koffi（Ffi）正是为此。

### 6.3 阻塞②：`asyncio` 无 token 参数（复述 §1.3）

实测签名：`(program, args, stdin, stdout, stderr, limit, kwds)` —— 无 token。

### 6.4 ctypes 四个坑（实测踩过，长期有效）

1. **不设 `argtypes`/`restype` ⇒ x64 HANDLE 被截断成 32 位 int** ⇒
   `OpenProcessToken` 返回 0 + `ERROR_INVALID_HANDLE(6)`。**看起来像"权限不足"，实际只是声明缺失。**
2. **`AllocateAndInitializeSid` 是变参函数（`...`）⇒ ctypes 无法封送** ⇒ 传 `None` 当 authority 会
   **读越界段错误**。正确做法：`ConvertStringSidToSidW`（吃字符串 SID）。
3. **`TOKEN_GROUPS` 里的 SID 是「指针 + 变长」** ⇒ 用固定步长遍历会**段错误**（stride 16/12 都不对）。
   ⇒ **不要自己构造 SID，也不要遍历 TokenGroups**；用字符串形式 + well-known SID。
4. `LocalFree` 在 **`kernel32`** 不在 `advapi32`；`wintypes` **没有 `LUID`**（`TOKEN_PRIVILEGES` 需自定义结构）。

### 6.5 附：一处 DSH 文档与本机实测的差异

DSH 说"每 Win32 调用都查错抛异常，POC 忽略返回值 ⇒ fail-open"。本轮实测印证了这句的重要性：
若忽略 `CreateRestrictedToken` 的返回值，**会得到一个未受限制的 token 却以为受限** ——
这正是我们要防的 fail-open。实施时必须每步查错。

## 7. 修订后的建议

| 方案 | 代价 | 保护强度 | 建议 |
|---|---|---|---|
| restricted token | 引入 koffi + **换子进程启动器**（数百行） | 写限制（`partial`） | ❌ **不实施**（成本/收益不成立） |
| **环境重定向** | ~10 行，只改 `child_env` 注入 | **不阻止乱写**，只控制落点 | ✅ **建议改用** |
| 容器档（Docker WSL2） | 打包镜像 + 桥改成容器内跑 | 强（硬件虚拟化背书） | 将来若真要隔离，优先考虑 |

---

## 8. ★ 修正：534 是**我的参数错误**，机制本身可用（2026-10-04 02:00-02:25）

### 8.1 我犯的两个错（教训：穷举变量前先核官方签名）

**错误 1：flag 常量值记错**

| Flag | 我用的 | 正确（微软文档） |
|---|---|---|
| `DISABLE_MAX_PRIVILEGE` | `0x02` ❌ | **`0x01`** |
| `SANDBOX_INERT` | 我当 DMP 用 ❌ | `0x02` |
| `LUA_TOKEN` | `0x04` ✓ | `0x04` |
| `WRITE_RESTRICTED` | `0x08` ✓ | `0x08` |

**错误 2（致命）：SID 传错了参数位**

```
CreateRestrictedToken(ExistingTokenHandle, Flags,
    DisableSidCount,  SidsToDisable,          ← 我一直把 restricting SID 传这里
    DeletePrivilegeCount, PrivilegesToDelete,
    RestrictedSidCount, SidsToRestrict,       ← ★ 该传这里（第 7 参）
    NewTokenHandle)
```

`SidsToDisable` 是 **deny-only** 列表（语义完全不同）。
改成传 `SidsToRestrict` 后 —— **5 种 flag 组合全部 `ok=1 err=0` 成功**。

⇒ 之前那个"`ERROR_NONE_MAPPED(534)` 说明 ctypes 封送不了、需上 koffi"的结论**是错的**。

### 8.2 修正后的实测

- ✅ `CreateRestrictedToken` 纯 ctypes **完全可用**（不需要 koffi）
- ✅ DACL 授权 `rc=0`，**不需要管理员**
- ✅ keep-alive 组 = **logon SID（从 `TokenUser` 取，不是 `TokenGroups`）+ Everyone + write SID**
- ⚠️ `TokenUser` 的 buffer **必须保活**（出函数即 GC ⇒ Sid 指针悬空 ⇒ 报 1337 `ERROR_INVALID_PARAMETER`）

### 8.3 ★★ 但最终仍不可行 —— 卡在更靠后的一环

决定性对照实验：

| 场景 | 结果 |
|---|---|
| A) `CreateProcessW` 无 token（基线） | ✅ rc=0 |
| B) `CreateProcessAsUserW` + **原始** token | ✅ rc=0 |
| C) `CreateProcessAsUserW` + **restricted** token | ❌ **rc=`0xC0000142`** |

⇒ **`CreateProcessAsUserW` 本身可用；是 restricted token 让子进程死在 DLL 初始化期。**

穷举 **4 种 keep-alive 组 × 3 种 creationflags = 12 组合，全死 `0xC0000142`**
（含 DSH 说"加了也死"的 console logon SID `S-1-2-1`；`CreateWellKnownSid` 本身可用）。

**与 DSH 记录完全一致** —— DSH 最终也移除了 console SID 并放弃了 console 隔离。

### 8.4 修订后的最终定论

| 阻塞 | 性质 |
|---|---|
| **① restricted token 的子进程在 Windows 上起不来** | `0xC0000142`，12/12 组合全死。**这条路本身不通**，不是实现问题 |
| ② 即使能跑，仍需换子进程启动器 | `asyncio.create_subprocess_exec` 无 token 参数 |

⇒ **不实施。** 这不是"标准库做不到"，而是 **Windows 上 restricted token + 实用子进程**走不通。

⇒ **改用环境重定向**（`npm_config_cache` / `PLAYWRIGHT_BROWSERS_PATH` / `TMP` → 会话私有目录）。
⚠️ 它不阻止桥乱写，只让桥写到指定处 —— 收益是"可控落点"而非"隔离"。

### 8.5 ctypes 坑位补记（在 §6.4 五条基础上）

6. `ConvertStringSidToSidW` 的 out 参数用 `POINTER(c_void_p)`。
7. `CreateWellKnownSid` 是 **6 参**；问大小时 `pSidBuffer` 传 `None`。
8. `SetNamedSecurityInfoW` 的 Owner/Group 传 `None` 时，argtypes **必须是 `c_void_p` 而非 `DWORD``。
9. ctypes 写字节：`c_ubyte*N` 下标赋值不接受 int；`memmove` 源只接受地址 ⇒ 用 `ctypes.pointer(c_char(v))`。
10. ★ **局部 buffer 里的 Sid 指针会随 buffer GC 悬空** ⇒ 保活（症状：1337）。

---

## 9. ★★★ 第四轮最终定论：**写限制成立**（02:45-03:20）

> ★ **§6 / §7 / §8 的"不可行"结论全部作废。** 本节是最终结论。
> 保留前几节仅作过程记录 —— 它们记录了我在同一个机制上被推翻三次的过程与原因。

验证脚本：`.workbuddy/_probe_ace_final.py`（其余临时 `_diag_*` 已清理）。

### 9.1 四项判定全 PASS

| 判定 | 含义 | 结果 |
|---|---|---|
| Q1 | 原始 token / 区内写（基线对照） | ✅ PASS |
| Q2 | restricted token / 区内写（应成功） | ✅ PASS |
| Q3 | restricted token / 区外写（**应被拒**） | ✅ PASS |
| **Q4** | **撤销授权后 / 区内写（应被拒，反向验证）** | ✅ PASS |

★ **Q4 是关键**：撤掉 `ws` 的 `WD+RC` ACE 后，同一个 token 也写不了
⇒ 证明 Q2 能写**确实是 ACE 在起作用**，不是碰巧。

授权后的实测 DACL：

```
D:AI(A;OICI;FA;;;WD)(A;OICI;FA;;;RC)(A;OICIID;FA;;;SY)(A;OICIID;FA;;;BA)(A;OICIID;FA;;;OW)
```

### 9.2 实施配方（三条）

**① DACL 必须 `(WD)` + `(RC)` 成对**

MSDN 2.4.2.4（Well-Known SID Structures）明文：

> RESTRICTED_CODE S-1-5-12 … Access is granted only if a token passes both tests.
> **Any ACL that specifies RC must also specify WD** — the "EVERYONE" token.

只写 `S-1-5-12` 而不写 `S-1-1-0` ⇒ **该 ACE 完全无效**（实测表现为"连授权目录都写不了"）。
**这一条是从"过严"翻到"成立"的关键。**

**② restricting SIDs = `WD + RC`，刻意不含用户 SID**

受限 token 的 access check 是**两道**：普通 SID 列表 + restricting SID 列表，两者都过才放行。
若 restricting 列表含用户 SID，则用户既有权限（`OW`=Owner Rights / `SY` / `BA`）能通过第二道检查
⇒ **区外也能写**。实测 `user+WD+RC`、`user+RC+WD`、`user+RC` 全部区外可写。

**③ flags** = `DISABLE_MAX_PRIVILEGE | LUA_TOKEN | WRITE_RESTRICTED`
（`LUA_TOKEN` 非必需，实测 `DMP|WR` 同样成立）

### 9.3 其它实测事实

- **`SetNamedSecurityInfoW` 是【合并】语义** —— 传新 DACL **不会**抹掉原有 ACE
  （实测授权后 `SY`/`BA`/`OW` 全在）。§8.5 里"PROTECTED 会抹掉用户 ACE 导致反转"的担心**不成立**。
- **capability SID（`S-1-4-*`）在普通文件 DACL 里不生效** —— 需 token 持有对应 capability，
  而 `CreateRestrictedToken` 不添加 ⇒ 走 capability SID 这条路行不通。
- 无 mandatory label（`LABEL_SECURITY_INFORMATION` 读回为空）⇒ 完整性级别不是阻碍。
- **restricted token 下能启动实用子进程**（`cmd`/`python` rc=0）。
  §8.3 观察到的 `0xC0000142 STATUS_DLL_INIT_FAILED` 是**被测程序选错**
  （PowerShell 加载大量 DLL；`whoami.exe` 需读 token 信息），**不是机制问题**。
- **`enforcement` 上限 = `partial`**：restricted token 只 intersect **写**权限，
  读 / 网络 / 进程可见性完全不受限。宣传成"沙箱"会夸大。

### 9.4 ★ 强烈建议走 SDDL 字符串路线

`ConvertStringSecurityDescriptorToSecurityDescriptorW`（字符串 → ACL）
与反向的 `ConvertSecurityDescriptorToStringSecurityDescriptorW`（ACL → 字符串）：

- **零手写字节**（`SetEntriesInAclW` 的 `EXPLICIT_ACCESS` / `TRUSTEE` 结构体我错了两次）
- **可审计**：DACL 就是一行可读 SDDL，能直接落进配置与日志
- **可可靠读回**：反向 API 拿字符串比对，比读二进制头可靠

### 9.5 仍未实施 —— 唯一剩的阻塞

`asyncio.create_subprocess_exec` **无 token 参数**（实测签名
`(program, args, stdin, stdout, stderr, limit, kwds)`）
⇒ 必须把 `agent/core/mcp_client.py:187` 的 spawn 换成 `CreateProcessAsUserW` 封装。

**改动面**：管道创建 + 读循环 + **进程组整棵收尸必须保留**
（`mcp_client.py:253` 那套 `uvx→uv→python` 收尸能力实测曾堆积 6 天，不能退化）+ S4 死亡可见归因。

⇒ 结论从"整条路不通"改为：**机制已验证，只差换 spawn 这一个阻塞**。
若目标只是"别让桥乱写系统目录"，**环境重定向**（`npm_config_cache` / `PLAYWRIGHT_BROWSERS_PATH` /
`TMP` → 会话私有目录）仍是代价低得多的选择 —— ⚠️ 但它**不阻止乱写，只让桥写到指定处**。

### 9.6 四轮里我犯的 7 处错（全是同一类：凭记忆猜常量/签名）

| # | 错的 | 真相 |
|---|---|---|
| 1 | `DISABLE_MAX_PRIVILEGE = 0x02` | `0x01` |
| 2 | restricting SID 传 `SidsToDisable` | 传**第 7 参 `SidsToRestrict`**（之前报 534） |
| 3 | `TokenRestrictedSids = 21` | `11`（21 是 `TokenHasRestrictions`） |
| 4 | `TOKEN_GROUPS_AND_PRIVILEGES` stride = 12 | x64 上 **16** |
| 5 | `ACL.AceCount` 读偏移 8 | 偏移 **4** ⇒ §6 的"ACE 数 = 0"是**假读数** |
| 6 | `SetEntriesInAclW` 按 4 参但传错位 | 首参是 **`ULONG cCountOfExplicitEntries`**；返回 **DWORD 错误码**，ACL 走 out 参数 |
| 7 | `TRUSTEE` 声明 3 字段 | **5 字段**（含 `MultipleTrusteeOperation`），SID 要放最后的 `ptstrName` |

**三条铁律**：

1. **穷举变量前先核官方签名/常量** —— 上面 7 处查一次 MSDN 就有，
   而我在 flag 上穷举了三轮，代价是三次错误结论。
2. **「成功码」与「读回验证」都可能是假的** —— `SetNamedSecurityInfoW` 返回 0 不代表 ACE 生效；
   而读回路径本身也可能读错（第 5 条）。真判据只能是**功能测试 + 反向验证**。
3. **选被测程序要最小化** —— 拿 PowerShell 测会把环境问题误判成机制问题。
