/**
 * ChatView 流式消息内核（B2.2）
 * ------------------------------------------------------------------
 * 迁移自 legacy `app.js:70-173`（addStreamMsg / 分段 / 合批），**原样移植**。
 *
 * 为什么不用 Vue 响应式重渲染：
 *   这段是全项目性能最敏感处——「定稿块永不重绘 + 活动尾块单帧重绘 + rAF 合批」
 *   把峰值 206 帧/秒压了下来。改用响应式数组重渲染会整体放弃该优化。
 *   故此处保留手工 DOM 增量，Vue 仅负责挂载/卸载生命周期。
 *
 * 不变量（迁移必须保住）：
 *   I-1 定稿块（stable）创建后**永不重绘**——只 insertBefore/appendChild；
 *   I-2 活动尾块（tail）每渲染帧**至多重绘一次**（html 相同则跳过）；
 *   I-3 一个 rAF 内**只做一次** DOM 写入（_dirty 合批）；
 *   I-4 终稿文本 === 累积 token 时走「零重排」路径（只收尾尾块）。
 *
 * 时间：2026-09-30 11:24 (GMT+8)
 */

import { mdInner, md, holdTail, partBlocks, esc, json2 } from '../components/markdown'

/** 增量渲染单元：一条 Agent 消息的完整生命周期 */
export interface StreamUnit {
  node: HTMLElement
  renderText(src: string, settle: boolean): void
  flush(): void
  addStep(p: { source?: string; data?: Record<string, unknown> } | null): void
  appendToken(text: string): void
  appendThinking(text: string): void
  finish(text: string | null, ev: Record<string, unknown> | null): void
  cancel(): void
  isEmpty(): boolean
  seal(): void
}

/** 滚动跟随：force 表示用户主动行为（新消息/卡片/答复） */
export type FollowFn = (force: boolean) => void

export interface StreamKernelOptions {
  /** 消息容器（.messages） */
  container: () => HTMLElement
  /** 滚动跟随（由宿主注入，内部含"仅贴底才跟随"逻辑） */
  follow: FollowFn
  /** 单步载荷渲染成 HTML（stepRow），由宿主注入以复用 App 级实现 */
  renderStep: (
    title: string,
    kind: string,
    status: string,
    payload: string,
  ) => string
}

export interface StreamKernel {
  /** 新建一个流式 Agent 消息单元 */
  addStreamMsg(): StreamUnit
  /** 卡片出现前封段；其后输出另起新段落 */
  beginTurnStream(): StreamUnit
  useSeg(): StreamUnit
  sealSeg(): void
}

/**
 * 创建流式内核。返回的句柄与 legacy 的模块级函数一一对应。
 *
 * 与 legacy 的差异（纪律要求，均已最小化）：
 *   - 模块级可变状态（_msgs/_raf/_dirty/_seg）收进闭包 → 可多实例、可测试；
 *   - `stepRow` 由外部注入（legacy 里是全局函数，此处避免循环依赖）；
 *   - 其余逐行等价。
 */
export function createStreamKernel(opts: StreamKernelOptions): StreamKernel {
  const { container, follow, renderStep } = opts

  // ---- 合批：一个渲染帧只刷一次 DOM（legacy:73-89）----
  let _raf = 0
  const _dirty = new Set<StreamUnit>()

  const scheduleFlush = () => {
    if (_raf) return
    _raf = requestAnimationFrame(() => {
      _raf = 0
      _dirty.forEach((u) => u.flush())
      _dirty.clear()
      follow(false)
    })
  }

  /** 文本节点追加：不动已有节点，避免整树重解析（legacy:79） */
  const appendText = (node: Node, text: string) => {
    const last = node.lastChild
    if (last && last.nodeType === 3) (last as Text).nodeValue += text
    else node.appendChild(document.createTextNode(text))
  }

  // ---- 单条流式消息（legacy:91-165）----
  function addStreamMsg(): StreamUnit {
    const d = document.createElement('div')
    d.className = 'msg agent'
    d.innerHTML =
      '<div class="who">Agent</div>' +
      '<details class="think" open><summary>💭 思考过程</summary><div class="tk" style="white-space:pre-wrap;font-size:12px;color:#8a8a8a;"></div></details>' +
      '<div class="bubble sb">…</div><div class="steps ss"></div>'
    container().appendChild(d)
    follow(true)

    const bub = d.querySelector('.sb') as HTMLElement
    const stp = d.querySelector('.ss') as HTMLElement
    const think = d.querySelector('.think') as HTMLDetailsElement | null
    const tk = d.querySelector('.tk') as HTMLElement

    let gotToken = false
    let gotThink = false
    let gotStep = false
    let raw = ''
    let live = false
    let bufThink = ''
    let wrap: HTMLElement | null = null
    const blocks: string[] = []
    let tailEl: HTMLElement | null = null
    let tailHtml = ''

    const unit: StreamUnit = {
      node: d,

      /** 增量渲染：已定稿块只追加、永不重绘；只重绘活动尾块（legacy:103） */
      renderText(src, settle) {
        src = String(src || '')
        if (!wrap) {
          // 首片即建 .text 外壳：卡片框不再等到终帧才出现
          wrap = document.createElement('div')
          wrap.className = 'text'
          bub.innerHTML = ''
          bub.appendChild(wrap)
          live = true
        }
        const p = partBlocks(src)
        if (tailEl && p.stable.length > blocks.length) {
          // 上一活动尾块已定稿 → 就地转正，不重建节点
          tailEl.innerHTML = mdInner(p.stable[blocks.length])
          blocks.push(p.stable[blocks.length])
          tailEl = null
          tailHtml = ''
        }
        for (let i = blocks.length; i < p.stable.length; i++) {
          // 追加新定稿块（定稿后永不重绘）
          const el = document.createElement('div')
          el.className = 'blk'
          el.innerHTML = mdInner(p.stable[i])
          if (tailEl) wrap.insertBefore(el, tailEl)
          else wrap.appendChild(el)
          blocks.push(p.stable[i])
        }
        if (p.tail) {
          // 活动尾块：每渲染帧至多重绘一次
          if (!tailEl) {
            tailEl = document.createElement('div')
            tailEl.className = 'blk'
            wrap.appendChild(tailEl)
            tailHtml = ''
          }
          const h = mdInner(settle ? p.tail : holdTail(p.tail))
          if (h !== tailHtml) {
            tailEl.innerHTML = h
            tailHtml = h
          }
        } else if (tailEl) {
          wrap.removeChild(tailEl)
          tailEl = null
          tailHtml = ''
        }
      },

      /** 一个渲染帧内只做一次 DOM 写入（首片到位即撤掉占位省略号）（legacy:122） */
      flush() {
        if (gotToken) unit.renderText(raw, false)
        if (bufThink) {
          appendText(tk, bufThink)
          bufThink = ''
          // 流式且展开时贴底，保证最新思考可见（固定窗口内滚动，不撑高整页）
          if (think && think.open) tk.scrollTop = tk.scrollHeight
        }
      },

      addStep(p) {
        gotStep = true
        const el = document.createElement('div')
        el.className = 'step'
        const src = p && p.source ? String(p.source) : ''
        const nm =
          (src ? src.replace(/^agent\//, '') : 'progress') +
          (p && p.data && p.data.step ? ' · step ' + p.data.step : '')
        el.innerHTML =
          '<div class="top"><span class="dot run"></span><span class="name">' +
          esc(nm) +
          '</span></div>' +
          '<details><summary>载荷</summary><pre>' +
          esc(json2((p && p.data) || {})) +
          '</pre></details>'
        stp.appendChild(el)
        follow(false)
      },

      /** B5.2 打字机：只累积源码，渲染交给 rAF 内的增量渲染（legacy:134） */
      appendToken(text) {
        gotToken = true
        raw += String(text || '')
        _dirty.add(unit)
        scheduleFlush()
      },

      /** B5.2 思考流（D4）：同样走缓冲合批（legacy:137） */
      appendThinking(text) {
        gotThink = true
        bufThink += String(text || '')
        _dirty.add(unit)
        scheduleFlush()
      },

      finish(text, ev) {
        _dirty.delete(unit)
        bufThink = ''
        // 终稿：丢弃未刷缓冲，防止覆盖后又被追加
        // 思考区：若从未收到思考帧则移除（避免空折叠块），否则收敛后折叠
        if (!gotThink && think && think.parentNode) {
          think.parentNode.removeChild(think)
        } else if (think) {
          think.open = false
        }
        if (gotStep) {
          // steps 区保留
        }
        const t = String(text || '')
        if (gotToken && t === raw) {
          unit.renderText(raw, true) // 与权威文本一致 → 只收尾活动块，零重排
        } else {
          bub.innerHTML = md(t || '（无回复）') // 有分叉 → 用权威文本整篇重建（对账，安全网）
        }
        if (ev) {
          stp.innerHTML += renderStep(
            'narrate',
            'evidence.json',
            Number(ev.tokens_out) > 0 ? 'ok' : 'err',
            json2({ tokens_in: ev.tokens_in, tokens_out: ev.tokens_out }),
          )
        }
        follow(false)
      },

      cancel() {
        // 保留已打出部分，仅追加标注
        _dirty.delete(unit)
        bufThink = ''
        if (!live && !gotToken) {
          live = true
          bub.innerHTML = ''
        } // 尚未出字：不留占位省略号
        else if (gotToken) {
          unit.renderText(raw, true)
        } // 收尾活动块，避免尾部被"滞后"截掉
        bub.innerHTML += '<span class="cancelled">（已取消）</span>'
        follow(false)
      },

      isEmpty() {
        return !gotToken && !gotThink && !gotStep
      },

      /** 封段：卡片出现前定稿，其后输出另起新段落在卡片下方（legacy:158） */
      seal() {
        _dirty.delete(unit)
        bufThink = ''
        if (think && think.parentNode) {
          if (!gotThink) think.parentNode.removeChild(think)
          else think.open = false
        }
        if (!gotToken) {
          bub.style.display = 'none' // 无正文则不显示空气泡
        } else {
          unit.renderText(raw, true) // 活动块收尾，不再整篇重建
        }
        follow(false)
      },
    }
    return unit
  }

  // ---- 流式分段：卡片帧先封段再插卡，保证 DOM 顺序即发生顺序（legacy:166）----
  let _seg: StreamUnit | null = null

  return {
    addStreamMsg,
    beginTurnStream() {
      _seg = addStreamMsg()
      return _seg
    },
    useSeg() {
      if (!_seg) _seg = addStreamMsg()
      return _seg
    },
    sealSeg() {
      if (!_seg) return
      const s = _seg
      _seg = null
      if (s.isEmpty()) {
        const n = s.node
        if (n.parentNode) n.parentNode.removeChild(n)
        return
      }
      s.seal()
    },
  }
}
