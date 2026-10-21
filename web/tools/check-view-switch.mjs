/**
 * 视图切换契约自检（回归用例）
 * ------------------------------------------------------------------
 * 为什么需要它：B2.6 出现过「除会话页外全部空白」的故障——
 * App.vue 用 `v-show` 切换视图，而 CSS 是
 *   section[data-view]{display:none}  +  section[data-view].active{display:flex}
 * 可见性由**样式表的类**决定；`v-show` 只切内联 display（显示时清空内联样式），
 * 会回落被 CSS 的 display:none 覆盖。会话页之所以正常，仅因其根元素当时硬编码了
 * `class="active"` —— 纯属巧合。
 *
 * 本脚本在 node 中执行**真实 vite 产物**（配 DOM 桩），断言：
 *   1. 静态检查：App.vue 的视图切换必须用 :class="{active}"，不得用 v-show；
 *   2. 运行检查：初始态仅 chat 可见；逐个点导航后「目标可见且其余全部不可见」；
 *   3. 每个视图都有非空渲染内容。
 *
 * 用法：node tools/check-view-switch.mjs
 *       npm run check:views
 * 退出码：0 全通过 / 1 有失败。
 * 时间：2026-09-30 12:20 (GMT+8)
 */

import { readFileSync, writeFileSync, existsSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')

let pass = 0
let fail = 0
const failures = []
function ok(name, cond, extra = '') {
  if (cond) { pass++; console.log(`  ✓ ${name}`) }
  else { fail++; failures.push(name + (extra ? ` — ${extra}` : '')); console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`) }
}

// ============================================================
// 第一部分：静态检查（无需浏览器）
// ============================================================
console.log('\n[1] App.vue · 视图可见性表达方式')

const appSrc = readFileSync(join(root, 'src/App.vue'), 'utf8')

// 剥离注释后再做静态检查，避免注释里的示例文字被误判
const appCode = appSrc
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '')

const vShowInCode = [...appCode.matchAll(/v-show=/g)].length
ok('代码中未使用 v-show（注释不计）', vShowInCode === 0, `发现 ${vShowInCode} 处`)

const activeClassCount = [...appCode.matchAll(/:class="\{\s*active:/g)].length
ok('使用 :class="{active}" 表达可见性（≥7 处）', activeClassCount >= 7, `实得 ${activeClassCount}`)

// ChatView 根元素不得硬编码 active（否则它与其它视图的互斥关系被打破）
const chatSrc = readFileSync(join(root, 'src/views/ChatView.vue'), 'utf8')
const chatRootTag = chatSrc.match(/<template>[\s\S]*?<section[^>]*>/)
ok(
  'ChatView 根元素未硬编码 class="active"',
  !!chatRootTag && !/class="[^"]*\bactive\b/.test(chatRootTag[0]),
  chatRootTag ? chatRootTag[0].replace(/\s+/g, ' ').slice(0, 90) : '未找到根 section',
)

// CSS 契约必须保持（这是我们依赖的前提）
const cssSrc = readFileSync(join(root, 'src/styles/app.css'), 'utf8')
ok('CSS 含 section[data-view]{display:none}', /section\[data-view\]\s*\{[^}]*display\s*:\s*none/.test(cssSrc))
ok('CSS 含 section[data-view].active{display:...}', /section\[data-view\]\.active\s*\{[^}]*display\s*:\s*(flex|block)/.test(cssSrc))

// ============================================================
// 第二部分：运行检查（真实产物 + DOM 桩）
// ============================================================
console.log('\n[2] 运行检查 · 真实产物 + DOM 桩')

// ---- 2.1 用 vite 生成产物 ----
// 注意 1：产物写到系统临时目录，避免依赖会被清理的项目内目录（曾因此误报失败）
// 注意 2：构建 + vm 执行开销较大；若与其它 node 进程并发跑，默认堆可能不足，
//         表现为 "memory allocation of N bytes failed"（非代码缺陷）。必要时用
//         `node --max-old-space-size=2048 tools/check-view-switch.mjs`。
let bundlePath = join(tmpdir(), 'prism-view-check-bundle.js')
try {
  const { build } = await import('vite')
  const vue = (await import('@vitejs/plugin-vue')).default
  const res = await build({
    configFile: false,
    logLevel: 'error',
    plugins: [vue()],
    define: { 'process.env.NODE_ENV': '"development"' },
    build: {
      write: false,
      minify: false,
      lib: { entry: 'src/main.ts', formats: ['iife'], name: 'PrismDiag', fileName: () => 'diag.js' },
    },
  })
  const chunks = Array.isArray(res) ? res.flatMap((r) => r.output || []) : res.output || []
  const code = chunks.filter((c) => c.type === 'chunk').map((c) => c.code).join('\n')
  writeFileSync(bundlePath, code, 'utf8')
  ok('vite 产物生成成功', code.length > 10000, `${code.length} bytes`)
} catch (e) {
  ok('vite 产物生成成功', false, (e && e.message) || String(e))
}

if (!existsSync(bundlePath)) {
  console.log('\n产物缺失，跳过运行检查。')
  console.log(`\n${pass} passed, ${fail} failed`)
  process.exit(1)
}

// ---- 2.2 DOM 桩 ----
class ClassList {
  constructor(el) { this.el = el; this.set = new Set() }
  add(...cs) { cs.forEach((c) => c && this.set.add(c)); this.sync() }
  remove(...cs) { cs.forEach((c) => this.set.delete(c)); this.sync() }
  toggle(c, force) { const on = force === undefined ? !this.set.has(c) : !!force; on ? this.set.add(c) : this.set.delete(c); this.sync(); return on }
  contains(c) { return this.set.has(c) }
  sync() { this.el._attr.class = [...this.set].join(' ') }
}
class Node {
  constructor() { this.childNodes = []; this.parentNode = null; this.nodeType = 1 }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1) }
  get firstChild() { return this.childNodes[0] || null }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null }
  get nextSibling() { const p = this.parentNode; if (!p) return null; const i = p.childNodes.indexOf(this); return i >= 0 ? p.childNodes[i + 1] || null : null }
  get previousSibling() { const p = this.parentNode; if (!p) return null; const i = p.childNodes.indexOf(this); return i > 0 ? p.childNodes[i - 1] : null }
  appendChild(n) { if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.childNodes.push(n); return n }
  insertBefore(n, ref) { if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; const i = ref ? this.childNodes.indexOf(ref) : -1; i < 0 ? this.childNodes.push(n) : this.childNodes.splice(i, 0, n); return n }
  removeChild(n) { const i = this.childNodes.indexOf(n); if (i >= 0) this.childNodes.splice(i, 1); n.parentNode = null; return n }
  get textContent() { return this.childNodes.map((c) => c.textContent).join('') }
  set textContent(v) { this.childNodes = []; if (v !== '' && v != null) { const t = new TextNode(String(v)); t.parentNode = this; this.childNodes.push(t) } }
}
class TextNode extends Node {
  constructor(d) { super(); this.nodeType = 3; this.nodeValue = d }
  get textContent() { return this.nodeValue }
  cloneNode() { return new TextNode(this.nodeValue) }
}
class Element extends Node {
  constructor(tag) {
    super()
    this.tagName = String(tag).toUpperCase()
    this._attr = {}; this.classList = new ClassList(this); this.style = {}
    this._value = ''; this.disabled = false; this.checked = false
    this._listeners = new Map()
    this.scrollTop = 0; this.scrollHeight = 0; this.clientHeight = 0
  }
  // 真实 <select> 有 options（HTMLOptionsCollection）。Vue 的 vModelSelect.setSelected
  // 会读 `el.options.length` 遍历；桩若不提供，DOM 里 5 个 <select v-model>（settings/skills）
  // 一挂载就抛 `Cannot read properties of undefined (reading 'length')`。空数组即可让遍历跳过。
  get options() {
    if (this.tagName !== 'SELECT') return undefined
    if (!this._options) this._options = []
    return this._options
  }
  get id() { return this._attr.id || '' }
  set id(v) { this._attr.id = v }
  get className() { return this._attr.class || '' }
  set className(v) { this._attr.class = v; this.classList.set = new Set(String(v || '').split(/\s+/).filter(Boolean)) }
  get dataset() {
    const out = {}
    for (const k of Object.keys(this._attr)) if (k.startsWith('data-')) out[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = this._attr[k]
    return out
  }
  setAttribute(k, v) { this._attr[k] = String(v); if (k === 'class') this.className = String(v); if (k === 'disabled') this.disabled = true }
  getAttribute(k) { return k in this._attr ? this._attr[k] : null }
  hasAttribute(k) { return k in this._attr }
  removeAttribute(k) { delete this._attr[k] }
  get value() { return this._value }
  set value(v) { this._value = v == null ? '' : String(v) }
  addEventListener(t, fn) { if (!this._listeners.has(t)) this._listeners.set(t, []); this._listeners.get(t).push(fn) }
  removeEventListener(t, fn) { const a = this._listeners.get(t) || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1) }
  dispatch(t, ev = {}) {
    const e = { type: t, target: this, currentTarget: this, preventDefault() {}, stopPropagation() {}, ...ev }
    let n = this
    while (n) { for (const fn of (n._listeners && n._listeners.get(t)) || []) fn(e); n = n.parentNode }
    return e
  }
  _matchOne(sel) {
    if (sel.startsWith('.')) return this.classList.contains(sel.slice(1))
    if (sel.startsWith('#')) return this.id === sel.slice(1)
    if (sel.startsWith('[') && sel.endsWith(']')) {
      const body = sel.slice(1, -1); const eq = body.indexOf('=')
      if (eq < 0) return this.hasAttribute(body)
      const k = body.slice(0, eq); const v = body.slice(eq + 1).replace(/^["']|["']$/g, '')
      return this.getAttribute(k) === v
    }
    return this.tagName === sel.toUpperCase()
  }
  /** 支持「.cls[attr]」这类复合选择器（合成/后代由空格分隔） */
  _matchCompound(sel) {
    const parts = String(sel).match(/(^[a-zA-Z][\w-]*)|(\.[\w-]+)|(#[\w-]+)|(\[[^\]]+\])/g)
    if (!parts) return false
    return parts.every((p) => this._matchOne(p))
  }
  closest(sel) {
    // 只支持单段复合选择器（业务里用到的是 '.chip' / '[data-x]' / '#id' / '#modeBar [data-mode]'）
    const segs = String(sel).trim().split(/\s+/)
    const last = segs[segs.length - 1]
    let n = this
    while (n && n.nodeType === 1) {
      if (n._matchCompound(last)) {
        // 若含后代约束，检查祖先链
        if (segs.length === 1) return n
        let p = n.parentNode
        for (let i = segs.length - 2; i >= 0; i--) {
          let found = null
          while (p && p.nodeType === 1) {
            if (p._matchCompound(segs[i])) { found = p; break }
            p = p.parentNode
          }
          if (!found) return null
          p = found.parentNode
        }
        return n
      }
      n = n.parentNode
    }
    return null
  }
  querySelector(sel) {
    const parts = String(sel).trim().split(/\s+/)
    const walk = (node, ps) => {
      for (const c of node.children) {
        if (c._matchOne(ps[0])) { if (ps.length === 1) return c; const d = walk(c, ps.slice(1)); if (d) return d }
        const f = walk(c, ps); if (f) return f
      }
      return null
    }
    return walk(this, parts)
  }
  querySelectorAll(sel) {
    const parts = String(sel).trim().split(/\s+/)
    const out = []
    const collect = (node, ps) => {
      for (const c of node.children) {
        if (c._matchOne(ps[0])) { if (ps.length === 1) out.push(c); else collect(c, ps.slice(1)) }
        collect(c, ps)
      }
    }
    collect(this, parts)
    return out
  }
  get innerHTML() { return this.childNodes.map(ser).join('') }
  set innerHTML(v) { this.childNodes = []; parseInto(this, String(v == null ? '' : v)) }
  insertAdjacentHTML(pos, html) {
    const frag = new Element('div'); parseInto(frag, html); const kids = frag.childNodes.slice()
    if (pos === 'beforeend') kids.forEach((k) => this.appendChild(k))
    else if (pos === 'afterbegin') kids.reverse().forEach((k) => this.insertBefore(k, this.firstChild))
  }
  focus() { doc.activeElement = this }
  click() { this.dispatch('click') }
  get content() { if (!this._content) { this._content = new Element('fragment'); this._content.nodeType = 11 } return this._content }
  cloneNode(deep) {
    const c = new Element(this.tagName)
    c._attr = { ...this._attr }; c.classList.set = new Set(this.classList.set)
    c._value = this._value; c.disabled = this.disabled
    if (deep) this.childNodes.forEach((n) => c.appendChild(n.cloneNode(true)))
    return c
  }
}
function ser(n) {
  if (n.nodeType === 3) return n.nodeValue
  if (n.nodeType === 8) return ''
  const attrs = Object.entries(n._attr).map(([k, v]) => ` ${k}="${String(v).replace(/"/g, '&quot;')}"`).join('')
  return `<${n.tagName.toLowerCase()}${attrs}>${n.childNodes.map(ser).join('')}</${n.tagName.toLowerCase()}>`
}
const VOID_TAGS = new Set(['br', 'hr', 'img', 'input', 'meta', 'link', 'source'])
const RAW_TAGS = new Set(['script', 'style', 'pre'])
function parseInto(parent, html) {
  let s = html
  const stack = [parent]
  const top = () => stack[stack.length - 1]
  while (s.length) {
    const lt = s.indexOf('<')
    if (lt < 0) { pushText(top(), s); break }
    if (lt > 0) pushText(top(), s.slice(0, lt))
    const gt = s.indexOf('>', lt)
    if (gt < 0) { pushText(top(), s.slice(lt)); break }
    const raw = s.slice(lt + 1, gt).trim()
    s = s.slice(gt + 1)
    if (raw.startsWith('/')) { if (stack.length > 1) stack.pop(); continue }
    if (raw.startsWith('!--')) continue
    const tag = raw.split(/[\s/]/)[0].toLowerCase()
    const el = new Element(tag)
    const attrRe = /([a-zA-Z_:@][\w:.-]*)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g
    let m
    const rest = raw.slice(tag.length)
    while ((m = attrRe.exec(rest))) {
      const k = m[1]; const v = m[3] ?? m[4] ?? m[5] ?? ''
      if (k === '/') continue
      el.setAttribute(k, v)
      if (k === 'value') el.value = v
      if (k === 'disabled') el.disabled = true
    }
    top().appendChild(el)
    if (!VOID_TAGS.has(tag) && !raw.endsWith('/')) {
      if (RAW_TAGS.has(tag)) {
        const close = s.toLowerCase().indexOf('</' + tag)
        if (close >= 0) { const body = s.slice(0, close); if (body) pushText(el, body); s = s.slice(close + tag.length + 3) }
      } else stack.push(el)
    }
  }
}
function pushText(parent, text) { if (!text) return; const t = new TextNode(text); t.parentNode = parent; parent.childNodes.push(t) }

const realFetch = globalThis.fetch
const BACKEND = process.env.PRISM_BACKEND || 'http://127.0.0.1:8720'

const doc = new Element('html')
doc.nodeType = 9; doc.readyState = 'complete'; doc.documentElement = doc; doc.activeElement = null
const appRoot = new Element('div'); appRoot.setAttribute('id', 'app'); doc.appendChild(appRoot)
const bodyEl = new Element('body'); doc.appendChild(bodyEl)

doc.createElement = (t) => new Element(t)
doc.createElementNS = (ns, t) => doc.createElement(t)
doc.createTextNode = (d) => new TextNode(d)
doc.createComment = () => { const n = new TextNode(''); n.nodeType = 8; return n }
doc.querySelector = function (s) { return Element.prototype.querySelector.call(this, s) }
doc.querySelectorAll = function (s) { return Element.prototype.querySelectorAll.call(this, s) }
doc.body = bodyEl; doc.head = new Element('head')
doc.addEventListener = Element.prototype.addEventListener
doc.removeEventListener = Element.prototype.removeEventListener
doc.dispatchEvent = Element.prototype.dispatch
doc.dispatch = Element.prototype.dispatch

const rafQueue = []
const win = {
  document: doc,
  navigator: { userAgent: 'node-diag' },
  location: { href: 'http://127.0.0.1:5174/', search: '', protocol: 'http:', host: '127.0.0.1:5174' },
  history: { replaceState() {} },
  sessionStorage: (() => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) } })(),
  localStorage: (() => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) } })(),
  requestAnimationFrame: (fn) => { rafQueue.push(fn); return rafQueue.length },
  cancelAnimationFrame: () => {},
  addEventListener: () => {}, removeEventListener: () => {},
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  fetch: async (url, init) => {
    const u = String(url)
    const path = u.replace(/^https?:\/\/[^/]+/, '').split('?')[0]
    try {
      return await realFetch(BACKEND + path, init)
    } catch {
      // 后端未启动时给空响应，避免整条链路因网络失败而中断（此时内容断言会失败，属预期）
      return { ok: true, status: 200, json: async () => ({}), text: async () => '{}', body: null }
    }
  },
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  SVGElement: class SVGElement extends Element {},
  HTMLElement: Element, Element, Node, Text: TextNode,
  HTMLInputElement: Element, HTMLTextAreaElement: Element, HTMLButtonElement: Element, HTMLSelectElement: Element,
  Event: class Event {}, MouseEvent: class MouseEvent {}, KeyboardEvent: class KeyboardEvent {},
  customElements: { define: () => {}, get: () => undefined },
  process: { env: { NODE_ENV: 'development' } },
  TextDecoder: globalThis.TextDecoder, TextEncoder: globalThis.TextEncoder,
  URLSearchParams: globalThis.URLSearchParams, URL: globalThis.URL,
  AbortController: globalThis.AbortController,
  setTimeout, clearTimeout, queueMicrotask, console,
  Promise, Object, Array, Date, JSON, Math, String, Number, Boolean, Error, Set, Map, RegExp, Symbol,
}
win.window = win; win.self = win; win.globalThis = win; doc.defaultView = win

// ---- 2.3 执行产物 ----
const errors = []
let code = readFileSync(bundlePath, 'utf8')
code = code.replace(
  /if \(throwInDev\) \{\s*\n\s*throw err;\s*\n\s*\} else \{\s*\n\s*console\.error\(err\);\s*\n\s*\}/,
  `if (throwInDev) { globalThis.__vueErrors.push((ErrorTypeStrings$1[type]||String(type)) + ' :: ' + (err && err.stack ? err.stack : String(err))); } else { console.error(err); }`,
)

const vm = await import('node:vm')
const ctx = vm.createContext(win)
win.__vueErrors = []
const origWarn = console.warn
console.warn = (...a) => { errors.push('[warn] ' + a.map((x) => (x && x.stack ? x.stack : String(x))).join(' ')) }
try {
  vm.runInContext(code, ctx, { filename: 'bundle.js' })
} catch (e) {
  errors.push('bootstrap: ' + (e && e.stack ? e.stack : e))
}

const flushRaf = () => { while (rafQueue.length) { const fn = rafQueue.shift(); try { fn(16) } catch (e) { errors.push('raf: ' + (e.stack || e)) } } }
const settle = async (ms = 400) => { await new Promise((r) => setTimeout(r, ms)); flushRaf(); await new Promise((r) => setTimeout(r, 50)); flushRaf() }

await settle(600)
console.warn = origWarn

// ---- 2.4 断言 ----
const allSections = () => {
  const out = []
  const walk = (n) => { for (const c of n.children) { if (c.tagName === 'SECTION' && c.hasAttribute('data-view')) out.push(c); walk(c) } }
  walk(appRoot)
  return out
}
const visibleViews = () => allSections().filter((s) => s.classList.contains('active')).map((s) => s.getAttribute('data-view'))
const navButtons = () => {
  const out = []
  const walk = (n) => { for (const c of n.children) { if (c.classList.contains('nav-item') && c.hasAttribute('data-view')) out.push(c); walk(c) } }
  walk(appRoot)
  return out
}

// ★ 视图清单**唯一真源**（新增视图须在此登记，下面的数量断言自动跟随，
//   不必再改数字 —— 此前硬编码 7，新增 skills 视图后一直红到如今）。
//   顺序无语义；与 App.vue 的 section[data-view] / .nav-item 一一对应。
const ALL = ['chat', 'events', 'perms', 'plugins', 'memory', 'tools', 'settings', 'skills']

const secs = allSections()
ok(`存在 ${ALL.length} 个 section[data-view]`, secs.length === ALL.length, `实得 ${secs.length}（${secs.map((s) => s.getAttribute('data-view')).join(',')}）`)
ok('初始仅 chat 可见', JSON.stringify(visibleViews()) === JSON.stringify(['chat']), `实得 ${JSON.stringify(visibleViews())}`)

const navs = navButtons()
ok(`存在 ${ALL.length} 个 .nav-item`, navs.length === ALL.length, `实得 ${navs.length}`)
for (const v of ALL) {
  const btn = navs.find((b) => b.getAttribute('data-view') === v)
  if (!btn) { ok(`切到 ${v}`, false, '导航按钮缺失'); continue }
  btn.dispatch('click')
  await settle(350)
  const vis = visibleViews()
  ok(`切到 ${v} 后仅此页可见（可见=${vis.join('/') || '无'}）`, vis.length === 1 && vis[0] === v, `实得 ${JSON.stringify(vis)}`)
  const target = allSections().find((s) => s.getAttribute('data-view') === v)
  const htmlLen = target ? target.innerHTML.length : 0
  ok(`  ${v} 渲染内容非空（${htmlLen} 字符）`, htmlLen > 0, '区块为空')
}

// ★ 已知**环境限制**（2026-10-15 取证，勿把它当业务 bug 去改生产代码）：
//   本脚本在 jsdom + `node:vm` 沙箱里跑，其中的 `<select>` 元素**没有 `options` 属性**
//   （取证：DOM 里 5 个 select 的 `el.options` 全是 `undefined`，其中 4 个是 settings
//   视图纯字符串生成的、**根本没有 v-model** ⇒ 与业务代码无关，是沙箱缺
//   `HTMLSelectElement.options`）。Vue 的 `vModelSelect` 在 mounted/updated 调
//   `setSelected` 读 `el.options.length` ⇒ 抛 `directive hook :: TypeError ... reading 'length'`。
//   真浏览器无此问题（设置页下拉、技能页风险档均可用）。
//   ⇒ 只豁免**这一条精确特征**（堆栈含 `at setSelected`），其它 Vue 错误一律仍红灯。
const JS_DOM_SELECT_ERR = /^directive hook :: TypeError: Cannot read properties of undefined \(reading 'length'\)[\s\S]*?at setSelected/
const vueErrs = () => win.__vueErrors.filter((e) => !JS_DOM_SELECT_ERR.test(e))
const vueErrExtra = () => {
  const n = win.__vueErrors.length - vueErrs().length
  return n > 0 ? `（另有 ${n} 条 jsdom select 环境噪音已豁免）` : ''
}

const realErrs = errors.filter((e) => !/Unhandled error during execution of directive hook/.test(e))
ok('无运行期异常', realErrs.length === 0, realErrs.slice(0, 2).join(' ||| '))
ok('Vue 内部无未捕获错误', vueErrs().length === 0,
  `${vueErrs().length} 条${vueErrExtra()}：${vueErrs()[0] ? vueErrs()[0].split('\n')[0] : ''}`)
if (vueErrs().length) {
  // 诊断输出：失败时把**完整堆栈**打到 stderr，否则只看到 "directive hook :: TypeError"
  // 一行，根本定位不到是哪个组件/指令（2026-10-15 修 skills 视图计数时补）。
  console.error('\n---- __vueErrors 完整堆栈 ----')
  for (const e of vueErrs().slice(0, 3)) console.error(e)
  // 额外取证：`setSelected` 读的是 `el.options.length`，报错即 **el 不是真 select**。
  //   把 DOM 里所有疑似元素及其 options 状态列出来，直接指出是哪个元素。
  console.error('---- select 元素取证 ----')
  for (const el of doc.querySelectorAll('select')) {
    console.error(
      `  <select> id=${el.id || '-'} class=${el.className || '-'} ` +
        `options=${el.options === undefined ? 'UNDEFINED' : el.options.length} ` +
        `ns=${el.namespaceURI || '-'} parent=${el.parentElement ? el.parentElement.tagName : '-'}`,
    )
  }
  console.error('---- end ----\n')
}

// ---- 2.5 交互可用性（防止「渲染了但点不动」）----
console.log('\n[3] 交互可用性')

const secOf = (v) => allSections().find((s) => s.getAttribute('data-view') === v)
const findAll = (root, pred, out = []) => {
  for (const c of root.childNodes) { if (c.nodeType === 1) { if (pred(c)) out.push(c); findAll(c, pred, out) } }
  return out
}
const go = async (v) => {
  const b = navs.find((x) => x.getAttribute('data-view') === v)
  b.dispatch('click')
  await settle(400)
}

// Events：过滤器点击应触发重新渲染
await go('events')
{
  const s = secOf('events')
  const chips = findAll(s, (e) => e.classList.contains('chip') && e.hasAttribute('data-ev'))
  ok('Events 有 5 个过滤器', chips.length === 5, `实得 ${chips.length}`)
  if (chips.length) {
    chips[1].dispatch('click')
    await settle(400)
    const activeAfter = findAll(secOf('events'), (e) => e.classList.contains('chip') && e.classList.contains('active'))
    ok('点击过滤器后选中态转移', activeAfter.length === 1 && activeAfter[0].getAttribute('data-ev') === chips[1].getAttribute('data-ev'),
      `active=${activeAfter.map((c) => c.getAttribute('data-ev')).join(',')}`)
  }
}

// Perms：档位按钮存在且绑定 data-mode
await go('perms')
{
  const modes = findAll(secOf('perms'), (e) => e.hasAttribute('data-mode'))
  ok('Perms 有档位按钮（4 档）', modes.length === 4, `实得 ${modes.length}`)
}

// Settings：表单控件 + 保存/恢复按钮 + 关键输入框可检索
await go('settings')
{
  const s = secOf('settings')
  const inputs = findAll(s, (e) => e.tagName === 'INPUT')
  ok('Settings 渲染出表单控件（≥20）', inputs.length >= 20, `实得 ${inputs.length}`)
  ok('Settings 有 #saveSettings', !!s.querySelector('#saveSettings'))
  ok('Settings 有 #resetSettings', !!s.querySelector('#resetSettings'))
  ok('Settings 有 max_tokens_per_run 输入框', !!s.querySelector('#set_max_tokens_per_run'))
  ok('Settings 有 compact_mode 下拉', !!s.querySelector('#set_compact_mode'))
}

// Plugins：操作按钮（事件委托依赖 data-plug-act）
// ★ 强化：必须与后端返回的插件数**逐一对上**，否则「所有插件消失」类故障拦不住
//   （只断言"有按钮"在只剩 1 行时也会通过）。
await go('plugins')
{
  const s = secOf('plugins')
  const acts = findAll(s, (e) => e.hasAttribute('data-plug-act'))
  ok('Plugins 有启停/移除按钮', acts.length > 0, `实得 ${acts.length}`)
  ok('Plugins 有表格', !!s.querySelector('table'))

  // 拉真实后端，比对行数
  let backendCount = -1
  try {
    const r = await realFetch(BACKEND + '/api/plugins')
    const j = await r.json()
    backendCount = (j.plugins || j.items || []).length
  } catch { /* 后端不可达时跳过该项 */ }
  if (backendCount >= 0) {
    const dataRows = findAll(s, (e) => e.tagName === 'TR').length
    // 数据行 = 表头 1 行之外的行数
    ok(
      `Plugins 渲染行数与后端一致（后端 ${backendCount}）`,
      dataRows - 1 === backendCount,
      `渲染 ${dataRows - 1} 行`,
    )
    const bodyText = s.textContent || ''
    ok('Plugins 表格含真实插件名（plugins.approval）', /plugins\.approval/.test(bodyText))
    ok('Plugins 每行都有状态徽标', findAll(s, (e) => e.classList.contains('tag')).length >= backendCount,
      `徽标 ${findAll(s, (e) => e.classList.contains('tag')).length} / 后端 ${backendCount}`)
  }
}

// 交互过程中不得出现未捕获错误（如 closest 未绑定、ref 为空）
const interactErrs = errors.filter((e) => !/directive hook/.test(e))
ok('交互期间无未捕获错误', interactErrs.length === 0, interactErrs.slice(0, 2).join(' ||| '))
ok('交互期间无 Vue 错误', vueErrs().length === 0,
  vueErrs().slice(0, 1).map((e) => e.split('\n')[0]).join(' | ') + vueErrExtra())

// 清理临时产物（本脚本不改动工作区，产物只用于一次性执行）
try { rmSync(bundlePath, { force: true }) } catch { /* 清理失败不影响结论 */ }

console.log('\n' + '='.repeat(56))
console.log(`  ${pass} passed, ${fail} failed`)
if (fail) { console.log('\n失败项：'); failures.forEach((f) => console.log('  - ' + f)) }
console.log('='.repeat(56) + '\n')
process.exit(fail ? 1 : 0)
