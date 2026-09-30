/**
 * 消息渲染（B2.6 迁移自 legacy app.js:44-69）
 * ------------------------------------------------------------------
 * `stepRowHtml` 与 `messageHtml` 为纯函数（逐字符可对拍）；
 * 挂载由 ChatView 负责。
 * 时间：2026-09-30 11:50 (GMT+8)
 */

import { esc, json2, md } from './markdown'
import type { MessageEvidence } from '../api/types'

/** legacy app.js:44 — 单步载荷行 */
export function stepRowHtml(
  title: string,
  kind: string,
  status: string,
  payload: string,
): string {
  const ok = status === 'ok'
  const dot = ok ? 'ok' : status === 'run' ? 'run' : 'err'
  return (
    '<div class="step"><div class="top"><span class="dot ' +
    dot +
    '"></span><span class="name">' +
    esc(title) +
    '</span>' +
    '<span class="tag ' +
    (ok ? 'ok' : 'err') +
    '">' +
    esc(status) +
    '</span></div>' +
    '<details><summary>查看 ' +
    esc(kind) +
    ' 载荷</summary><pre>' +
    esc(payload) +
    '</pre></details></div>'
  )
}

/** legacy app.js:54-65 — 历史消息的执行证据区 */
export function evidenceHtml(evid: MessageEvidence | null): string {
  let steps = ''
  if (evid) {
    const plan = evid.plan || {}
    const exec = evid.evidence || {}
    steps = stepRowHtml(
      'plan',
      'plan.json',
      exec.error ? 'err' : 'ok',
      json2({ intent: plan.intent, tool: plan.tool || null, args: plan.args || null }),
    )
    if (exec.intent) {
      const name =
        exec.intent === 'tool_call' ? 'tool:' + (exec.tool || plan.tool || '?') : 'ctx.' + exec.intent
      const brief = exec.ok
        ? exec.result || exec.ping || exec.note || 'ok'
        : String(exec.error || 'denied')
      steps += stepRowHtml('execute: ' + name, 'evidence.json', exec.ok ? 'ok' : 'err', json2({ ok: exec.ok, brief }))
    }
    steps += stepRowHtml(
      'narrate',
      'evidence.evidence',
      Number(evid.tokens_out) > 0 ? 'ok' : 'err',
      json2({ tokens_in: evid.tokens_in, tokens_out: evid.tokens_out }),
    )
  }
  return steps
}

/** legacy app.js:49-67 — 整条消息的 innerHTML（user 气泡 / agent 回复） */
export function messageHtml(who: string, text: string, evid: MessageEvidence | null): string {
  if (who === 'user') {
    return '<div class="who">你</div><div class="bubble">' + esc(text) + '</div>'
  }
  const steps = evidenceHtml(evid)
  return '<div class="who">Agent</div>' + md(text) + (steps ? '<div class="steps">' + steps + '</div>' : '')
}
