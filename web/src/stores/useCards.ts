/**
 * 对话卡片内核（B2.5）
 * ------------------------------------------------------------------
 * 迁移自 legacy `app.js:216-347`（hitCard / showApproval / dismissApproval /
 * showAskUser / dismissAskUser），**原样移植**。
 *
 * 与流式内核同理：卡片有自己的状态机（appr/asku 注册表 + done 态 + 超时态），
 * 用命令式 DOM 更贴合，故保留手工 DOM，收进闭包以获得可测试性。
 *
 * 不变量：
 *   C-1 同 call_id 只弹一次（既有卡片不重复创建）；
 *   C-2 了结后（done/超时）所有 button/input 必须 disabled，**且容器加 .done**
 *       —— 选项行是 div，单靠 disabled 拦不住点击；
 *   C-3 答复留痕（addUserNote）必须紧跟卡片，维持单一时间序。
 *
 * 时间：2026-09-30 11:47 (GMT+8)
 */

import { hitCardHtml, approvalBodyHtml, askBodyHtml, askSubtitle } from '../components/cards'
import type { ApprovalPayload, AskUserPayload } from '../api/types'

/** 问询应答载荷（legacy app.js:317/323/330/333 四种形态） */
export type AskAnswerPayload =
  | { answered: true; choice: string; text?: never; choices?: never }
  | { answered: true; choices: string[]; choice?: never; text?: never }
  | { answered: true; text: string; choice?: never; choices?: never }

export interface CardKernelOptions {
  /** 消息容器（.messages） */
  container: () => HTMLElement
  /** 强制贴底 */
  scrollMsg: () => void
  /** 答复留痕（紧随卡片追加用户气泡） */
  addUserNote: (text: string) => void
  /** 提交应答（POST /api/approval/answer），返回 {ok,resolved} */
  submitAnswer: (
    body: Record<string, unknown>,
  ) => Promise<{ ok?: boolean; resolved?: boolean }>
}

export interface CardKernel {
  /** 展示审批卡（同 call_id 幂等） */
  showApproval(d: ApprovalPayload): void
  /** 审批已在他处了结（应答/超时）→ 转为了结态 */
  dismissApproval(d: ApprovalPayload): void
  /** 展示澄清卡（同 call_id 幂等） */
  showAskUser(d: AskUserPayload): void
  /** 澄清已在他处了结 */
  dismissAskUser(d: AskUserPayload): void
}

export function createCardKernel(opts: CardKernelOptions): CardKernel {
  const { container, scrollMsg, addUserNote, submitAnswer } = opts

  const appr: Record<string, HTMLElement> = {}
  const asku: Record<string, HTMLElement> = {}

  /** legacy app.js:224 — 扁平卡片外壳，挂到消息流并贴底 */
  function hitCard(kind: string, badge: string, title: string, sub: string): HTMLElement {
    const el = document.createElement('div')
    el.className = 'hitcard'
    el.innerHTML = hitCardHtml(kind, badge, title, sub)
    container().appendChild(el)
    scrollMsg()
    return el
  }

  /** 禁用卡片内全部交互（C-2） */
  function disableAll(el: HTMLElement): void {
    el.querySelectorAll<HTMLButtonElement | HTMLInputElement>('button,input').forEach((x) => {
      x.disabled = true
    })
  }

  // ================= 审批卡（legacy app.js:231-271）=================

  function showApproval(d: ApprovalPayload): void {
    if (!d || !d.call_id || appr[d.call_id]) return // 同 call_id 只弹一次
    const el = hitCard(
      'approval',
      '审批',
      '请求执行工具',
      d.tool ? 'tool · ' + d.tool : '需要你的批准后才会执行',
    )
    const tag = el.querySelector<HTMLElement>('.tag')!
    el.insertAdjacentHTML('beforeend', approvalBodyHtml(d))
    appr[d.call_id] = el

    const st = el.querySelector<HTMLElement>('.hitst')!
    const done = (txt: string, cls: string) => {
      disableAll(el)
      tag.className = 'tag ' + cls
      tag.textContent = txt
      st.textContent = ''
      scrollMsg()
    }
    const answer = async (approved: boolean, always?: boolean) => {
      st.textContent = '提交中…'
      try {
        const j = await submitAnswer({ call_id: d.call_id, approved, always: !!always })
        if (j.ok && j.resolved) {
          done(
            approved ? (always ? '已批准 · 本会话同类不再问' : '已批准') : '已拒绝',
            approved ? 'ok' : 'err',
          )
          addUserNote(approved ? (always ? '总是允许（本会话同类不再询问）' : '批准执行') : '拒绝执行')
        } else done('已超时 · 默认拒绝', 'err')
      } catch (e) {
        st.textContent = '提交失败：' + (e as Error).message
      }
    }
    el.querySelector<HTMLElement>('.hityes')!.onclick = () => answer(true)
    el.querySelector<HTMLElement>('.hitalways')!.onclick = () => answer(true, true)
    el.querySelector<HTMLElement>('.hitno')!.onclick = () => answer(false)
  }

  function dismissApproval(d: ApprovalPayload): void {
    const id = d && d.call_id
    const el = id && appr[id]
    if (!el) return // 他处应答/超时：原卡片转为已了结态
    disableAll(el)
    const tag = el.querySelector<HTMLElement>('.tag')!
    const ok = d && d.approved
    tag.className = 'tag ' + (ok ? 'ok' : 'err')
    tag.textContent = ok ? '已批准' : '已了结 · 默认拒绝'
    const st = el.querySelector<HTMLElement>('.hitst')
    if (st) st.textContent = ''
  }

  // ================= 澄清卡（legacy app.js:287-347）=================

  function showAskUser(d: AskUserPayload): void {
    if (!d || !d.call_id || asku[d.call_id]) return
    const opts = Array.isArray(d.options) ? d.options : []
    const multi = !!d.multi
    const el = hitCard('ask', '问询', 'Agent 想确认你的想法', askSubtitle(opts, multi))
    const tag = el.querySelector<HTMLElement>('.tag')!

    el.insertAdjacentHTML('beforeend', askBodyHtml(d, multi))
    asku[d.call_id] = el

    const st = el.querySelector<HTMLElement>('.hitst')!
    const done = (txt: string, cls?: string) => {
      el.classList.add('done') // 选项行是 div，单靠 disabled 拦不住（C-2）
      disableAll(el)
      tag.className = 'tag ' + (cls || 'ok')
      tag.textContent = txt
      st.textContent = ''
      scrollMsg()
    }
    const answer = async (payload: Record<string, unknown>, note?: string) => {
      st.textContent = '提交中…'
      try {
        const j = await submitAnswer({ call_id: d.call_id, ...payload })
        if (j.ok && j.resolved) {
          done('已答复 · Agent 继续中', 'ok')
          addUserNote(note || (payload.choice as string) || (payload.text as string) || '')
        } else done('已超时 · Agent 将按假设继续', 'run')
      } catch (e) {
        st.textContent = '提交失败：' + (e as Error).message
      }
    }

    // 紧凑形态：点选即提交
    el.querySelectorAll<HTMLElement>('.apopt').forEach((b) => {
      b.onclick = () => {
        el.querySelectorAll<HTMLElement>('.apopt').forEach((x) =>
          x.classList.toggle('selected', x === b),
        )
        answer({ answered: true, choice: b.dataset.v }, b.dataset.l)
      }
    })

    // 卡片行形态
    const rows = Array.from(el.querySelectorAll<HTMLElement>('.hitopt'))
    if (rows.length) {
      const sub2 = el.querySelector<HTMLButtonElement>('.hitsub')
      rows.forEach((r) => {
        r.onclick = () => {
          if (!multi) {
            rows.forEach((x) => x.classList.toggle('selected', x === r))
            answer({ answered: true, choice: r.dataset.v }, r.dataset.l)
            return
          }
          r.classList.toggle('selected')
          if (sub2) sub2.disabled = !rows.some((x) => x.classList.contains('selected'))
          st.textContent = ''
        }
      })
      if (sub2) {
        sub2.onclick = () => {
          const pick = rows.filter((x) => x.classList.contains('selected'))
          if (!pick.length) {
            st.textContent = '请至少选择一项'
            return
          }
          answer(
            { answered: true, choices: pick.map((x) => x.dataset.v) },
            pick.map((x) => x.dataset.l).join('、'),
          )
        }
      }
    }

    // 自由文本
    const send = () => {
      const t = el.querySelector<HTMLInputElement>('.hitinput')
      const v = t ? t.value.trim() : ''
      if (!v) return
      answer({ answered: true, text: v }, v)
    }
    const sb = el.querySelector<HTMLElement>('.hitsend')
    if (sb) sb.onclick = send
    const tx = el.querySelector<HTMLInputElement>('.hitinput')
    if (tx) {
      tx.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault()
          send()
        }
      })
    }
  }

  function dismissAskUser(d: AskUserPayload): void {
    const id = d && d.call_id
    const el = id && asku[id]
    if (!el) return
    el.classList.add('done')
    disableAll(el)
    const tag = el.querySelector<HTMLElement>('.tag')!
    const ok = d && d.answered
    tag.className = 'tag ' + (ok ? 'ok' : 'run')
    tag.textContent = ok ? '已答复' : '已超时 · Agent 将按假设继续'
    const st = el.querySelector<HTMLElement>('.hitst')
    if (st) st.textContent = ''
  }

  return { showApproval, dismissApproval, showAskUser, dismissAskUser }
}
