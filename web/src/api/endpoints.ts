/**
 * API 契约层 · 端点函数化封装（B2.1）
 * ------------------------------------------------------------------
 * 把 legacy `app.js` 的 13 处 `fetch` 调用点收敛为具名函数。
 * 每条均标注其 legacy 对应行号，便于迁移期双向核对。
 *
 * 覆盖清单（legacy 行号 → 本文件函数）：
 *   app.js:194  POST /api/chat/stream      → streamChat（见 sse.ts）
 *   app.js:252  POST /api/approval/answer  → answerApproval
 *   app.js:308  POST /api/approval/answer  → answerApproval（澄清应答复用同端点）
 *   app.js:352  GET  /api/status           → getStatus
 *   app.js:357  GET  /api/session          → getSession
 *   app.js:379  GET  /api/{name}           → loadResource（events/perms/memory/tools/settings/plugins）
 *   app.js:452  POST /api/plugins/{act}    → pluginAction
 *   app.js:563  POST /api/settings         → saveSettings
 *   app.js:571  POST /api/settings (reset) → resetSettings
 *   app.js:587  POST /api/approval/mode    → setApprovalMode
 *   app.js:598  POST /api/session/reset    → resetSession
 *   app.js:614  POST /api/approval/mode    → setApprovalMode
 *   app.js:619  GET  /api/approval/pending → getApprovalPending
 *
 * 时间：2026-09-30 11:06 (GMT+8)
 */

import { get, post, put, del } from './client'
import type {
  StatusResponse,
  SessionResponse,
  Row,
  ApprovalAnswerRequest,
  ApprovalAnswerResponse,
  ApprovalMode,
  ApprovalModeRequest,
  ApprovalModeResponse,
  ApprovalPendingResponse,
  PluginActionRequest,
  PluginActionResponse,
  SettingsPatchRequest,
  SettingsResponse,
  SettingsScope,
} from './types'

// ---------- 状态 / 会话 ----------

/** legacy app.js:352 — 运行状态（provider/model/budget） */
export function getStatus(): Promise<StatusResponse> {
  return get<StatusResponse>('/status')
}

/** legacy app.js:357 — 会话信息 */
export function getSession(): Promise<SessionResponse> {
  return get<SessionResponse>('/session')
}

/** legacy app.js:598 — 重置会话（清空消息历史） */
export function resetSession(): Promise<Record<string, unknown>> {
  return post<Record<string, unknown>>('/session/reset')
}

// ---------- 审批 / 权限 ----------

/** legacy app.js:252、308 — 提交审批或澄清应答 */
export function answerApproval(body: ApprovalAnswerRequest): Promise<ApprovalAnswerResponse> {
  return post<ApprovalAnswerResponse>('/approval/answer', body)
}

/** legacy app.js:587、614 — 切换权限档位（bypass 需 confirm:true） */
export function setApprovalMode(mode: ApprovalMode, confirm = false): Promise<ApprovalModeResponse> {
  const body: ApprovalModeRequest = { mode, confirm }
  return post<ApprovalModeResponse>('/approval/mode', body)
}

/** legacy app.js:619 — 刷新后恢复档位显示 + 在途弹窗/问询卡 */
export function getApprovalPending(): Promise<ApprovalPendingResponse> {
  return get<ApprovalPendingResponse>('/approval/pending')
}

// ---------- 通用资源加载 ----------

/** `load(name)` 支持的名字（对应 GET /api/{name}） */
export type ResourceName = 'events' | 'perms' | 'memory' | 'tools' | 'settings' | 'plugins' | 'context' | 'skills'

/** legacy app.js:379 — 通用列表加载；返回体形状随资源而异，交由调用方按需收窄 */
export function loadResource<T = Row[] | Row>(name: ResourceName): Promise<T> {
  return get<T>('/' + name)
}

// ---------- 插件 ----------

/** legacy app.js:452 — 插件启停/脚手架/移除（act 拼进路径） */
export function pluginAction(payload: PluginActionRequest): Promise<PluginActionResponse> {
  return post<PluginActionResponse>('/plugins/' + payload.act, payload.body || {})
}

// ---------- 技能（Skill 管理面） ----------

/** GET /api/skills（?q= 搜索）→ 技能清单 */
export function skillsList(q = ''): Promise<SkillsResponse> {
  return q ? get<SkillsResponse>('/skills', { q }) : get<SkillsResponse>('/skills')
}

/** GET /api/skills/{name} → 单个技能完整内容 */
export function skillGet(name: string): Promise<SkillRow> {
  return get<SkillRow>('/skills/' + encodeURIComponent(name))
}

/** POST /api/skills → 新增技能（默认写盘） */
export function skillAdd(body: Record<string, unknown>): Promise<SkillRow> {
  return post<SkillRow>('/skills', body)
}

/** PUT /api/skills/{name} → 更新/启停 */
export function skillUpdate(name: string, body: Record<string, unknown>): Promise<SkillRow> {
  return put<SkillRow>('/skills/' + encodeURIComponent(name), body)
}

/** DELETE /api/skills/{name} → 移除（默认写盘删文件） */
export function skillRemove(name: string, body?: Record<string, unknown>): Promise<Record<string, unknown>> {
  return del<Record<string, unknown>>('/skills/' + encodeURIComponent(name), body)
}

/** POST /api/skills/{name}/reload → 从磁盘重载（仅文件型） */
export function skillReload(name: string): Promise<SkillRow> {
  return post<SkillRow>('/skills/' + encodeURIComponent(name) + '/reload')
}

// ---------- 设置 ----------

/** legacy app.js:563 — 保存配置补丁（G2：可指定写入作用域，默认容器级） */
export function saveSettings(
  patch: Record<string, unknown>,
  scope: SettingsScope = 'container',
): Promise<SettingsResponse> {
  const body: SettingsPatchRequest = { patch, scope }
  return post<SettingsResponse>('/settings', body)
}

/** legacy app.js:571 — 恢复默认配置（容器级：清除容器覆盖） */
export function resetSettings(): Promise<SettingsResponse> {
  const body: SettingsPatchRequest = { reset: true }
  return post<SettingsResponse>('/settings', body)
}

/** G2 — 「重置为继承」：只清**本会话**的覆盖，逐键回落到容器/默认（不重建容器） */
export function resetSessionSettings(): Promise<SettingsResponse> {
  const body: SettingsPatchRequest = { reset: true, scope: 'session' }
  return post<SettingsResponse>('/settings', body)
}

// ---------- 重导出：SSE ----------

export { streamChat, SSE_EVENTS } from './sse'
export type { SseFrameHandler, StreamChatOptions, SseEventName } from './sse'
