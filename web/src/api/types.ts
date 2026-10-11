/**
 * API 契约层 · 类型定义（B2.1）
 * ------------------------------------------------------------------
 * 唯一 DTO 真源。字段来源 = 后端 `agent/run/chat.py` 各端点实际返回结构
 * + legacy `app.js` 的消费点（字段名以其读法为准）。
 *
 * 纪律：视图/store 不得自行 `any` 化响应；新增字段先在此登记。
 * 时间：2026-09-30 11:06 (GMT+8)
 */

// ---------- 通用 ----------

/** 后端统一错误体（401/500 等） */
export interface ApiError {
  error: string
  detail?: string
}

/** 分页/列表端点通用的行记录（字段随视图而异，宽类型兜底） */
export type Row = Record<string, unknown>

// ---------- status / session ----------

export interface BudgetInfo {
  spent: number
  max: number
}

export interface StatusResponse {
  provider_label: string
  model: string
  version: string
  session_id: string
  budget: BudgetInfo
}

export interface SessionResponse {
  session_id?: string
  [k: string]: unknown
}

// ---------- 会话 ----------

export interface ChatRequest {
  message: string
}

/** SSE `final` 帧携带的对账证据 */
export interface Evidence {
  intent?: string
  tool?: string
  ok?: boolean
  result?: string
  ping?: string
  note?: string
  error?: string
  tokens_in?: number
  tokens_out?: number
}

export interface PlanInfo {
  intent?: string
  tool?: string | null
  args?: Record<string, unknown> | null
}

/** 历史消息上挂载的执行证据（addMsg 第三参） */
export interface MessageEvidence {
  plan?: PlanInfo
  evidence?: Evidence
  tokens_out?: number
  tokens_in?: number
}

// ---------- SSE 帧（event + payload） ----------

/** 帧名常量见 sse.ts 的 SseEvent；此处给出各帧 payload 形状 */

/** `start` 帧 payload */
export interface StartPayload {
  run_id: string
}

/** `progress` 帧 payload */
export interface ProgressPayload {
  source?: string
  data?: Record<string, unknown>
}

/** `token` / `thinking` 帧 payload */
export interface TextDeltaPayload {
  data?: { text?: string }
}

/** `final` 帧 payload */
export interface FinalPayload {
  text?: string
  evidence?: Evidence
  /** 阶段 2（D9）：自动续接等可见提示（可选展示） */
  notice?: string
}

/** `error` 帧 payload */
export interface ErrorPayload {
  error?: string
}

/** 审批帧（approval / approval_done）payload */
export interface ApprovalPayload {
  call_id?: string
  tool?: string
  risk?: string
  mode?: string
  kwargs?: Record<string, unknown>
  resolved?: boolean
  approved?: boolean
  always?: boolean
}

/** 澄清帧（ask_user / ask_user_done）payload */
export interface AskUserPayload {
  kind?: string
  call_id?: string
  question?: string
  options?: AskOption[] | string[]
  multi?: boolean
  [k: string]: unknown
}

export interface AskOption {
  label?: string
  value?: string
  desc?: string
  description?: string
}

// ---------- 审批 / 权限 ----------

export interface ApprovalAnswerRequest {
  call_id: string
  approved: boolean
  always?: boolean
}

export interface ApprovalAnswerResponse {
  ok: boolean
  resolved?: boolean
  error?: string
}

export type ApprovalMode = 'default' | 'plan' | 'auto' | 'bypass'

export interface ApprovalModeRequest {
  mode: ApprovalMode
  confirm?: boolean
}

export interface ApprovalModeResponse {
  ok: boolean
  mode?: ApprovalMode
  error?: string
}

export interface ApprovalPendingResponse {
  mode?: ApprovalMode
  items?: Array<{ req?: AskUserPayload & ApprovalPayload }>
}

// ---------- 事件 ----------

export interface EventRow {
  ts?: string
  type?: string
  [k: string]: unknown
}

export interface EventsResponse {
  /** 后端实际返回字段（chat.py `/api/events`） */
  rows?: EventRow[]
  events?: EventRow[]
  items?: EventRow[]
}

// ---------- 上下文账本（P3） ----------

export interface ContextResponse {
  rounds?: Row[]
  [k: string]: unknown
}

// ---------- 工具 / 记忆 / 插件 ----------

export interface ToolRow {
  name?: string
  description?: string
  risk?: string
  params?: Record<string, unknown>
  required?: string[]
  [k: string]: unknown
}

export interface ToolsResponse {
  tools?: ToolRow[]
  items?: ToolRow[]
}

export interface MemoryRow {
  id?: number | string
  text?: string
  score?: number
  accessed_count?: number
  invalid_at?: string | null
  [k: string]: unknown
}

export interface MemoryResponse {
  memories?: MemoryRow[]
  items?: MemoryRow[]
  [k: string]: unknown
}

export interface PluginRow {
  name?: string
  enabled?: boolean
  provides?: string[] | string
  [k: string]: unknown
}

export interface PluginsResponse {
  plugins?: PluginRow[]
  items?: PluginRow[]
}

export type PluginAction = 'enable' | 'disable' | 'scaffold' | 'remove'

export interface PluginActionRequest {
  act: PluginAction
  body?: Record<string, unknown> | null
}

export interface PluginActionResponse {
  ok: boolean
  error?: string
  [k: string]: unknown
}

// ---------- 技能（Skill） ----------

/** 单个技能卡片（与后端 SkillsCatalog._card 对齐） */
export interface SkillRow {
  name: string
  description: string
  risk: string
  owner: string
  tags: string[]
  version: string
  has_run: boolean
  enabled: boolean
  source: string
  content?: string
  [k: string]: unknown
}

export interface SkillsResponse {
  skills: SkillRow[]
  enabled: boolean
}

// ---------- 设置 ----------

/** 可用工具域选项（设置页以此渲染 tool_domains 的可勾选清单） */
export interface SettingsDomainOption {
  /** 域 = 工具注册来源 owner（插件 id），如 plugins.windows_mcp */
  name: string
  /** 该域当前的工具数；available=false 时为 0 */
  count: number
  /** 是否当前已注册（false = 配置里已选但该插件当前停用，保留以便不改动用户选择） */
  available: boolean
}

export interface SettingsResponse {
  /** 生效值（容器 ∪ 本会话，缺项回落默认）——旧字段名，保持兼容 */
  values?: Record<string, unknown>
  domains?: SettingsDomainOption[]
  context_plugin_enabled?: boolean
  approval_enabled?: boolean
  /** G7：外部工具**隔离档位**插件（plugins.sandbox）是否启用（目录插件默认关） */
  sandbox_enabled?: boolean
  /**
   * ★ 沙箱自报的**声明档位**（`none` / `read-only` / `workspace-write`）。
   * 声明 ≠ 兑现 —— 要看 `sandbox_enforcement`。
   */
  sandbox_mode?: string
  /**
   * ★ 沙箱自报的**实际隔离强度**（`full` / `partial` / `none`）。
   * **当前后端无进程级隔离实现 ⇒ 恒为 `none`**。这不是缺陷陈述，是事实陈述 ——
   * 但绝不是"受保护"。缺这个字段时前端只能猜"正常"，那正是此前的误导源，
   * 所以它必须由后端**自报**而非前端推断（对齐 DeepSeek Harness 的 `SandboxEnforcement`）。
   */
  sandbox_enforcement?: string
  /** 声明了档位但兑现不了（当前仅 mode≠none 时为 true） */
  sandbox_gap?: boolean
  /** 沙箱自述覆盖的工具名模式（隔离按桥配置，固定 mcp.* 域） */
  sandbox_targets?: string[]
  /** 自述理由（人类可读，排查"为什么没保护"的唯一线索） */
  sandbox_reason?: string
  /**
   * ★ 落点（2026-10-04）：声明档位时，外部工具子进程被挪到该目录
   * （cwd + 缓存/临时目录重定向）。空串 = 未重定向（`mode=none`）。
   *
   * ⚠️ **它不代表"受保护"**：重定向挡不住绝对路径写入。与 `sandbox_enforcement`
   * 分开报正是为了不让人把"东西落在哪"误读成"隔离有多强"。
   */
  sandbox_workspace?: string
  /** 是否真的做了环境重定向（与 `sandbox_workspace` 配套，避免靠路径猜） */
  sandbox_env_redirected?: boolean
  /** 当前请求落在哪个会话键（`{user}:{session}`） */
  session_id?: string
  /** 允许写到「本会话」层的设置键白名单（G2 会话级化） */
  scopable?: string[]
  /** 容器级生效值（本会话未覆盖时的基线） */
  container?: Record<string, unknown>
  /** 本会话**显式**覆盖（仅用户确实写过的键，用于区分「继承 / 覆盖」） */
  session?: Record<string, unknown>
  /** 同 values：容器 ∪ 本会话的生效值 */
  effective?: Record<string, unknown>
  [k: string]: unknown
}

export interface SettingsPatchRequest {
  patch?: Record<string, unknown>
  reset?: boolean
  /** 写入作用域：container（默认，全局）| session（仅本会话） */
  scope?: SettingsScope
}

/** 设置写入作用域（G2 会话级化） */
export type SettingsScope = 'container' | 'session'
