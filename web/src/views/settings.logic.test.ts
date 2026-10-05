/**
 * settings.logic 单测（2026-10-05：前端零单测的第一批）。
 *
 * 为什么先测这个文件：它是前端最大的逻辑块（610 行），且是**纯函数**
 * （字段描述表 + HTML 生成），不需要挂载 Vue 组件即可对拍。
 *
 * 优先覆盖的是**安全语义**与**前后端契约**，而不是"常量还在不在"这类恒真断言：
 *   - 设置值来自后端、会被拼进 HTML ⇒ 转义漏了就是存储型 XSS；
 *   - 沙箱 `mode` 与 `enforcement` 是两个字段，前端**绝不能**把 `none` 渲染成"正常"，
 *     否则"声明了档位但没兑现"这件事就永远不会被用户看见。
 */

import { describe, it, expect } from 'vitest'
import { renderSettingsHtml, isFloatKey, ENFORCEMENT_BADGES, SANDBOX_MODES } from './settings.logic'
import type { SettingsResponse } from '../api/types'

const base: SettingsResponse = {
  values: { max_tokens_per_run: 1000, finalize_reserve_tokens: 200 },
  container: { max_tokens_per_run: 1000, finalize_reserve_tokens: 200 },
  effective: { max_tokens_per_run: 1000, finalize_reserve_tokens: 200 },
  scopable: [],
  session: {},
  session_id: 'default',
}

describe('renderSettingsHtml 转义（安全属性）', () => {
  it('会话标识里的 HTML 必须被转义，不得原样注入', () => {
    const evil = '<script>alert(1)</script>'
    const html = renderSettingsHtml({ ...base, session_id: evil })
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;')
  })

  it('工具域名称同样要转义（域名来自插件 id，属外部输入）', () => {
    const html = renderSettingsHtml({
      ...base,
      domains: [{ name: '<img src=x onerror=alert(1)>', count: 1, available: true }],
    })
    expect(html).not.toContain('<img src=x')
    expect(html).toContain('&lt;img')
  })
})

describe('作用域语义', () => {
  it('会话作用域下，不可会话化的键必须禁用（提交也不该被接受）', () => {
    const j: SettingsResponse = { ...base, scopable: [] }
    const sessionHtml = renderSettingsHtml(j, 'session')
    expect(sessionHtml).toContain('disabled')
  })

  it('会话作用域禁用项多于容器作用域（作用域判断若写反，这里会红）', () => {
    const j: SettingsResponse = { ...base, scopable: [] }
    // ★ 刻意**不**断言具体 id/class —— 那类断言在升级换控件命名时会碎。
    // 也不断言"容器作用域不含 disabled"：**实测**容器视图本身就有因插件未启用
    // 而禁用的控件（改这段时先跑一遍，别凭假设写断言）。
    // 真正要守的语义是**相对关系**：会话作用域会额外禁用「不可会话化」的键。
    const count = (s: string) => (s.match(/disabled/g) ?? []).length
    expect(count(renderSettingsHtml(j, 'session')))
      .toBeGreaterThan(count(renderSettingsHtml(j, 'container')))
  })
})

describe('沙箱档位渲染（前后端契约）', () => {
  it('enforcement=none 必须有独立徽标，且不能显示成“正常/受保护”', () => {
    const badge = ENFORCEMENT_BADGES['none']
    expect(badge, '缺少 none 档徽标 ⇒ 前端只能猜“正常”，正是此前的误导源').toBeTruthy()
    expect(badge.text.length).toBeGreaterThan(0)
    expect(badge.title).toBeTruthy()
    expect(/正常|已保护|受保护/.test(badge.text)).toBe(false)
  })

  it('enforcement 的每种取值都要有徽标（后端新增档位时这里会红）', () => {
    // ★ 这里枚举的是**协议取值**（后端可能上报的三种强度），不是前端自己的常量。
    // 拿前端常量遍历断言"自己包含自己"是恒真断言 —— 那种测试永远绿，等于没有。
    for (const level of ['full', 'partial', 'none']) {
      expect(ENFORCEMENT_BADGES[level], `缺少 ${level} 档徽标 ⇒ 该档位会被渲染成空白`).toBeTruthy()
    }
  })

  it('声明档位集合必须含 none（默认档，缺了设置页会漏项）', () => {
    expect(SANDBOX_MODES).toContain('none')
    expect(SANDBOX_MODES.length).toBeGreaterThan(1)
  })
})

describe('isFloatKey', () => {
  it('浮点键集合与后端口径一致', () => {
    expect(isFloatKey('memory_ratio')).toBe(true)
    expect(isFloatKey('window_high_ratio')).toBe(true)
    expect(isFloatKey('max_tokens_per_run')).toBe(false)
    expect(isFloatKey('不存在的键')).toBe(false)
  })
})
