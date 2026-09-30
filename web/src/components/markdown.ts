/**
 * Markdown 渲染（B2.2 迁移自 legacy app.js:1-43）
 * ------------------------------------------------------------------
 * 原样移植，**零算法改动**。这四个纯函数是 ChatView 渲染的唯一真源：
 *   inlineLn / fenceOpen / mdInner / mdBlock / partBlocks / holdTail / md
 *
 * 设计要点（迁移必须保住，勿"优化"）：
 *   - `partBlocks`：围栏感知的块切分，末块恒为「活动尾块」；
 *   - `holdTail`：行内标记未闭合时只压住末尾一小段（滞后一拍），避免闪烁；
 *   - `mdInner`：未闭合围栏乐观渲染（只追加、不回溯）；
 *   - 增量渲染依赖 `mdInner`/`partBlocks` 的**纯函数性**（同输入必同输出）。
 *
 * 时间：2026-09-30 11:24 (GMT+8)
 */

/** HTML 转义（legacy app.js:2） */
export const esc = (s: unknown): string =>
  String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c] as string,
  )

/** 缩进 JSON（legacy app.js:3） */
export const json2 = (o: unknown): string => JSON.stringify(o, null, 2)

/** 单行行内规则（围栏内部不走这里）— legacy app.js:4 */
export function inlineLn(ln: string): string {
  let h = esc(ln)
  h = h.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>').replace(/`([^`]+)`/g, '<code>$1</code>')
  return h
    .replace(/^#{1,3} (.*)$/, '<b>$1</b>')
    .replace(/^- (.*)$/, '<span style="color:var(--muted)">•</span>&nbsp;$1')
}

/** 块内围栏是否未闭合（奇数个围栏行）— legacy app.js:9 */
export function fenceOpen(b: string): boolean {
  let n = 0
  String(b || '')
    .split('\n')
    .forEach((l) => {
      if (/^\s*```/.test(l)) n++
    })
  return n % 2 === 1
}

/** 一个块 → HTML：行间补 <br>（.text 是 normal 空白），围栏内原样 — legacy app.js:12 */
export function mdInner(t: string): string {
  const out: string[] = []
  // 注：用对象包一层，避免 TS 在 forEach 闭包后把 code 错误窄化为 never
  //（legacy 为 JS 无此问题；运行时行为与原文逐行等价，勿改回裸 let）
  const st: { code: string[] | null } = { code: null }
  String(t || '')
    .split('\n')
    .forEach((ln) => {
      if (/^\s*```/.test(ln)) {
        if (st.code === null) {
          st.code = []
        } else {
          out.push('<pre>' + esc(st.code.join('\n')) + '</pre>')
          st.code = null
        }
        return
      }
      if (st.code !== null) {
        st.code.push(ln)
        return
      }
      out.push(inlineLn(ln))
    })
  if (st.code !== null) {
    out.push('<pre>' + esc(st.code.join('\n')) + '</pre>')
  } // 未闭合围栏：乐观渲染（只追加、不回溯）
  return out.join('<br>')
}

/** legacy app.js:20 */
export function mdBlock(b: string): string {
  return '<div class="blk">' + mdInner(b) + '</div>'
}

export interface PartBlocks {
  stable: string[]
  tail: string
}

/** 围栏感知的块切分：空行分界，末块恒为「活动尾块」— legacy app.js:21 */
export function partBlocks(src: string): PartBlocks {
  const lines = String(src || '').split('\n')
  let end = lines.length
  if (end && lines[end - 1] === '') end-- // 末尾单个换行只是"这行写完了"，不算空行
  const blocks: string[] = []
  let cur: string[] = []
  let fence = false
  for (let i = 0; i < end; i++) {
    const ln = lines[i]
    if (/^\s*```/.test(ln)) {
      fence = !fence
      cur.push(ln)
      continue
    }
    if (!fence && /^[ \t]*$/.test(ln)) {
      if (cur.length) {
        blocks.push(cur.join('\n'))
        cur = []
      }
      continue
    }
    cur.push(ln)
  }
  if (cur.length) {
    blocks.push(cur.join('\n'))
    return { stable: blocks.slice(0, -1), tail: blocks[blocks.length - 1] }
  }
  return { stable: blocks, tail: '' }
}

/** 行内标记未闭合 → 只压住活动块末尾一小段（滞后一拍）— legacy app.js:33 */
export function holdTail(b: string): string {
  if (fenceOpen(b)) return b // 围栏内是字面量，无需让步
  const i = b.lastIndexOf('\n') + 1
  const line = b.slice(i)
  if (/^\s*`{1,2}$/.test(line)) return b.slice(0, i) // 半截围栏（` / ``）：压住等它长成围栏
  if (/^\s*```/.test(line)) return b // 完整围栏行：本身是结构，不参与行内判定
  let cut = -1
  ;['**', '`'].forEach((m) => {
    if ((line.split(m).length - 1) % 2 === 1) {
      const p = line.lastIndexOf(m)
      if (p > cut) cut = p
    }
  })
  return cut < 0 ? b : b.slice(0, i + cut)
}

/** 整篇渲染：历史消息与终帧对账共用同一口径 — legacy app.js:41 */
export function md(t: string): string {
  const p = partBlocks(t)
  const all = p.stable.concat(p.tail ? [p.tail] : [])
  return '<div class="text">' + all.map(mdBlock).join('') + '</div>'
}
