/**
 * SkillsView 视图逻辑（镜像 PluginsView 的 B2.3 范式）
 * ------------------------------------------------------------------
 * `renderSkillsHtml(rows)` 为纯函数（表格由它生成，交互由 Vue 组件委托）；
 * 交互（查看/启用停用/移除/重载/新增）由 Vue 组件调用，语义与插件页一致。
 * 时间：2026-10-11 17:30 (GMT+8)
 */

import { esc } from '../components/markdown'
import type { SkillRow } from '../api/types'

/** 风险档徽标 */
export function riskTag(risk: string): { text: string; cls: string } {
  if (risk === 'write') return { text: '写', cls: 'err' }
  return { text: '读', cls: 'ok' }
}

/** 来源徽标（file=文件型 / plugin=插件型） */
export function sourceTag(source: string): string {
  return source === 'plugin' ? '插件' : '文件'
}

/** 技能表 HTML（纯函数） */
export function renderSkillsHtml(rows: SkillRow[]): string {
  let h =
    '<table><tr><th>名称</th><th>摘要</th><th>档</th><th>来源</th><th>状态</th><th>操作</th></tr>'
  if (!rows.length) {
    h +=
      '</table><div class="muted" style="margin-top:8px">（暂无技能。在下方表单新增，或把 SKILL.md 放入 data/skills/）</div>'
    return h
  }
  h += rows
    .map((s) => {
      const rt = riskTag(s.risk || 'read')
      const en = s.enabled !== false
      return (
        '<tr>' +
        '<td style="font-family:var(--font-mono);font-size:12px">' +
        esc(s.name) +
        '</td>' +
        '<td style="max-width:320px">' +
        esc(s.description || '') +
        '</td>' +
        '<td><span class="tag ' +
        rt.cls +
        '">' +
        rt.text +
        '</span></td>' +
        '<td>' +
        sourceTag(s.source || 'file') +
        '</td>' +
        '<td>' +
        (en
          ? '<span class="tag ok">启用</span>'
          : '<span class="tag">停用</span>') +
        '</td>' +
        '<td>' +
        '<button class="chip" data-skill-act="view" data-name="' +
        esc(s.name) +
        '">查看</button> ' +
        '<button class="chip" data-skill-act="toggle" data-name="' +
        esc(s.name) +
        '" data-enable="' +
        (en ? 'false' : 'true') +
        '">' +
        (en ? '停用' : '启用') +
        '</button> ' +
        (s.source === 'plugin'
          ? '<span class="muted" style="font-size:11px">插件型不可移除</span>'
          : '<button class="chip" data-skill-act="remove" data-name="' +
            esc(s.name) +
            '" style="color:var(--err)">移除</button>') +
        '</td>' +
        '</tr>'
      )
    })
    .join('')
  h += '</table>'
  return h
}
