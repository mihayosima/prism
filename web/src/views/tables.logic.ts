/**
 * 表格类视图逻辑（B2.4：Events / Perms / Memory / Tools）
 * ------------------------------------------------------------------
 * 迁移自 legacy app.js:378-443。全部为**纯函数**，逐字符可对拍。
 * 时间：2026-09-30 11:45 (GMT+8)
 */

import { esc, json2, tableFrom } from '../components/table'
import type { Row, ToolRow } from '../api/types'

// ================= EventsView =================

/** 事件表列（legacy app.js:383） */
export const EVENT_COLS = ['seq', 'event', 'at', 'payload'] as const

/**
 * 事件过滤（legacy app.js:381-382）：
 * `all` 全通过；否则按 `event` 前缀匹配。
 */
export function filterEvents(rows: Row[], filter: string): Row[] {
  return rows.filter((r) => {
    if (filter === 'all') return true
    return String(r.event || '').startsWith(filter)
  })
}

/** 事件视图 HTML（legacy app.js:380-383） */
export function renderEventsHtml(rows: Row[], filter: string): string {
  const filtered = filterEvents(rows, filter)
  return filtered.length
    ? tableFrom(filtered, [...EVENT_COLS])
    : '<div class="empty">无匹配事件</div>'
}

// ================= PermsView =================

/** 会话档位（legacy app.js:388） */
export const PERM_MODES: Array<[string, string]> = [
  ['default', '默认'],
  ['plan', '计划'],
  ['auto', '自动'],
  ['bypass', '跳过'],
]

/** 最近裁决表列（legacy app.js:396） */
export const PERM_RECENT_COLS = ['seq', 'payload', 'at'] as const

/** 最近裁决最多显示条数（legacy app.js:396） */
export const PERM_RECENT_LIMIT = 20

export interface PermsData {
  mode?: string
  policy?: Record<string, unknown>
  recent?: Row[]
}

/** 权限视图 HTML（legacy app.js:384-397） */
export function renderPermsHtml(j: PermsData): string {
  const jj = j.policy || {}
  let h = ''
  if (j.mode) {
    // approval 插件启用时才显示档位切换
    h +=
      '<h3>会话档位（approval mode）</h3><div class="filters" id="modeBar">' +
      PERM_MODES.map(
        ([k, l]) => '<button class="chip' + (j.mode === k ? ' active' : '') + '" data-mode="' + k + '">' + l + '</button>',
      ).join('') +
      '</div><div class="muted" style="margin:-6px 0 12px;font-size:11.5px">plan=只读计划 · auto=写类触发审批 · bypass=跳过审批（高危，需确认）</div>'
  }
  h +=
    '<h3>权限矩阵（config.agent.permissions，默认 open）</h3>' +
    '<div class="card"><pre class="json">' +
    esc(json2(jj)) +
    '</pre></div>' +
    '<h3>最近裁决（agent/permission/decision，durable）</h3>' +
    (j.recent && j.recent.length
      ? tableFrom(j.recent.slice(0, PERM_RECENT_LIMIT), [...PERM_RECENT_COLS])
      : '<div class="empty">暂无裁决</div>')
  return h
}

// ================= MemoryView =================

/** 长期记忆表列（legacy app.js:432） */
export const MEMORY_COLS = [
  'id',
  'kind',
  'content',
  'score_observed',
  'created_at',
  'accessed_count',
  'invalid_at',
  'source',
] as const

export interface MemoryLayer {
  key?: string
  name?: string
  subtitle?: string
  available?: boolean
  summary?: Record<string, unknown>
}

export interface MemoryData {
  layers?: MemoryLayer[]
  long_term?: { enabled?: boolean; stats?: Record<string, unknown>; rows?: Row[] }
}

/** 记忆视图 HTML（legacy app.js:399-434） */
export function renderMemoryHtml(j: MemoryData): string {
  const lt = j.long_term || {}
  let h =
    '<h3>记忆 · 分层总览</h3>' +
    '<div class="muted" style="margin:-4px 0 12px">三层记忆（系统 / 短期 / 工作）始终可见，无论长期记忆是否启用；长期记忆（plugins.memory）为可选子块。</div>'
  ;(j.layers || []).forEach((L) => {
    const s = (L.summary || {}) as Record<string, unknown>
    h +=
      '<div class="card" style="margin-bottom:12px"><div style="display:flex;justify-content:space-between;align-items:center">' +
      '<b>' +
      esc(L.name) +
      '</b><span class="tag ' +
      (L.available ? 'blue' : 'off') +
      '">' +
      (L.available ? '已装配' : '未启用') +
      '</span></div>' +
      '<div class="muted" style="font-size:11px;margin:2px 0 8px">' +
      esc(L.subtitle) +
      '</div>'
    if (!L.available) {
      h += '<div class="empty">该层未启用 —— 在「插件」页点「启用」后生效。</div>'
    } else {
      if (L.key === 'system_memory') {
        h += '<div class="mono muted">字符数 ' + (s.chars || 0) + '</div>'
        if (s.preview)
          h += '<pre class="json" style="max-height:160px;overflow:auto">' + esc(s.preview) + '</pre>'
      } else if (L.key === 'short_term') {
        h +=
          '<span class="tag blue" style="margin:0 6px 4px 0;display:inline-block">消息 ' +
          (s.messages || 0) +
          '</span>' +
          '<span class="tag blue" style="margin:0 6px 4px 0;display:inline-block">已摘要覆盖至 seq ' +
          (s.summary_covers_seq || 0) +
          '</span>' +
          '<span class="tag ' +
          (s.has_summary ? 'blue' : 'off') +
          '" style="margin:0 6px 4px 0;display:inline-block">' +
          (s.has_summary ? '有滚动摘要' : '无滚动摘要') +
          '</span>'
        if (s.compact_stats)
          h += '<div class="mono muted" style="margin-top:4px">' + esc(json2(s.compact_stats)) + '</div>'
        if (s.latest_summary_preview)
          h += '<pre class="json" style="max-height:140px;overflow:auto">' + esc(s.latest_summary_preview) + '</pre>'
      } else if (L.key === 'work_memory') {
        h += '<span class="tag blue" style="display:inline-block">活跃任务 ' + (s.count || 0) + '</span>'
        if (s.active_tasks && (s.active_tasks as unknown[]).length)
          h += '<div class="mono" style="margin-top:4px">' + esc(json2(s.active_tasks)) + '</div>'
        else h += '<div class="empty" style="margin-top:4px">当前无活跃任务</div>'
      }
    }
    h += '</div>'
  })
  h += '<h3 style="margin-top:18px">长期记忆（plugins.memory · opt-in）</h3>'
  if (!lt.enabled) {
    h += '<div class="empty">未启用 —— 在「插件」页点「启用」后，下方显示其 Zep 式记忆内容。</div>'
  } else {
    const st = (lt.stats || {}) as Record<string, unknown>
    h +=
      '<div class="card">' +
      (Object.entries((st.by_kind || {}) as Record<string, unknown>)
        .map(
          ([k, v]) =>
            '<span class="tag blue" style="margin:0 6px 4px 0;display:inline-block">' + esc(k) + ' ' + v + '</span>',
        )
        .join('') || '<span class="muted">空</span>') +
      '<span class="mono muted" style="margin-left:10px">total=' +
      (st.total || 0) +
      '</span></div>'
    h += lt.rows && lt.rows.length ? tableFrom(lt.rows, [...MEMORY_COLS]) : '<div class="empty">暂无记忆</div>'
  }
  return h
}

// ================= ToolsView =================

/** 工具视图 HTML（legacy app.js:435-442） */
export function renderToolsHtml(rows: ToolRow[]): string {
  let h = '<h3>工具目录（ctx.tasks.catalog → 供 tool_call 调用）</h3>'
  h += rows.length
    ? rows
        .map(
          (t) =>
            '<div class="card"><div style="display:flex;justify-content:space-between;gap:10px"><b>' +
            esc(t.name) +
            '</b><span class="tag blue">' +
            esc(t.owner || '-') +
            '</span></div>' +
            '<div class="muted" style="margin-top:4px">' +
            esc(t.description || '') +
            '</div>' +
            '<div class="mono" style="margin-top:4px">params=' +
            esc(json2(t.params || {})) +
            ' · required=' +
            esc((t.required || []).join(',')) +
            '</div></div>',
        )
        .join('')
    : '<div class="empty">暂无工具（接入 windows-mcp 等工具插件后出现）</div>'
  return h
}
