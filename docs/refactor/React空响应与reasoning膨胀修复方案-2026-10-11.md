# React 空响应静默失败与 reasoning 上下文膨胀修复方案

**编制时间：** 2026-10-11 00:52（GMT+8）
**文档性质：** 修复方案（**已实施**：C 已交付；A 核心 + 重试增强已交付；B 经核查无需改代码，设置页翻 `on` 即生效）
**适用范围：** `agent/plugins/react/`、`agent/sys/llm.py`、`agent/profiles.py`、`agent/core/tokens.py`、`agent/plugins/context/governor.py`

## 更新记录

| 时间 | 版本 | 变更摘要 |
|---|---|---|
| 2026-10-11 00:52 | v1 | 首版。基于代码实证给出 A/B/C 三处修复方案，并纠正此前两轮分析中的两处误判。 |
| 2026-10-11 01:40 | v2 | **实施落地**：C（`max_tokens` 设置页可调，container 级）已交付；A 核心（截断透传+收敛判失败）与 A 重试增强（空+截断时调大 max_tokens+抑制思考重试一次）已交付并通过回归；B 经核查为设置项 `compact_mode` 翻 `on` 即生效，**无需改代码**，本文原 B 手写截断方案作废。 |

---

## 0. 此前两轮分析的两处误判（先澄清，避免方案建立在错误事实上）

1. **"审计全空白"是误判。**
   - 前文用 `WHERE ts >= ...` 查 `agent_events`，但该表**真实列名是 `at`**（无 `ts` 列），查询直接报错返回空集，被我误读为"零记录"。
   - 用正确列名重查后，本轮会话（`default:s-mv2l01jm-7x48rczy`）实有 **`agent/llm/truncated` 2 条**事件落库（`durable`），正好对应两次空响应（seq 650、652）。
   - **结论：** 截断信号本就有审计，无需补审计埋点；真正的缺口在"收敛处未消费该信号做降级反馈"，而非"无事件"。

2. **"回灌优化没解决问题"措辞不当。**
   - 用户的 B-fix（提交 `8e15204`）解决的是 **DeepSeek 历史回传必须携带 `reasoning_content`，否则 HTTP 400**——这是协议硬约束，**正确且必要**，本轮空响应与膨胀问题都不归它管。

---

## 1. 已核实根因（均带行号，代码实证）

### 1.1 空响应（静默失败）—— P0

| 环节 | 代码点位 | 行为 |
|---|---|---|
| 生成上限 | `profiles.py:36` `max_tokens: 8192` → `llm.py:382-384` 写入请求体 | reasoning 与 content **共享** 8192 预算 |
| 触发截断 | `llm.py:445` `out["truncated"] = True`（当 `finish_reason == "length"`） | reasoning 吃满 → content 为空 |
| 信号落库 | `llm.py:131-134` `await durable(ctx, Events.LLM_TRUNCATED, ...)` | 已落库（实证见 §0） |
| **信号丢失** | `components.py:65` `class ReasonOutput` 无 `truncated` 字段；`components.py:121` 构造时未透传 | **`truncated` 停在 dict 层，`ReasonOutput` 对象拿不到** |
| 静默占位 | `loop.py:811` `ReactResult(ok=True, text=out.text or "（未给出回答）", ...)` | `text` 空且 `truncated` 被完全忽略 → 用户只见诡异空回复，`ok=True` 让上层以为成功 |

> 业界佐证：Claude Code 官方把同类现象（thinking 占满 output 上限、content 无 text 块）列为确认 bug（issue #68882），期望行为是检测后提示"thinking budget consumed all output tokens"。本仓缺陷形态与之同源。

### 1.2 历史 reasoning 上下文膨胀 —— P1

- **回灌本身不可去除**：`loop.py:784-786` 把 `out.reasoning_content` 存入 turn 并 `task.chain.append`；续接时 `messages.extend(chain)`。DeepSeek 要求历史 assistant 带该字段，去掉即 400（这是 B-fix 的初衷，正确）。
- **`estimate_tokens` 计入 reasoning**：`tokens.py:22-30` 对整对象 `json.dumps` 估算，故 `reasoning_content` 占用 token 预算，逐轮膨胀。
- **治理器裁剪不保留 reasoning**：`_summarize_units`（`loop.py:1197-1210`）只提取 content/tool_calls/tool_results 压成纯文本"过程摘要"，**丢弃 reasoning_content**（摘要字符串无该字段）。
- **compact 默认不执行**：`governor.py:81` `_DEFAULT_COMPACT_MODE = "shadow"`、`profiles.py:68` `compact_mode: "shadow"` —— 仅算决策、不真裁。本仓 react chain 已注册 `on_compact=_chain_compactor`（`loop.py:1181`），但 shadow 模式下不触发。

> 业界佐证：Claude Code 用 auto-compact（窗口达 83.5% 自动摘要整段历史）消膨胀；Codex 用服务端 `encrypted_content`（不累积明文 reasoning）。DeepSeek 无加密推理态，本仓只能明文回灌，正解在"压缩"而非"不回灌"。

---

## 2. 修复方案（三处，均不动 B-fix 回灌逻辑）

### 方案 A（P0，最该修）：修复空响应静默失败

**改动点：**

- **A1. `components.py:65`** —— `ReasonOutput` 增加字段：
  ```python
  truncated: bool = False   # E：正文被 max_tokens 腰斩（reasoning 占满输出），须透传闭环
  ```
- **A2. `components.py:121`** —— 构造处透传（与 `reasoning_content` 同口径）：
  ```python
  return ReasonOutput(
      text=str(data.get("text") or ""),
      tool_calls=calls,
      reasoning_content=str(data.get("reasoning_content") or ""),
      truncated=bool(data.get("truncated")),   # ← 新增透传
  )
  ```
- **A3. `loop.py:811`** —— 收敛分支改为"检测即降级 + 重试一次"：
  ```python
  if not out.text and getattr(out, "truncated", False):
      # reasoning 占满输出预算 → 正文为空，非成功
      # 首选：剥离 reasoning 重试一次（注入 system_note 或直接调大本次 max_tokens）
      if not retried_once:
          retried_once = True
          # 以「直接给最终答复、勿展开思考过程」重试；重试逻辑须复用现有 reason 调用
          ...
      return ReactResult(
          ok=False,
          text="（reasoning 占满输出额度，已重试仍无正文，请改换模型或放大输出上限）",
          steps=steps, tokens_in=tokens_in, tokens_out=tokens_out,
          tools=tool_trace, observations=observed, run_id=run_id,
      )
  return ReactResult(ok=True, text=out.text, ...)
  ```
  - 重试**限 1 次**（防成本/延迟失控）；注入文案与 "改换模型 / 放大输出上限" 提示。
  - 收敛处为 "空 text + truncated" 补一条 durable 事件（如 `agent/react/empty-on-truncated`），与既有 `LLM_TRUNCATED` 形成闭环。

**为什么这样改：** 信号在 `llm.py` 已产生并落库，只差"透传到 `ReasonOutput` + 收敛处消费"。改动纯增量，不影响既有字段。

### 方案 B（P1）：压缩历史 reasoning 上下文（消除膨胀副作用）

**推荐落点：在 `_prepare_chain` 做副本裁剪（shadow-safe，不触碰 B-fix 回灌）：**

- **B1. `loop.py:1049` `_prepare_chain`** —— 对 `type == "think"` 的**旧条目**（序号 `i < cut`）生成**新 dict 副本**，把 `reasoning_content` 截断到约 1024 tokens（超长截前段 + 标注「…已裁剪思考过程」）；**必须改副本、不动原 `chain`**（否则存档失真、下次加载也失真）。
- **B2. 回传给 DeepSeek 的请求仍保留完整 `reasoning_content`**（不过裁，保 400 合规）；**仅喂给下一轮模型窗口的副本**裁剪。这是与紧凑 `compact` 互不冲突的 shadow-safe 做法。

**关于借力治理器 `compact` 的边界：**
- 本仓 react chain 已注册 `on_compact`，但默认 `shadow` 不执行；且 `_summarize_units` 生成的摘要**不含 `reasoning_content`**——若真正执行 compact，被压缩单元回传给 DeepSeek 时缺该字段会触发 400。
- 故 **B 不依赖治理器 compact 切换执行模式**，改用 `_prepare_chain` 副本裁剪，规避 400 风险；治理器 compact 保持 shadow 不变。

**验证硬约束（必做）：** 真 DeepSeek 跑一轮，确认裁剪后历史回传**仍不 400**（DeepSeek 仅校验字段存在性，不校验内容完整性，理论上安全，须实测确认）。

### 方案 C（根因 / 配置层）：调大单次生成上限

- **`profiles.py:36`** `max_tokens: 8192 → 16384`（DeepSeek-Flash 支持更大值则 32768）。
- 依据：Anthropic 官方口径——`max_tokens` 是 thinking + output 总和，须 > thinking budget，否则无 token 留给正文。
- 约束：`agent.budget.max_tokens_per_run = 200000`（`profiles.py:37`）已兜底，调大单次上限总体可控。

---

## 3. 实施顺序与验证

| 批次 | 内容 | 风险 | 说明 |
|---|---|---|---|
| **批次 1（建议先做）** | A1+A2+A3 + C | 低 | A 纯增量、消除用户可见空回复；C 单行配置降触顶概率 |
| **批次 2（单独排期）** | B1 副本裁剪 | 中 | 需真机验证 DeepSeek 400 边界 |
| 后续可选 | 评估 `compact_mode` 由 shadow 切执行 | 高 | 影响面大，需独立灰度，非本次必需 |

**验证清单：**
1. fixture provider 模拟 `finish_reason=length` 且空 text → 确认 A3 命中 `ok=False` + 重试/降级提示（不静默）。
2. 真 DeepSeek 跑一轮确认 B1 裁剪后历史回传不 400。
3. 回归 `test_api_400_fixes.py`（B-fix 400 用例）不被破坏。
4. 全量 pytest（按本仓纪律：设 `CODEBUDDY_SAFE_DELETE_SANDBOX=0`，逐文件独立 basetemp）。

---

## 4. 风险与回滚

- **A**：新增 `truncated` 字段为纯增量，不改变既有接口；重试限 1 次防成本失控。可逐行回退。
- **C**：单行配置变更，回退即还原 8192。
- **B**：副本裁剪不改原链存档，属非破坏性；唯一风险是 400，已用"回传留完整 / 窗口裁副本"隔离。

---

## 5. 不做的事（明确边界）

- **不去除 B-fix 回灌** —— 那是 DeepSeek 400 合规必需，去除即回归事故。
- **不切换 compact 执行模式**作为本次主要手段 —— 有 400 风险，改用 shadow-safe 的 `_prepare_chain` 副本裁剪。
- **不新增截断审计事件** —— `LLM_TRUNCATED` 已落库，只需在收敛处消费闭环。
