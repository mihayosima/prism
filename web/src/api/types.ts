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

// ---------- 设置 ----------

export interface SettingsResponse {
  [k: string]: unknown
}

export interface SettingsPatchRequest {
  patch?: Record<string, unknown>
  reset?: boolean
}
