/**
 * PluginsView 视图逻辑（B2.3 迁移自 legacy app.js:361-377, 445-468）
 * ------------------------------------------------------------------
 * `renderPluginsHtml(rows)` 为纯函数（原样移植，逐字符可对拍）；
 * 交互（启停/移除/脚手架）由 Vue 组件调用，语义与 legacy 一致。
 * 时间：2026-09-30 11:40 (GMT+8)
 */

import { esc } from '../components/markdown'
import type { PluginRow } from '../api/types'

/** 插件行的可操作判定：系统/内置业务不可管理（与引擎同语义） */
export function isManageable(p: PluginRow): boolean {
  return (p as { delta?: string }).delta !== 'system/builtin'
}

/** 插件状态徽标：由 desired/actual 推导（legacy app.js:367-372） */
export function pluginState(p: PluginRow): { text: string; cls: string } {
  const d = p as { delta?: string; desired?: boolean; actual?: boolean }
  if (d.delta === 'system/builtin') return { text: '系统/内置', cls: 'ok' }
  if (d.desired && d.actual) return { text: '已启用', cls: 'ok' }
  if (d.desired && !d.actual) return { text: '启用中（重启生效）', cls: 'run' }
  if (!d.desired && d.actual) return { text: '未启用（重启生效）', cls: 'run' }
  return { text: '未启用', cls: '' }
}

/** legacy app.js:361 — 插件表 HTML（纯函数，逐字符对拍） */
export function renderPluginsHtml(rows: PluginRow[]): string {
  let h =
    '<h3>插件（agent/plugins · 改后立即重建生效）</h3><table><tr><th>id</th><th>layer</th><th>provides</th><th>版本</th><th>状态</th><th>操作</th></tr>'
  h += rows
    .map((p) => {
      const d = p as { delta?: string; desired?: boolean; provides?: string[]; version?: string; layer?: string }
      const key = esc(p.name || (p as { id?: string }).id)
      let ops = '—'
      if (d.delta !== 'system/builtin') {
        ops =
          '<button class="chip" data-plug-act="enable" data-key="' +
          key +
          '" data-enable="' +
          (d.desired ? 'false' : 'true') +
          '">' +
          (d.desired ? '停用' : '启用') +
          '</button> ' +
          '<button class="chip" data-plug-act="remove" data-key="' +
          key +
          '" style="color:var(--err)">移除</button>'
      }
      const st = pluginState(p)
      return (
        '<tr><td>' +
        key +
        '</td><td>' +
        esc(d.layer || '') +
        '</td><td>' +
        esc((d.provides || []).join(',')) +
        '</td><td>' +
        esc(d.version || '') +
        '</td>' +
        '<td><span class="tag ' +
        st.cls +
        '">' +
        esc(st.text) +
        '</span></td><td>' +
        ops +
        '</td></tr>'
      )
    })
    .join('')
  h +=
    '</table><div class="muted" style="margin-top:6px;font-size:11.5px">新增插件：填名字与 provides → 新增插件（自动启用并重建生效）；系统/内置业务不可管理（与引擎同语义）。</div>'
  return h
}

/** 交互前置确认（legacy app.js:463-464）：返回 false 表示用户取消 */
export function confirmPluginAction(act: string, key: string, enableFlag: string | undefined): boolean {
  if (act === 'remove' && !confirm('移除 ' + key + ' ？')) return false
  if (act === 'enable' && enableFlag === 'false' && !confirm('确定停用 ' + key + ' ？\n停用后需重启服务才会彻底卸载，期间仍装配于当前进程。'))
    return false
  return true
}

/** 把表格行点击解析为动作载荷（legacy app.js:465-467） */
export function pluginActionPayload(
  act: string,
  key: string,
  enableFlag: string | undefined,
): { act: 'enable' | 'remove'; body: Record<string, unknown> } {
  return act === 'enable'
    ? { act: 'enable', body: { id: key, enable: enableFlag === 'true' } }
    : { act: 'remove', body: { id: key } }
}
