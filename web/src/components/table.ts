/**
 * 共享渲染工具（B2.3 迁移自 legacy app.js:348-350 等）
 * ------------------------------------------------------------------
 * 纯函数，作为各视图 HTML 生成的公共底座。**零行为改动**。
 * 时间：2026-09-30 11:40 (GMT+8)
 */

import { esc, json2 } from './markdown'

/** legacy app.js:348 — 单元格值的空值/对象归一 */
export function fmt(x: unknown): string | number {
  return x === null || x === undefined ? '-' : typeof x === 'object' ? json2(x) : (x as string | number)
}

/** legacy app.js:349 — 通用表格（表头 + 行） */
export function tableFrom(rows: Array<Record<string, unknown>>, cols: string[]): string {
  return (
    '<table><tr>' +
    cols.map((c) => '<th>' + esc(c) + '</th>').join('') +
    '</tr>' +
    rows
      .map(
        (r) => '<tr>' + cols.map((c) => '<td>' + esc(fmt(r[c])) + '</td>').join('') + '</tr>',
      )
      .join('') +
    '</table>'
  )
}

export { esc, json2 }
