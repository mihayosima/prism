# S4 — 工具 / MCP 源头护栏（`source_clip_budget` 接线）设计方案

- **状态**：**✅ 已实施**（v3：v2 评审补正 7 条 + 实现期校正 8 处，见 §8）
- **日期**：2026-09-26
- **对应**：`docs/memory/上下文装配链路-现状与完善方案.md` 的 **S4**（= 原路线图的 **G3**）
- **前序**：G1 分块装配（`aa82b6f`）、S3 循环内上下文治理（`196546d`）均已落地
- **v2 变更**：① 点 4 前提实测推翻 → 幂等须**依赖定点硬裁**（非"天然"）；② 连带修正验收项 1 判据；③ dict 处理补"保留通道非空才让位"边界；④ 明确 guard_source 含**形态策略**、不含**额度策略**
- **v3 变更**：定点硬裁收敛方式二次修正 + 修掉 S3 遗留的末条保底外壳漏算缺陷（见 §8）

---

## 0. 目标与边界

### 0.1 目标

让**巨量工具 / MCP 输出在进入模型上下文之前**被裁到 token 口径上限，堵住"注入入口未净化"这个缺口。

一句话：**护栏保证不爆（源头修剪），G1/S3 保证够用（预算内精裁）。**

### 0.2 边界（不做的事）

- **不做**语义摘要 / 内容提炼（那是记忆层的职责）；护栏只做**头尾保留 + 省略标注**。
- **不新立第二套 token 口径**——上限定多少仍由 `plugins.context` 唯一决定。
- **不改** guard（白名单 / 风险分级）与审批链路——护栏是"结果体积"治理，不是"能不能调用"治理。
- **不引入**向量化 / 智能挑选保留片段（后置，留接缝）。

### 0.3 沿用三条职责红线（S3 已钉死）

| 关切 | 归属 | 本方案中的体现 |
|---|---|---|
| 内容怎么裁、占多少 token | `plugins.context` | 只出**数字**（`source_clip_budget()`），是唯一 token 语义持有方 |
| 任务进度到哪了 | `plugins.work_memory` | **零改动** |
| 谁编排 | 消费方（`sys` / `plugins.react`） | 只在**执行边界**接线，不持策略 |

### 0.4 一处必须先说清的区分：形态策略 ≠ 额度策略

v1 曾笼统声称 `guard_source`「不含策略」。**这个说法过宽，v2 收紧为两层**：

| 层 | 内容 | 归属 | 理由 |
|---|---|---|---|
| **额度策略** | 上限定多少（8192？） | `plugins.context` 唯一决定 | token 语义只有一个持有方（S3 红线） |
| **形态策略** | dict 各通道如何取舍（structured 优先、让位通道清空） | **`core/tokens.guard_source` 内置** | 这是"外部源头返回值的形态知识"，与 token 额度无关；放 core 才能被 sys 与 plugins 共同复用而不重复实现 |

即：`guard_source` 是**源净化原语**——**无额度策略，有形态规则**。它不认识 `observe`/`think`/`act`，只认识"一个外部返回值该怎么在体积约束下保持结构可辨"。

---

## 1. 现状取证（file:line 实证）

| # | 位置 | 事实 | 问题 |
|---|---|---|---|
| 1 | `core/mcp_client.py:65-72` | `call_tool` 返回 `{content, text, structured}`，**零裁剪** | 源头未净化 |
| 2 | `plugins/windows_mcp/__init__.py:89-94`、`plugins/playwright_mcp/__init__.py:108-113` | handler 直接 `return await client.call_tool(...)` | 两个 MCP 桥都不裁 |
| 3 | `plugins/react/components.py:123,142` | `DefaultActor(max_chars=2000)` → `truncate(result, 2000)` | **字符口径**，且是插件内硬编码 |
| 4 | `plugins/react/prompt.py:81-86` | `truncate` 按 `len(s)` 字符切，标注 `…(truncated N chars)` | 与 token 口径无关；CJK 下 2000 字符 ≈ 1500 token，ASCII 下 ≈ 500 token，**同一配置实际差异 3 倍** |
| 5 | `plugins/react/loop.py:66`、`plugins/react/__init__.py:66,75-76` | `tool_result_max_chars: int = 2000` 配置键 | 用户可调却按字符，语义误导 |
| 6 | `sys/chat.py:498-499` | 单步路径 `result` 原样进 `evidence` | 单步路径**完全无裁剪** |
| 7 | `sys/chat.py:508-514` | `_narrate` 把 `json.dumps({"plan","evidence"})` 拼进 **rigid** 块 `answer_head` | 巨量结果直接烘进刚性块 |
| 8 | `sys/tasks.py:120-147` | `invoke` 是**唯一执行 choke point**（`pre-execute` waterfall → schema → 执行 → `post-execute`） | 已有天然接线点，且当前**未**利用 |
| 9 | `plugins/context/governor.py:106-108` | `source_clip_budget()` 恒等返回 `source_clip_tokens` | **全仓零消费方** |
| **10** | **`plugins/context/governor.py:246-257`** | **`_clip_hard`：因 `clip_text` 的省略标注自身占 token、结果会略超预算，故按超出比例定点迭代收缩至实占 ≤ 预算** | **同一坑 S3 已踩过并已解决** —— S4 必须复用，而非重犯 |
| **11** | **`sys/tasks.py:17-25` + `agent/api.py:30,63-66`** | **`sys/tasks` 已 `from agent.api import ...`；`api.py` 已转出 `clip_text / clip_json / estimate_tokens`** | **点 5 分层疑问：确认已满足，零风险** |

### 1.1 一个已被忽略的隐患：三套口径并存

仓内目前同时存在三个"工具结果上限"，互不知晓：

- `source_clip_tokens = 8192`（token，治理器，**未接线**）
- `tool_result_max_chars = 2000`（**字符**，react 内硬编码）
- `clip_max_tokens = 1000`（token，S3 循环内**老观察折叠**阈值）

其中前两者语义重叠（都想当"单条结果上限"），单位却不同。**S4 必须先消掉这个二义性**，否则 S3 刚立的"token 语义只有一家"在工具结果这条线上就破了。

### 1.2 【v2 新增】实测：`clip_text` 不保证 `≤ max_tokens`

用真实 `core.tokens` 跑（`max_tokens = 8192`，构造 ~40 倍超限载荷）：

| 样本 | 原值 tokens | 第 1 次裁后 | 是否 ≤ 8192 | 第 2 次裁后 | 二次 omitted | 稳定？ |
|---|---|---|---|---|---|---|
| ASCII | 300372 | **8199** | ✗ | 8197 | 8 | ✗ |
| CJK | 311296 | **8199** | ✗ | 8202 | 3 | ✗ |
| MIXED | 354985 | **8198** | ✗ | 8199 | 5 | ✗ |

**两条硬结论**：

1. **裁后 ≠ ≤ 上限**：`_OMIT_MARKER = "[...省略 {n} tokens...]"` 自身占 token，把结果顶到 8198~8199（超出 6~7）。→ **v1 的验收项 1（`estimate_tokens <= 8192`）按字面无法达成，必须改用"定点硬裁"。**
2. **`clip_text` 不幂等**：第二遍仍会再裁（净减 3~8 token），`stable=False`。→ **v1 的「嵌套 invoke 天然幂等」是错的**，幂等须**以定点硬裁为前提**才成立（见 §3.5 点 4）。

> 旁证：`test_context_governor.py:57` 的注释「标注本身有少量开销」只断言 `estimate(out) < estimate(big)`，**从未断言 `<= max_tokens`** —— 说明该缺口此前被有意绕开，而非已解决。

---

## 2. 关键裁决

### D1 — 接线位置：选「执行边界单一 choke point」

| 候选 | 位置 | 覆盖 | 优点 | 缺点 |
|---|---|---|---|---|
| A 源头 | `core/mcp_client.call_tool` | 仅 MCP | 最早净化、省内存 | core 不持有 token 语义 → 需 `max_tokens` 入参；**不覆盖普通任务**；两个 MCP 桥各改一处 |
| B 消费侧 | react `DefaultActor` + `chat._narrate` | react + 单步 | 贴合"进模型前" | **两处实现 → 漂移风险**；非 MCP 工具与未来新路径都要各接一次 |
| **C 执行边界** | **`sys/tasks.invoke`** | **全部工具（MCP 与非 MCP）** | **一处实现、零漂移、自动覆盖两条路径** | 结果在离开工具时即被裁（见 D5 副作用） |

**裁决：C 为主。** 理由：

1. `invoke` 是**机器权威边界**——结果在此刻离开工具、进入调用方；在此净化收益最高、实现最少。
2. 与职责红线一致：`sys` 可 `import agent.api`（含 `clip_text` / `estimate_tokens`）；预算经 `ctx.get(Services.CONTEXT)` **软依赖**取得，不破坏分层。
3. A 的"省内存"优势在本项目规模下不成立——结果本就要进 `evidence` / chain；真遇超大载荷再叠加 A 即可（**增量、不返工**）。

### D2 — 裁剪动作：新增 core 原语 `guard_source`（**v2 强化**）

新增 `core/tokens.guard_source(value, max_tokens) -> tuple[Any, int]`：

- **未超限 → `(value, 0)` 原样返回**（小结果**零损耗**，`is` 同一对象）；
- `max_tokens is None` **或** `<= 0` → **不裁**（返回原值——与 `clip_text` 的"清空"语义**有意相反**）；
- `dict`：按形态规则选保留通道（structured 优先 → text → 整体 JSON），**让位通道清空为 `None`（不删键）**；
- `str` → 定点硬裁；
- 其它 → `json.dumps` 后定点硬裁。

**v2 关键修正**：裁剪一律走**定点硬裁**（保证 `estimate(裁后) <= max_tokens`），**不是**直接调 `clip_text`（后者会超，见 §1.2）。定点逻辑与 `governor._clip_hard` 同源——建议上提 core，消除重复实现（见 §3.1 / Q6）。

**为什么放 core**：core 是 sys 与 plugins **共同允许** import 的唯一层；且 `tokens.py` 已是裁剪原语的归属地（其 `clip_json` docstring 已声明"结构化保留属调用方上层职责"）。**无额度策略**——上限仍由 context 出；**有形态规则**（见 §0.4）。

### D3 — 上限口径：本轮保持固定上限，动态钳制列为 S4b

`source_clip_budget()` 现为固定值 8192。理论隐患是"一条结果可用掉 41% 输入预算"。

**裁决：本轮不动它的语义（固定上限），把"动态钳制"单列为 S4b（可选）。** 理由：

- 固定 8192 已远优于"无上限"；且 **S3 已保证** `total_used <= input_budget`——即使单条吃满 8192，窗口总账仍受硬上限约束。
- 动态钳制需要在调用点知道"其它 token 占用"（prior 等），而那是 **chat 的预算闸范畴**（同 S3 的 `_ensure_react_headroom`），放进来会让 `invoke` 反向感知会话上下文，**职责越界**。
- 避免过度设计。若后续需要，S4b 定义：`effective = min(source_clip_tokens, max(0, input_budget() - other_tokens))`，在 chat 侧装配前计算并下传。

### D4 — 消除旧字符口径（S3 遗留）

- react `DefaultActor` **移除字符截断**，仅保留 `as_text`（把结果转 JSON 字符串——观察需要文本，但**不再定上限**）。
- `ReactConfig.tool_result_max_chars`（`loop.py:66`）**删除**（避免"改了没反应"的误导配置）；`react/__init__.py:66,75-76` 同步简化。
- `prompt.truncate`（`prompt.py:81-86`）**随调用方一并移除**（`__all__` 同步），其 `…(truncated N chars)` 字样由守卫测试禁回（验收项 8）。
- 删后**唯一**的"单条结果上限" = `source_clip_budget()`（源头护栏）；`clip_max_tokens=1000` 只保留 S3 的"**循环内老观察折叠**"语义，两者正交：
  - **8192** = 单条**源头**上限（一进仓就裁）；
  - **1000** = 循环内**老**观察折叠阈值（`_prepare_chain`，只折老的不折新的）。

### D5 — 副作用接受：`evidence` / UI 也看到裁后内容

在 `invoke` 处裁 → `evidence.result`、以及前端「事件」页展示的，都是**净化后**内容。

**裁决：接受。** 理由：① 巨量原文对 UI 展示同样无意义，反而拖垮前端传输；② 若"保留原文仅供展示"，就必须回到 B 方案的双实现，得不偿失；③ 原文仍可从**工具自身**（MCP / 外部系统）按需重新获取，护栏不制造新真相。

### D6 — 观测：复用 `TASKS_POST_EXECUTE`，不新开事件

`invoke` 已在 `sys/tasks.py:143` 发 `TASKS_POST_EXECUTE`。**扩展字段**即可：

```json
{ "task": "mcp.windows.Powershell", "caller": "plugins.react",
  "ok": true, "span_ms": 420,
  "clipped": true, "omitted_tokens": 292173, "limit_tokens": 8192 }
```

不注册新事件名（SSOT 守卫要求"常量被源码引用"），零新增登记负担。

---

## 3. 详细设计

### 3.1 新增原语（`core/tokens.py`）

```python
def guard_source(value: Any, max_tokens: int | None) -> tuple[Any, int]:
    """把单次外部源头结果按 token 上限净化（结构化优先于纯文本）。

    契约（**钉死，含与 clip_text 的有意分歧**）：
    - 未超限 → ``(value, 0)`` **原样返回**（小结果零损耗，对象同一 ``is``）；
    - ``max_tokens is None`` 或 ``<= 0`` → **不裁**（返回原值）。
      注意：这与 ``clip_text`` 相反（后者 ``<=0`` 返回空串）——**原语是"裁"，护栏是"防爆"**，
      护栏宁可放过、不可误清空。此分歧有双向单测锁定，不得"顺手统一"。
    - dict 形态规则（**让位通道清空为 ``None``，保留键**，避免下游 KeyError）：
        * 有 ``structured`` 且非空 → 保留 structured（定点硬裁），``content``/``text`` 清空为 None；
        * 否则有 ``text`` 且非空   → 保留 text（定点硬裁），``content`` 清空为 None；
        * 否则（无可用文本通道）   → 整体 ``json.dumps`` 后硬裁，**返回裁后字符串**
          （与下游 ``as_text`` 同构；仅在超限时改变类型）。
      *边界*：仅当**保留通道非空**才让位另一通道——若 ``text`` 为空串而 ``content`` 非空，
      则以 content 为保留通道，**不得**把两者都清空（防止内容全丢）。
    - str → 定点硬裁；其它 → ``json.dumps`` 后定点硬裁。
    - 一律走**定点硬裁**（result 实占 <= max_tokens），**不得**直接用 ``clip_text`` 的返回值
      —— 省略标注自身占 token，会致结果略超上限（见设计稿 §1.2 实测）。

    返回 ``(净化后的值, omitted_tokens)``，其中
    ``omitted_tokens = estimate_tokens(原值) - estimate_tokens(裁后值)``
    （**净减少量**，而非 clip_text 返回的"中段 token 数"）。
    二者定义不同：净减少量已含标注开销、**且涵盖"清空让位通道"的节省**（dict 场景下
    clip_text 的返回值根本不覆盖该部分，若沿用会系统性少报）。

    **含形态策略，不含额度策略**：上限定多少由调用方
    （``plugins.context.source_clip_budget()``）决定，符合"token 语义唯一归属 context"。
    """
```

**定点硬裁的落位（Q6 二选一）**：

- **方案 (ii) 推荐**：把 `governor._clip_hard:246-257` 的定点迭代逻辑**上提为 core 公共原语**
  （如 `clip_text_hard(text, budget)`），`guard_source` 调它，`governor._clip_hard` 退化为薄封装
  （或直接删除改调公共原语）。→ **全仓唯一 token 裁剪实现**，与 S3「token 语义只有一家」同调。
- **方案 (i) 保守**：`guard_source` 内联一份 4 轮定点迭代（约 6 行），暂不动 governor。
  缺点：同一算法两份实现，存在漂移风险。

`agent/api.py` 同步 re-export（`guard_source` 加入 `line 30` 的 import 与 `__all__`），使 sys / plugins 经唯一出口引用。

### 3.2 接线（`sys/tasks.py::invoke`）

```python
# 在 invoke 内新增私有辅助（**整函数体 self-try**，保证绝不外抛）
def _guard_result(ctx, result):
    """源头护栏：软依赖取额度 → guard_source。**任何异常都不外抛**，
    返回 (原值, (False, 0, None))——护栏不得成为新的失败点。"""
    try:
        gov = ctx.get(Services.CONTEXT)                       # 软依赖；无则跳过
        budget = getattr(gov, "source_clip_budget", None)
        if not callable(budget):
            return result, (False, 0, None)
        limit = int(budget())
        guarded, omitted = guard_source(result, limit)
        return guarded, (omitted > 0, omitted, limit)
    except Exception:                                        # noqa: BLE001
        logger.warning("source guard 失效，回退原值（task 结果未净化）", exc_info=True)
        return result, (False, 0, None)
```

接线点（**最小 diff**，插在 `result = await cmd.invoke(kwargs)` 之后、`TASKS_POST_EXECUTE` emit 之前）：

```python
started = time.monotonic()
ok, error = True, None
clipped, omitted, limit = False, 0, None
try:
    result = await cmd.invoke(kwargs)
    result, (clipped, omitted, limit) = _guard_result(ctx, result)   # ← 新增
except Exception as exc:
    ok, error = False, str(exc)
    raise
finally:
    span_ms = int((time.monotonic() - started) * 1000)
    await ctx.events.emit(Events.TASKS_POST_EXECUTE, {
        "task": name, "caller": caller,
        "ok": ok, "span_ms": span_ms, "error": error,
        "clipped": clipped, "omitted_tokens": omitted, "limit_tokens": limit,   # ← 新增
    })
return result
```

**异常隔离的三重保证**（钉死）：
1. **try 范围**：只包 `_guard_result` 的**函数体**（`budget()` 调用 + `guard_source` 调用），**不包** `cmd.invoke`（后者有自己的 try/`raise`，失败必须照常上抛）；
2. **异常返回原值**（不是空值/None），任务成功态不受污染（`ok` 仍为 `True`）；
3. **日志级别 `warning`**（`exc_info=True`）：任务是**成功**的，用 `error` 会误导；但护栏失效必须可见，故不用 `info`/`debug`。

- 无 `context` 插件 / 无该方法 → **不裁**（与 G1 / S3 全线降级一致，不裸崩、不劣化）。
- `omitted > 0` → `TASKS_POST_EXECUTE` 随行 `clipped=True` / `omitted_tokens` / `limit_tokens`。

### 3.3 react 去字符口径（`plugins/react/`）

- `components.py`：`DefaultActor.__init__` 去掉 `max_chars`；`_one` 返回 `ToolResult(call.name, as_text(result), tool_call_id=call.id)`。
- `prompt.py`：`truncate` **删除**（含 `__all__` 导出）——`…(truncated N chars)` 字样由守卫测试禁回。
- `loop.py`：删 `ReactConfig.tool_result_max_chars`。
- `__init__.py`：构造 `DefaultActor(caller="plugins.react")`，去掉 `max_chars` 与配置读取。

### 3.4 降级矩阵

| 缺失项 | 行为 |
|---|---|
| 无 `plugins.context` | 不裁（原样返回），不裸崩 |
| `source_clip_budget()` 不存在（旧注入实现） | `getattr` 守卫 → 不裁 |
| `max_tokens is None` / `<= 0`（用户设为 0） | 不裁（宁可放过） |
| `guard_source` / `budget()` 抛异常 | `_guard_result` 内 self-try → `warning` + 返回**原值**（护栏**不得**成为新的失败点） |

### 3.5 「评审补正 7 条」逐条裁决

| # | 补正要点 | 裁决 | 落地 |
|---|---|---|---|
| 1 | `max_tokens <= 0` 语义钉死；`None` 也视为不裁 | ✅ **采纳**（并收紧） | 签名 `int \| None`；判序 `is None or <= 0` → 不裁；**明示与 `clip_text` 相反**，双向单测锁定（§3.1） |
| 2 | `omitted_tokens` = 原值 token − 裁后 token | ✅ **采纳**（并加理由） | 由 `guard_source` **自行计算**，不沿用 `clip_text` 返回值：① 差一个标注开销；② **dict 清空通道的节省 clip_text 根本没覆盖**，沿用会少报（§3.1） |
| 3 | 异常处理：try 只包 guard、返回原值、记 warning | ✅ **采纳**（并落地为辅助函数） | 抽 `_guard_result`，**整函数体 self-try**；返回原值；`warning + exc_info`；插在 emit 之前（否则事件拿不到字段）（§3.2） |
| 4 | 嵌套 invoke 天然幂等 → 加单测 | ⚠️ **前提修正**（**实测推翻**） | **直接用 `clip_text` 不幂等**（实测 8199 > 8192、二遍仍裁 3~8）。正确表述：**采用定点硬裁后才幂等**。单测改为锁定该不变量；并诚实标注"**当前仓内无"工具调工具"路径**（`Task.invoke` 只收 `kwargs`，handler 不持 ctx），此为**面向未来的锁定**"（§1.2 / §3.1） |
| 5 | `sys/tasks` 能否 import `core/tokens`？会不会破分层 | ✅ **已满足，零改动风险** | `sys/tasks.py:17-25` **已** `from agent.api import ...`；`agent/api.py:30,63-66` **已**转出 `clip_text/clip_json/estimate_tokens`；`test_layered_import_direction` 对 `agent.api` **不作层映射**（`t[0]=="api"` → continue），sys→api 永合法。S4 只需把 `guard_source` **加进 `api.py`** 的 import + `__all__` |
| 6 | dict 的"丢弃"要具体：清空 vs 删除 | ✅ **采纳**（并补边界） | **清空不删键**（保持 dict 结构，避免下游 KeyError）；值用 **`None`**（非 `""`——`""` 会被误读为"工具返回空串"）。**补充边界**：仅当**保留通道非空**才让位另一通道，否则 `text="" + content` 非空时会把内容全丢（§3.1） |
| 7 | 验收项 8 用 grep 式守卫断言防旧口径回归 | ✅ **采纳**（并定位置） | 静态扫描 `…(truncated` / `tool_result_max_chars` 无残留；**新建 `agent/tests/test_source_guard.py`** 承载（理由：`test_plugin_decoupling.py` 三轴边界明确，塞第四轴会稀释；"谁破坏 S4 谁在 S4 测试里红"更内聚）。**扫描须排除 tests 自身**——否则测试内的断言字符串会自触发（复用 `_py_files()` 的排除口径） |

---

## 4. 与既有阶段的衔接

| 阶段 | 关系 |
|---|---|
| **G1 分块装配** | 护栏是"源头"；G1 是"装配期"。二者叠加后，`answer_head` 等 rigid 块的实际体积已受源头约束，G1 的刚性降级**从"常态"降为"兜底"** |
| **S3 循环内治理** | S4 消除 S3 遗留的 `2000 字符`旧口径；`clip_max_tokens=1000` 折叠阈值**不受影响**（正交语义，见 D4）。**S4 复用 S3 的 `_clip_hard` 定点思路**（Q6 建议上提为公共原语） |
| **S5 / G4（记忆配额反推）** | 无关，独立推进 |
| **G2（已由 S3 覆盖）** | 无关 |

---

## 5. 分步实施（共 6 步）

1. **原语**：`core/tokens.py` 新增 `guard_source`（含定点硬裁；Q6 若选方案 (ii) 同时上提 `clip_text_hard` 并让 `governor._clip_hard` 复用）；`agent/api.py` re-export；单测
   （未超限零损耗 / `None` 与 `<=0` 不裁 / dict structured 优先 / **保留通道非空才让位** / dict 仅 text / 整体 JSON / 非结构化 / 裁后**严格 ≤ 上限** / **二次裁 omitted == 0 且同对象**）。
2. **接线**：`sys/tasks.py::invoke` 接 `_guard_result` + 事件字段；单测
   （有治理器裁 / 无治理器不裁不崩 / MCP dict 结构化优先 / 小结果零损耗 / **`guard_source` 抛异常时 invoke 仍返回原值且 `ok=True`** / 事件字段齐备）。
3. **去旧口径**：`plugins/react/{components,prompt,loop,__init__}.py` 移除字符截断与 `truncate`；
   更新 `test_react.py::test_large_tool_result_truncated`（`test_react.py:175-189`，原断言 `"truncated"` 字样 → 改在 `invoke` 层断言 token 口径标注 `[...省略 N tokens...]`）。
4. **守卫测试**：新建 `agent/tests/test_source_guard.py`，含旧口径静态扫描（排除 tests 自身）。
5. **文档同步**：`上下文装配链路-现状与完善方案.md`（S4 标 ✅、§1.5 配置表 `source_clip_tokens` 备注改"已接线"、§2.2 G3 标已实现）；
   `上下文治理-设计方案.md` 的 G3 节标"已实现（接线点调整为 `sys/tasks.invoke`）"。
6. **验证**：全量 pytest（现 228 + 新增）+ 端到端（构造返回 ~50k token 的假工具，断言进模型 ≤ 8192）。

---

## 6. 验收标准

| # | 验收项 | 判据 |
|---|---|---|
| 1 | 巨量结果被裁到上限 | 单工具返回 ~50k token → `invoke` 返回值 `estimate_tokens <= 8192`（**须定点硬裁；裸 `clip_text` 会到 8199，见 §1.2**） |
| 2 | 关键行保留 | 裁剪后首尾内容存在，中段被 `[...省略 N tokens...]` 替换 |
| 3 | 小结果零损耗 | 未超限时返回**同一对象**（`is` 相等），字节不变 |
| 4 | 结构化优先 | 结果含 `structured` 时保留结构化表示，`content`/`text` **清空为 None**（键保留） |
| 5 | 两桥均受控 | `windows_mcp` / `playwright_mcp` 工具返回值均 ≤ 上限（经 `invoke`，无需各改） |
| 6 | 降级不裸崩 | 停用 `plugins.context` → 不裁、不报错，主链路正常 |
| 7 | 单步路径同样受控 | `chat._narrate` 的 `answer_head` 体积受源头约束（经同一 `invoke`） |
| 8 | 旧字符口径已清除 | 静态扫描：非测试源码内无 `…(truncated`、无 `tool_result_max_chars`（守卫式断言，扫描排除 tests 自身） |
| 9 | 可观测 | `TASKS_POST_EXECUTE` 含 `clipped` / `omitted_tokens` / `limit_tokens` |
| 10 | 回归 | 全量 pytest 绿（228 + 新增）；`test_plugin_decoupling` 四轴不变（无新增硬依赖） |
| **11** | **护栏不外抛** | monkeypatch `guard_source` 抛异常 → `invoke` 仍返回**原值**、`ok=True`、`omitted=0`，且日志含 warning（验收"护栏不得成为新的失败点"） |
| **12** | **幂等（前置定点硬裁）** | 对已裁结果再跑 `guard_source` → `omitted == 0` 且返回同一对象；合成嵌套 `invoke`（内层裁到上限）→ 外层 `omitted == 0` |

---

## 7. 风险与取舍

| 风险 | 评估 | 缓解 |
|---|---|---|
| 护栏裁掉"本该完整看到"的内容 | 低——8192 token 对绝大多数工具结果已足够宽 | 可调（设置视图）；头尾保留保证结构可辨 |
| **（v2 新增）裁后仍略超上限** | 中——`_OMIT_MARKER` 自身占 token，实测超 6~7 | **改用定点硬裁**（复用 `governor._clip_hard` 思路上提 core）；验收项 1 据此判定 |
| `invoke` 成为热点路径的性能负担 | 极低——`estimate_tokens` 为 O(len)，且未超限时短路（**先估长度再决定是否 `json.dumps`**） | 保持"未超限零损耗"短路 |
| 去掉 react 字符截断后，单条观察可能变大 | 中——原本上限 2000 字符（CJK ≈1500 token），现放宽到 8192 token | 由 S3 的 `clip_max_tokens=1000` 在老观察折叠环节兜住；新观察大一点是**有意的**（保真） |
| 护栏异常掀翻工具调用 | 中 | `_guard_result` 整函数体 self-try → 返回**原值** + `warning`（验收项 11） |
| 把定点逻辑上提 core 触动 S3/G1 | 低—中（Q6 方案 ii） | 改动集中在 `core/tokens`；`clip_text` 语义（头尾比例、标注格式、`<=0` 清空）**不变**，仅使"裁后实占 ≤ 上限"成立；需全量跑 `test_context_governor` 复核 |

---

## 8. 待拍板项

1. **Q1 接线位置**：采用 **C（`sys/tasks.invoke` 单一 choke point）**？还是坚持设计稿原意的 **A（`core/mcp_client.call_tool` 源头）**？（本方案推荐 C，A 可作 S4b 增量叠加。）
2. **Q2 上限口径**：本轮保持**固定 8192**（推荐），动态钳制列入 S4b？
3. **Q3 副作用**：接受 `evidence` / UI 看到的也是**净化后**内容（推荐接受）？
4. **Q4 降级**：无 `plugins.context` 时**不裁**（推荐，与全线降级一致）？还是退回 core 内置常量（会破坏"数字唯一来源"）？
5. **Q5 清理范围**：react 的 `tool_result_max_chars` 配置键 + `truncate` **删除**（推荐）还是保留但忽略（避免用户既有配置报错）？
6. **（v2 新增）Q6 定点硬裁落位**：
   - **方案 (ii)（推荐）**：把 `governor._clip_hard` 上提为 `core/tokens.clip_text_hard`，`guard_source` 与 governor **共用一份**——全仓唯一裁剪实现，合 S3 纪律，但要复核 `test_context_governor` 断言面；
   - **方案 (i)（保守）**：`guard_source` 内联 4 轮定点，暂不动 governor——零回归风险，但同算法两份实现。

## 8. 实现状态与实现期校正（v3）

**已实施**。拍板结果：Q1=**C**（`sys/tasks.invoke`）、Q2=**固定 8192**、Q3=**接受副作用**、
Q4=**无插件不裁**、Q5=**删除**旧配置键与 `truncate`、Q6=**方案 (ii)**（定点硬裁上提 core 共用）。

### 8.1 交付清单

| 层 | 文件 | 内容 |
|---|---|---|
| 原语 | `core/tokens.py` | 新增 `clip_text_hard`（定点硬裁）+ `guard_source` + 私有 `_guard_dict` / `_guard_value` / `_safe_tokens` / `_dumps` / `_nonempty` |
| 出口 | `agent/api.py` | re-export `clip_text_hard` / `guard_source` |
| 治理器 | `plugins/context/governor.py` | `_clip_hard` 退化为薄封装（**复用** `clip_text_hard`）；**修末条保底外壳漏算**（见 8.2②） |
| 接线 | `sys/tasks.py` | 新增 `_guard_result`（整函数体 self-try）；`invoke` 在 emit 前净化；事件加 3 字段 |
| 去旧口径 | `plugins/react/{components,prompt,loop,__init__}.py` | 删 `max_chars` / `truncate` / `tool_result_max_chars` |
| 测试 | `tests/test_source_guard.py`（新建 13 项）· `tests/test_react.py`（用例 7 改写）· `tests/test_react_context.py`（去重） | — |

### 8.2 实现期校正（相对 v2 设计稿）

1. **定点硬裁的收敛方式二次修正（重要）**：governor 原式 `target = budget²/t` 只适用于
   **小幅超出**（governor 调用前已先 `clip_text` 过一次），直接用于源头护栏会两类翻车——
   ① **大幅超出时过度截断**：实测 5 万 token → **只剩 1349**；② **在已裁结果上迭代**时，
   上一轮的标注字符被当作内容"回收"，不动点恒高于上限（实测 CJK **卡在 8198**）。
   改为「**每轮都从原文重裁、按超出量递减目标**」后达标：
   `ASCII 8191 / CJK 8191 / MIXED 8192`（均 ≤ 8192），且**二次裁剪 `omitted == 0`、`stable=True`**（真幂等）。

2. **修掉 S3 遗留缺陷：`_trim_messages` 末条保底漏算 message 外壳**。原实现只把 **content 字符串**
   裁到 `budget`，未计 `{"role":…,"content":…,"seq":…}` 外壳（≈11 token）→ 块 `used` **超出**
   `allocated`（实测 `history` 234 > 225），全量用例 `test_assemble_total_within_budget_when_flexible` 失败。
   此前之所以"通过"，纯因旧 `_clip_hard` 的过度收缩（4000 字符被砍到 **55** token）**掩盖**了该缺陷——
   即：旧口径的欠用与本次的越界互为遮蔽。修法：先扣外壳开销再裁 content，并按残差再收一次。

3. **dict 通道规则收紧**：把 `content` 列为**第三顺位保留通道**（`structured → text → content`），
   而非 v2 写的"无可用文本通道就整体 JSON 化"。如此"保留通道非空才让位"**天然成立**，
   而"整体 JSON 化返回字符串"仅在**三通道全空**时触发（此时无内容可丢，只剩其它键）。

4. **内核禁词守卫拦截**：`core` 不得出现业务词，`test_agent_purity` 禁用词含「策略」「场景」→
   `guard_source` 措辞改为「**含形态取舍、不含额度取舍**」（语义不变，文档仍可用"策略"）。

5. **测试落位**：新建 `tests/test_source_guard.py`（13 项）。口径守卫用 **tokenize 剔除注释**后扫描
   ——**注释允许提及历史**（如"旧 X 已删除"），**代码与字符串不得残留**旧口径。

6. **`test_react.py` 的 `_pm` 增 `context` 开关**（默认 `False`，用例 7 置 `True`）：
   源头护栏需治理器在线；其余用例保持原状，零影响。

7. **修掉 `test_react_context.py` 的重复定义**：`test_ten_plus_step_chain_stays_within_budget`
   被定义了**两次**（前者为死代码），已去重。

8. **顺带澄清**：`TASKS_POST_EXECUTE` 是 **emit（总线/live）模式**，**不落** `agent_events` 表；
   消费方是 `sys/progress.py`（**透传整个 payload**，无字段白名单）→ 裁剪遥测自动直达前端进度流。

### 8.3 验收结果

| # | 验收项 | 结果 |
|---|---|---|
| 1 | 巨量结果被裁到上限 | ✅ 端到端 50k token 工具 → **8191 ≤ 8192** |
| 2 | 关键行保留 | ✅ 首尾保留 + `[...省略 N tokens...]` |
| 3 | 小结果零损耗 | ✅ 未超限返回**同一对象**、`clipped=False` / `omitted=0` |
| 4 | 结构化优先 | ✅ `content`/`text` **清空为 None、键保留** |
| 5 | 两桥均受控 | ✅ 经同一 `invoke` choke point（无需各改） |
| 6 | 降级不裸崩 | ✅ 停用 `plugins.context` → 原样返回 |
| 7 | 单步路径同样受控 | ✅ 同经 `invoke` |
| 8 | 旧字符口径已清除 | ✅ `test_no_legacy_char_clip_wording_in_source`（13 项之一） |
| 9 | 可观测 | ✅ 事件含 `clipped`/`omitted_tokens`/`limit_tokens` |
| 10 | 回归 | ✅ **241 passed / 2 failed**（2 项为 Redis 环境敏感既有用例，与本改造无关；基线同） |
| 11 | 护栏不外抛 | ✅ monkeypatch 抛异常 → 返回**原值**、`ok=True` |
| 12 | 幂等（前置定点硬裁） | ✅ 二次裁剪 `omitted == 0` 且同对象；嵌套 `invoke` 外层不重复裁 |

> 实施完成（未提交）。剩余动作：提交 + 重启 8720 使新代码在运行进程中生效。

