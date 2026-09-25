# 工作记忆（第 3 层）— Redis 缓存设计方案

> 状态：待审。系统记忆（第 1 层）已落地并通过全量回归；本方案聚焦第 3 层「工作记忆」。
> 定位：任务级进度缓存，任务结束即失效；存储后端 = Redis（用户指定）。

---

## 0. 目标与边界

| 项 | 内容 |
|---|---|
| 层 | 记忆架构第 3 层：工作记忆 |
| 存什么 | 任务完成进度（step / tool_trace / checkpoints / status），**不存对话全文** |
| 寿命 | 任务级——`run()` 开始建、结束 DEL；兜底 TTL 防泄漏 |
| 后端 | Redis（外部服务）；Redis 不可用时降级为进程内 dict，功能不崩 |
| 形态 | 系统层插件 `agent.sys.work_memory`，`provides=[Services.WORK_MEMORY]` |

**不触碰**：系统记忆（文件，已落）、长期记忆（`plugins.memory`）、短期记忆（`agent_messages` 表 + 滚动摘要，属 G1 范畴）。

---

## 1. 现状盘点（已核对代码）

- `agent/plugins/react/loop.py`：工作态全在 `run()` 闭包——`steps:int`、`tool_trace:list[str]`、`observed`（观察结果）。任务结束随函数返回消失。
- `_record()`（`loop.py:220`）写 `ctx.state[StateKeys.REACT_LAST]` = 最近 run 摘要（steps/tools/tokens）。`ctx.state` 是内存态，进程重启即失。
- `agent/core/state_keys.py`：`REACT_LAST` / `LOOP_RUNNING` 已存在；工作记忆新增独立服务键，**不替换**它们（`LOOP_RUNNING` 是调度层在跑集合，`REACT_LAST` 是跨 run 历史摘要）。
- `pyproject.toml`：无 redis 依赖 → 需新增 `redis>=5.0`。
- `profiles.SYS_PLUGINS`：已含 `SystemMemory`；工作记忆同列系统层。

---

## 2. 设计

### 2.1 形态与装配
- 系统层插件 `agent/sys/work_memory.py`：`provides=[Services.WORK_MEMORY]`、`inject=[]`（纯缓存服务，零业务依赖）。
- 装配进 `profiles.SYS_PLUGINS`（与 `SystemMemory` 同级，内核级能力，硬装配）。
- 新增 `core/services_catalog.py` 登记 `Services.WORK_MEMORY`。

### 2.2 服务接口（接缝可注入，Protocol 留插件内）
```python
class WorkMemoryLike(Protocol):
    async def begin(self, task_id: str, meta: dict) -> None: ...       # 任务开始：SET + TTL
    async def update(self, task_id: str, **fields) -> None: ...        # 进度更新：HSET
    async def checkpoint(self, task_id: str, name: str, data: Any) -> None: ...  # 检查点
    async def get(self, task_id: str) -> dict | None: ...              # 读进度
    async def finish(self, task_id: str) -> None: ...                  # 任务结束：DEL
    async def list_active(self, session_id: str | None = None) -> list[str]: ...  # 在跑任务（可选）
```
- 接缝消费：`wm = ctx.get(Services.WORK_MEMORY) or InMemoryWorkMemory()`——Redis 不可用时退化内存实现，接口一致。

### 2.3 Redis 键空间
- 键前缀：`wm:`（可配 `prefix`）。
- 任务键：`wm:{task_id}`（Redis hash）
  - fields：`session_id`、`run_id`、`status`(started/running/done/failed)、`step`、`max_steps`、`tools`(json list)、`last_action`、`checkpoints`(json)、`started_at`、`updated_at`
- TTL：任务级，默认 `ttl=3600s` 兜底；`finish()` 主动 `DEL`。
- 可选 session 索引：`wm:idx:{session_id}` = set(task_ids)，供 `list_active(session_id)`。

### 2.4 生命周期绑定
| 信号 | 动作 |
|---|---|
| `run()` 开始 | `begin(task_id, meta{ session_id, run_id, max_steps })` |
| 每步 `_record` | `update(task_id, step=..., tools=..., last_action=...)` + 可选 `checkpoint` |
| `run()` 正常结束 / 步数触顶 / 硬失败 | `finish(task_id)` → `DEL` |
| 兜底 | TTL 到期自动过期（防结束信号丢失泄漏） |
| 跨重启恢复（**可选增强**） | 若 `run()` 重启且 key 存在且 `status=running` → 读 step/tool_trace 续跑；方案声明为可选，默认不启用 |

### 2.5 与记忆架构其他层的边界（澄清第 6 决策点）
- **工作记忆** = 任务进度态（step / tool_trace / checkpoints / status）。承接 react 闭包里的 `steps`/`tool_trace`/`observed`，**不存对话全文**。
- **短期记忆** = 对话历史 `messages`（仍走 `agent_messages` 表 + 滚动摘要，属 G1）。
- **长期记忆** = 跨会话知识（`plugins.memory`）。
- 三者正交：工作记忆补齐了「任务进行中可查、可恢复」的空白，不与短期/长期重叠。

### 2.6 降级纪律（守解耦）
- Redis 未配置 / 连不上 / 导入失败 → 降级为进程内 `InMemoryWorkMemory`（dict 实现，接口同）；功能不崩，仅跨进程/重启无效。
- 消费方（react）经 `ctx.get(Services.WORK_MEMORY)` 接缝，**不 import** 具体 Redis 实现。
- 插件停用/降级 → react 退回当前内存闭包行为（不写工作记忆），与现状等价。

### 2.7 配置
- `agent.work_memory` 节：`url`(redis://localhost:6379/0)、`ttl`(3600)、`prefix`("wm:")、`enabled`(true)。
- `core/config_schema.py` 放行该节（`_any` 或具体 schema）。

### 2.8 checkpoints 语义约定

`checkpoints` 是工作记忆 hash 中的**命名里程碑快照列表**，与 `step` / `tools` / `last_action` 等「当前进度」字段正交：

- `update` 字段 = 单值、每步覆盖，记「现在到哪了」；
- `checkpoints` = 追加式列表（可保留多个），记「哪些节点是稳的、可回退的」。

**单个 checkpoint 内容 schema**（限体积，复用 G0 `clip_json`）：
```json
{
  "name": "after_file_written",
  "step": 7,
  "ts": "2026-09-25T23:20:00Z",
  "data": { "tool_trace": ["..."], "last_action": "...", "note": "文件已落盘，待编译" }
}
```

**触发点（W2 接入时，react 在以下边界调用 `checkpoint()`）**：
1. 一个**子目标完成之后**（如「文件已写，待编译」）——把阶段性成果固化为可回退点；
2. 调用**不可逆 / 高风险动作之前**（删除、发送、支付、外部写）——先留锚点，失败时能回退；
3. `run()` **跨重启续跑的恢复锚点**（呼应决策点 #2，若启用续跑）。

**价值**：后续步骤失败或走偏时，可从最后一个 good checkpoint 续跑 / 回退，不必从头重放整条轨迹。这正是它相对 `update` 的本质区别——`update` 只记当前态，`checkpoints` 保留可回退的历史节点。

**限容**：`data.tool_trace` 经 `clip_json(max_tokens=...)` 截断；`checkpoints` 列表本身设上限（默认保留最近 10 个），超出丢弃最旧，防 hash 膨胀。

---

## 3. 实施步骤（审查通过后）

**W1 — 插件骨架 + Redis 客户端 + 降级**
- `pyproject.toml` 加 `redis>=5.0` 主依赖（保证导入）；导入失败时告警并降级内存版。
- `agent/sys/work_memory.py`：`WorkMemory` 插件（Redis 实现）+ `InMemoryWorkMemory`（降级）。
- `core/services_catalog.py` 登记 `Services.WORK_MEMORY`；`core/config_schema.py` 放行 `agent.work_memory`。
- `profiles.SYS_PLUGINS` 加 `WorkMemory`。

**W2 — react 接入（消费面）**
- `run()` 开始 `begin`、每步 `update`/`checkpoint`、结束 `finish`；接缝 `ctx.get(...)` 获取，降级透明。
- 现有 `REACT_LAST`/`LOOP_RUNNING` 保留不动（职责不同）。

**W3 — 测试 + 回归**
- `tests/test_work_memory.py`：生命周期（begin→update→get→finish 后 DEL）、TTL 过期、降级（无 Redis → 内存）、与 react 集成（run 中写入、结束清除）。
- 跑全量 pytest，保持绿后统一提交。

---

## 4. 风险与决策点

| # | 项 | 建议 / 待定 |
|---|---|---|
| 1 | redis 主依赖 vs optional extra | 建议主依赖（用户指定用 redis）；降级内存版保证无 Redis 也能跑。 |
| 2 | 跨重启恢复 | 默认不启用（可选增强），避免与 react 幂等语义耦合过深。 |
| 3 | task_id 来源 | 复用现有 `session_id`+`run_id` 组合；需确认 run 是否有稳定 run_id（当前 `loop.py:149` 每步传 `session_id`，run_id 可生成 uuid）。 |
| 4 | checkpoints 语义与体积 | **已定（见 §2.8）**：触发点 = 子目标完成 / 不可逆动作前 / 续跑锚点；内容 schema = name+step+ts+data(tool_trace/last_action/note)；体积经 `clip_json` 限容 + 列表上限 10。 |
| 5 | 与 G1 短期记忆压缩的时序 | 工作记忆 finish 后可触发短期摘要，但本方案不耦合，留 G1 处理。 |

---

## 5. 验收

- 任务运行期可在 Redis 查到 `wm:{task_id}` 进度；任务结束 key 被 DEL。
- Redis 宕机 → 自动降级内存版，对话/任务照常，无异常。
- 全量 pytest 通过；SSOT 守卫（`Services.WORK_MEMORY` 被源码引用）、K0 纯度守卫不破。
- 停用插件 → react 退回内存闭包行为，与现状等价。
