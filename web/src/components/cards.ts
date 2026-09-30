/**
 * 对话卡片 HTML 生成（B2.5 迁移自 legacy app.js:224-230, 273-286, 287-299）
 * ------------------------------------------------------------------
 * 纯函数，逐字符可对拍。三类卡片：审批（approval）/ 澄清（ask）/ 通用卡头。
 * 时间：2026-09-30 11:47 (GMT+8)
 */

import { esc, json2 } from './markdown'
import type { ApprovalPayload, AskUserPayload, AskOption } from '../api/types'

/** legacy app.js:224 — 扁平卡片外壳（头 + 徽标 + 标题 + 副标题） */
export function hitCardHtml(kind: string, badge: string, title: string, sub: string): string {
  return (
    '<div class="hithd"><div class="hitic ' +
    kind +
    '">' +
    badge +
    '</div>' +
    '<div class="hittt"><b>' +
    esc(title) +
    '</b><span>' +
    esc(sub) +
    '</span></div>' +
    '<span class="tag run">待处理</span></div>'
  )
}

/** legacy app.js:235 — 风险等级 → tag class */
export function riskClass(risk?: string): string {
  return risk === 'danger' ? 'err' : risk === 'write' ? 'run' : 'ok'
}

/** legacy app.js:236-245 — 审批卡片正文（meta + kwargs + 按钮组） */
export function approvalBodyHtml(d: ApprovalPayload): string {
  const riskCls = riskClass(d.risk)
  return (
    '<div class="hitmeta">' +
    (d.risk ? '<span class="tag ' + riskCls + '">风险 · ' + esc(d.risk) + '</span>' : '') +
    (d.mode ? '<span class="tag off">档位 · ' + esc(d.mode) + '</span>' : '') +
    '</div>' +
    (d.kwargs && Object.keys(d.kwargs).length
      ? '<pre class="hitkw">' + esc(json2(d.kwargs)) + '</pre>'
      : '') +
    '<div class="hitft"><button class="btn hityes">批准</button>' +
    '<button class="btn ghost2 hitalways">总是允许</button>' +
    '<button class="btn danger hitno">拒绝</button>' +
    '<span class="hitst"></span></div>'
  )
}

/** legacy app.js:273 — 卡片行形态：标题 + 说明（可多选） */
export function optRowHtml(o: AskOption, multi: boolean): string {
  return (
    '<div class="hitopt' +
    (multi ? ' multi' : '') +
    '" data-v="' +
    esc(o.value || o.label) +
    '" data-l="' +
    esc(o.label) +
    '">' +
    '<span class="mk"></span><span class="tt"><b>' +
    esc(o.label) +
    '</b>' +
    (o.desc ? '<em>' + esc(o.desc) + '</em>' : '') +
    '</span></div>'
  )
}

/** legacy app.js:277-286 — 选择栏：形态由选择规格决定 */
export function optListHtml(opts: AskOption[], multi: boolean): string {
  const rich = multi || opts.some((o) => o.desc) || opts.some((o) => (o as { group?: string }).group)
  if (!rich) {
    // 只有候选名 → 紧凑 chip 行（原观感）
    return (
      '<div class="hitops">' +
      opts
        .map(
          (o) =>
            '<button class="chip apopt" data-v="' +
            esc(o.value || o.label) +
            '" data-l="' +
            esc(o.label) +
            '">' +
            esc(o.label) +
            '</button>',
        )
        .join('') +
      '</div>'
    )
  }
  // 有说明/有分组/多选 → 卡片行，按 group 分区
  const order: string[] = []
  const byG: Record<string, AskOption[]> = {}
  opts.forEach((o) => {
    const g = (o as { group?: string }).group || ''
    if (!(g in byG)) {
      byG[g] = []
      order.push(g)
    }
    byG[g].push(o)
  })
  return (
    order
      .map(
        (g) =>
          '<div class="hitgrp">' +
          (g ? '<div class="gtt">' + esc(g) + '</div>' : '') +
          byG[g].map((o) => optRowHtml(o, multi)).join('') +
          '</div>',
      )
      .join('') + (multi ? '<button class="btn hitsub" disabled>提交选择</button>' : '')
  )
}

/** legacy app.js:290-292 — 澄清卡片副标题（依选项规格变化） */
export function askSubtitle(opts: unknown[], multi: boolean): string {
  return opts.length
    ? multi
      ? '可多选，勾选后一次提交；也可输入文字指示'
      : '点选方案或输入文字指示，答复后将继续执行'
    : '输入文字指示后，Agent 将继续执行'
}

/** legacy app.js:295-299 — 澄清卡片正文 */
export function askBodyHtml(d: AskUserPayload, multi: boolean): string {
  const opts = Array.isArray(d.options) ? d.options : []
  let h = '<div class="hitbd">' + esc(d.question || '（无问题文本）') + '</div>'
  if (opts.length) h += optListHtml(opts as AskOption[], multi)
  if (d.allow_free_text !== false)
    h += '<div class="hitrow"><input class="hitinput" placeholder="或输入文字指示…"><button class="btn hitsend">发送</button></div>'
  h += '<div class="hitst" style="margin-top:8px"></div>'
  return h
}
