/**
 * B2.7 逐视图功能对拍自检（回归用例）
 * ------------------------------------------------------------------
 * 目的：B2 迁移完成后，逐个视图核对「功能等价」，而不只是「能渲染」。
 *
 * 与 check-view-switch.mjs 的分工：
 *   - check-view-switch：可见性/互斥/基础交互是否**存在**；
 *   - 本脚本：功能点是否**正确**（字段完整、排序、过滤、空态、数量一致…）。
 *
 * ★ 关键手法：fetch 桩**直连真实后端**（不是假数据），因此本脚本同时
 *   覆盖真实契约 —— 前后端字段一旦漂移（如后端改名、前端改取法），
 *   这里会立刻红灯。这是"对拍"而不是"打桩自嗨"。
 *
 * 用法：node --max-old-space-size=2048 tools/check-views-functional.mjs
 *       npm run check:functional
 * 环境变量：PRISM_BACKEND（默认 http://127.0.0.1:8720）
 * 退出码：0 全通过 / 1 有失败。
 * 时间：2026-09-30 12:55 (GMT+8)
 */

import { readFileSync, writeFileSync, existsSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'
import vm from 'node:vm'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')
const BACKEND = process.env.PRISM_BACKEND || 'http://127.0.0.1:8720'
const realFetch = globalThis.fetch

let pass = 0
let fail = 0
const failures = []
function ok(name, cond, extra = '') {
  if (cond) { pass++; console.log(`  ✓ ${name}`) }
  else { fail++; failures.push(name + (extra ? ` — ${extra}` : '')); console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`) }
}

// ============================================================
// 0. 后端可达性（本脚本强依赖真实后端）
// ============================================================
console.log('\n[0] 后端可达性')
let backendUp = false
try {
  const r = await realFetch(BACKEND + '/api/plugins')
  backendUp = r.ok
} catch { /* 下面会报 */ }
ok(`后端可达（${BACKEND}）`, backendUp, '后端未启动 ⇒ 功能对拍无法进行')
if (!backendUp) {
  console.log('\n后端不可达，跳过功能对拍。请先启动后端（8720）后重试。')
  console.log(`\n${pass} passed, ${fail} failed`)
  process.exit(1)
}

// 预取各资源真实数据（用于"数量一致"类强断言）
const BACKEND_DATA = {}
for (const name of ['events', 'perms', 'memory', 'tools', 'settings', 'plugins']) {
  try {
    const r = await realFetch(`${BACKEND}/api/${name}`)
    BACKEND_DATA[name] = await r.json()
  } catch { BACKEND_DATA[name] = {} }
}

// ============================================================
// 1. 生成真实 vite 产物
// ============================================================
console.log('\n[1] 构建产物')

let bundlePath = join(tmpdir(), 'prism-functional-check-bundle.js')
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
      lib: { entry: 'src/main.ts', formats: ['iife'], name: 'PrismFuncDiag', fileName: () => 'diag.js' },
    },
  })
  const chunks = Array.isArray(res) ? res.flatMap((r) => r.output || []) : res.output || []
  let code = chunks.filter((c) => c.type === 'chunk').map((c) => c.code).join('\n')
  code = code.replace(
    /if \(throwInDev\) \{\s*\n\s*throw err;\s*\n\s*\} else \{\s*\n\s*console\.error\(err\);\s*\n\s*\}/,
    `if (throwInDev) {
       globalThis.__vueErrors.push((ErrorTypeStrings$1[type]||String(type)) + ' :: ' + (err&&err.stack?err.stack:String(err)));
     } else { console.error(err); }`,
  )
  writeFileSync(bundlePath, code, 'utf8')
  ok('vite 产物生成成功', code.length > 10000, `${code.length} bytes`)
} catch (e) {
  ok('vite 产物生成成功', false, (e && e.message) || String(e))
}
if (!existsSync(bundlePath)) {
  console.log('\n产物缺失，跳过功能对拍。')
  console.log(`\n${pass} passed, ${fail} failed`)
  process.exit(1)
}

// ============================================================
// 2. DOM 桩（与 check-view-switch.mjs 同款最小实现）
// ============================================================
class ClassList {
  constructor(el) { this.el = el; this.set = new Set() }
  add(...c) { c.forEach((x) => x && this.set.add(x)); this.sync() }
  remove(...c) { c.forEach((x) => this.set.delete(x)); this.sync() }
  toggle(c, f) { const has = this.set.has(c); const want = f === undefined ? !has : !!f; want ? this.set.add(c) : this.set.delete(c); this.sync(); return want }
  contains(c) { return this.set.has(c) }
  sync() { this.el._attr.class = [...this.set].join(' ') }
}
const VOID_TAGS = new Set(['br', 'hr', 'img', 'input', 'meta', 'link'])

class Node {
  constructor(type) { this.nodeType = type; this.childNodes = []; this.parentNode = null }
  get firstChild() { return this.childNodes[0] || null }
  get lastChild() { return this.childNodes[this.childNodes.length - 1] || null }
  get nextSibling() {
    if (!this.parentNode) return null
    const i = this.parentNode.childNodes.indexOf(this)
    return this.parentNode.childNodes[i + 1] || null
  }
  get previousSibling() {
    if (!this.parentNode) return null
    const i = this.parentNode.childNodes.indexOf(this)
    return i > 0 ? this.parentNode.childNodes[i - 1] : null
  }
  appendChild(n) { if (n.parentNode) n.parentNode.removeChild(n); n.parentNode = this; this.childNodes.push(n); return n }
  insertBefore(n, ref) {
    if (!ref) return this.appendChild(n)
    if (n.parentNode) n.parentNode.removeChild(n)
    const i = this.childNodes.indexOf(ref)
    n.parentNode = this
    this.childNodes.splice(i < 0 ? this.childNodes.length : i, 0, n)
    return n
  }
  removeChild(n) { const i = this.childNodes.indexOf(n); if (i >= 0) { this.childNodes.splice(i, 1); n.parentNode = null } return n }
  remove() { if (this.parentNode) this.parentNode.removeChild(this) }
  cloneNode(deep) {
    const c = this._cloneSelf ? this._cloneSelf() : new Node(this.nodeType)
    if (deep) this.childNodes.forEach((ch) => c.appendChild(ch.cloneNode(true)))
    return c
  }
}
class TextNode extends Node {
  constructor(t) { super(3); this.data = String(t) }
  get textContent() { return this.data }
  set textContent(v) { this.data = String(v) }
  _cloneSelf() { return new TextNode(this.data) }
}
class CommentNode extends Node {
  constructor() { super(8); this.data = '' }
  _cloneSelf() { return new CommentNode() }
}
class Fragment extends Node {
  constructor() { super(11) }
  _cloneSelf() { return new Fragment() }
}

const VOID2 = new Set(['br', 'hr', 'img', 'input', 'meta', 'link', 'source', 'col'])
class Element extends Node {
  constructor(tag) {
    super(1)
    this.tagName = String(tag).toUpperCase()
    this._attr = {}
    this._listeners = {}
    this.classList = new ClassList(this)
    this._value = ''
    this._checked = false
    this.disabled = false
  }
  // ★ 表单值：v-html 写在属性上，读的时候要能拿到（桩的 value 曾恒空 ⇒ 误报「回填失败」）
  get value() {
    if (this.tagName === 'SELECT' && this._value === '') {
      const sel = this.querySelectorAll('option').find((o) => o.hasAttribute('selected'))
      if (sel) return sel.getAttribute('value') || ''
    }
    return this._value
  }
  set value(v) { this._value = String(v) }
  get checked() { return this._checked }
  set checked(v) { this._checked = !!v }
  get type() { return this._attr.type || 'text' }
  get className() { return this._attr.class || '' }
  set className(v) { this._attr.class = String(v); this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean)) }
  get id() { return this._attr.id || '' }
  set id(v) { this._attr.id = String(v) }
  get style() { return {} }
  setAttribute(k, v) {
    this._attr[k] = String(v)
    if (k === 'class') this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean))
    // ★ 表单属性联动：v-html 生成的是 HTML 字符串，值写在属性上；
    //   若不联动，读 el.value / el.checked 会恒为空 ⇒ 误报「回填失败」（踩过）。
    if (k === 'value') this.value = String(v)
    if (k === 'checked') this.checked = true
  }
  getAttribute(k) { return this._attr[k] === undefined ? null : this._attr[k] }
  hasAttribute(k) { return this._attr[k] !== undefined }
  removeAttribute(k) { delete this._attr[k] }
  addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn) }
  removeEventListener(t, fn) { const a = this._listeners[t] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1) }
  dispatch(e) {
    e.target = e.target || this
    for (const fn of [...(this._listeners?.[e.type] || [])]) fn.call(this, e)
    // 冒泡
    let n = this.parentNode
    while (n) { for (const fn of [...(n._listeners?.[e.type] || [])]) fn.call(n, e); n = n.parentNode }
    return true
  }
  get dataset() {
    const d = {}
    for (const k of Object.keys(this._attr)) {
      if (k.startsWith('data-')) d[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = this._attr[k]
    }
    return d
  }
  get children() { return this.childNodes.filter((n) => n.nodeType === 1) }
  get textContent() {
    if (this.tagName === 'INPUT' || this.tagName === 'TEXTAREA' || this.tagName === 'SELECT') return this.value
    return this.childNodes.map((n) => n.textContent || '').join('')
  }
  set textContent(v) { this.childNodes = []; this.appendChild(new TextNode(v)) }
  get innerHTML() { return this.childNodes.map(serialize).join('') }
  set innerHTML(v) { this.childNodes = []; parseInto(this, String(v)) }
  _cloneSelf() {
    const e = new Element(this.tagName)
    Object.assign(e._attr, this._attr)
    e.classList.set = new Set(this.classList.set)
    e.value = this.value
    e.checked = this.checked
    return e
  }
  insertAdjacentHTML(pos, html) {
    const f = new Fragment()
    parseInto(f, String(html))
    if (pos === 'beforeend') f.childNodes.slice().forEach((n) => this.appendChild(n))
    else this.appendChild(f)
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null }
  querySelectorAll(sel) { const out = []; collect(this, sel.trim(), out); return out }
  closest(sel) { let n = this; while (n && n.nodeType === 1) { if (matches(n, sel)) return n; n = n.parentNode } return null }
  getRootNode() { let n = this; while (n.parentNode) n = n.parentNode; return n }
  focus() {}
  get scrollHeight() { return 1000 }
  get scrollTop() { return 0 }
  set scrollTop(_v) {}
  get clientHeight() { return 600 }
  get content() {
    if (this.tagName !== 'TEMPLATE') return undefined
    if (!this._content) this._content = new Fragment()
    return this._content
  }
}

function matches(el, compound) {
  const parts = compound.match(/([#.]?[\w-]+|\[[^\]]+\])/g) || []
  for (const p of parts) {
    if (p.startsWith('#')) { if (el.id !== p.slice(1)) return false }
    else if (p.startsWith('.')) { if (!el.classList.contains(p.slice(1))) return false }
    else if (p.startsWith('[')) {
      const m = p.match(/\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]/)
      if (!m) return false
      if (m[2] === undefined) { if (!el.hasAttribute(m[1])) return false }
      else if (el.getAttribute(m[1]) !== m[2]) return false
    } else if (el.tagName !== p.toUpperCase()) return false
  }
  return true
}
function collect(root, sel, out) {
  for (const group of sel.split(',').map((s) => s.trim()).filter(Boolean)) {
    const chain = group.split(/\s+/)
    const last = chain[chain.length - 1]
    const walk = (node) => {
      for (const c of node.childNodes) {
        if (c.nodeType !== 1) continue
        if (matches(c, last) && matchesChain(c, chain.slice(0, -1))) out.push(c)
        walk(c)
      }
    }
    walk(root)
  }
}
function matchesChain(el, ancestors) {
  if (!ancestors.length) return true
  let n = el.parentNode
  const need = [...ancestors]
  while (n && n.nodeType === 1 && need.length) {
    if (matches(n, need[need.length - 1])) need.pop()
    n = n.parentNode
  }
  return need.length === 0
}
function serialize(n) {
  if (n.nodeType === 3) return n.data
  if (n.nodeType === 8) return `<!--${n.data}-->`
  if (n.nodeType === 11) return n.childNodes.map(serialize).join('')
  const tag = n.tagName.toLowerCase()
  const attrs = Object.entries(n._attr).map(([k, v]) => ` ${k}="${v}"`).join('')
  if (VOID2.has(tag)) return `<${tag}${attrs}>`
  return `<${tag}${attrs}>${n.childNodes.map(serialize).join('')}</${tag}>`
}

const TAG_RE = /<\/?([a-zA-Z][\w-]*)((?:\s+[\w-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*)\s*(\/?)>/g
function parseAttrs(s) {
  const a = {}
  const re = /([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g
  let m
  while ((m = re.exec(s))) a[m[1]] = decodeEnt(m[2] ?? m[3] ?? m[4] ?? '')
  return a
}
function parseInto(parent, html) {
  let last = 0
  let m
  const stack = [parent]
  TAG_RE.lastIndex = 0
  while ((m = TAG_RE.exec(html))) {
    const text = html.slice(last, m.index)
    if (text) stack[stack.length - 1].appendChild(new TextNode(decodeEnt(text)))
    last = TAG_RE.lastIndex
    const [full, tag, attrs, selfClose] = m
    if (full.startsWith('</')) { if (stack.length > 1) stack.pop(); continue }
    const el = new Element(tag)
    Object.entries(parseAttrs(attrs)).forEach(([k, v]) => el.setAttribute(k, v))
    stack[stack.length - 1].appendChild(el)
    if (!selfClose && !VOID2.has(tag.toLowerCase())) {
      if (el.tagName === 'TEMPLATE') { stack.push(el.content) } else stack.push(el)
    }
  }
  const tail = html.slice(last)
  if (tail) stack[stack.length - 1].appendChild(new TextNode(decodeEnt(tail)))
}
function decodeEnt(s) {
  return s.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ')
}

// ---------- window ----------
const doc = new Element('html')
doc.nodeType = 9; doc.readyState = 'complete'; doc.documentElement = doc; doc.activeElement = null
const appRoot = new Element('div'); appRoot.setAttribute('id', 'app'); doc.appendChild(appRoot)
const bodyEl = new Element('body'); doc.appendChild(bodyEl)
doc.createElement = (t) => new Element(t)
doc.createElementNS = (_ns, t) => new Element(t)
doc.createTextNode = (d) => new TextNode(d)
doc.createComment = () => new CommentNode()
doc.querySelector = function (s) { return Element.prototype.querySelector.call(this, s) }
doc.querySelectorAll = function (s) { return Element.prototype.querySelectorAll.call(this, s) }
doc.body = bodyEl; doc.head = new Element('head')
doc.addEventListener = () => {}; doc.removeEventListener = () => {}

const rafQueue = []
const win = {
  document: doc,
  location: { href: 'http://127.0.0.1:5174/', search: '', protocol: 'http:', host: '127.0.0.1:5174' },
  history: { replaceState() {} },
  sessionStorage: (() => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) } })(),
  localStorage: (() => { const m = new Map(); return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) } })(),
  requestAnimationFrame: (fn) => { rafQueue.push(fn); return rafQueue.length },
  cancelAnimationFrame: () => {},
  addEventListener: () => {}, removeEventListener: () => {},
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  // ★ 直连真实后端：功能对拍即契约对拍
  fetch: async (url, init) => {
    const u = String(url)
    const path = u.replace(/^https?:\/\/[^/]+/, '').split('?')[0]
    return await realFetch(BACKEND + path, init)
  },
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
win.window = win; win.self = win; win.globalThis = win
win.__vueErrors = []
doc.defaultView = win

// 会话对齐（2026-10-05）：本脚本预取 /api/events 等资源时**不带会话头** ⇒ 后端落到
// `default:default`（CI 预热写入的会话）。而前端默认会**生成随机 sid**（标签级会话），
// 于是 /api/events 查的是空会话 ⇒ 事件视图渲染 0 行、与预取数据对不上（2 项假红）。
// 这里把前端会话钉到 `default`，使"预取"与"视图实查"落在同一会话——对拍才成立。
win.sessionStorage.setItem('prism_session_id', 'default')

// ---------- 执行 ----------
const errors = []
process.on('uncaughtException', (e) => errors.push((e && e.stack) || String(e)))
let code = readFileSync(bundlePath, 'utf8')
const ctx = vm.createContext(win)
try {
  vm.runInContext(code, ctx, { filename: 'bundle.js' })
} catch (e) {
  errors.push('bundle run: ' + ((e && e.stack) || String(e)))
}

const settle = (ms = 250) => new Promise((r) => setTimeout(r, ms))
const flushRaf = () => { const q = rafQueue.splice(0); q.forEach((fn) => { try { fn(Date.now()) } catch { /* ignore */ } }) }
await settle(400); flushRaf(); await settle(80)

const allSections = () => doc.querySelectorAll('section[data-view]')
const navs = doc.querySelectorAll('.nav-item')
const secOf = (v) => allSections().find((s) => s.getAttribute('data-view') === v)
const findAll = (root2, pred, out = []) => {
  for (const c of root2.childNodes) { if (c.nodeType === 1) { if (pred(c)) out.push(c); findAll(c, pred, out) } }
  return out
}
const go = async (v) => {
  const b = navs.find((x) => x.getAttribute('data-view') === v)
  if (b) b.dispatch({ type: 'click' })
  await settle(400); flushRaf(); await settle(80)
}

ok('App 已挂载（存在 7 个 section[data-view]）', allSections().length === 7, `实得 ${allSections().length}`)

// ============================================================
// 3. 逐视图功能对拍
// ============================================================
console.log('\n[2] 逐视图功能对拍（真实后端数据）')

// ---------- events ----------
console.log('\n  ── events ──')
await go('events')
{
  const s = secOf('events')
  const d = BACKEND_DATA.events
  const backendRows = (d.rows || d.events || d.items || []).length
  const ths = findAll(s, (e) => e.tagName === 'TH').map((e) => e.textContent.trim())
  ok('events 表头含 seq/event/at/payload', ['seq', 'event', 'at', 'payload'].every((c) => ths.includes(c)), ths.join(','))

  const trs = findAll(s, (e) => e.tagName === 'TR')
  const renderedRows = Math.max(0, trs.length - 1)
  ok(`events 渲染行数 ≤ 后端行数且 > 0（后端 ${backendRows}）`,
    backendRows === 0 ? renderedRows === 0 : (renderedRows > 0 && renderedRows <= backendRows),
    `渲染 ${renderedRows}`)

  // 倒序：seq 应递减（后端按 seq desc 返回）
  if (renderedRows >= 2) {
    const seqs = trs.slice(1).map((tr) => {
      const td = findAll(tr, (e) => e.tagName === 'TD')[0]
      return parseInt((td?.textContent || '').trim(), 10)
    }).filter((n) => !Number.isNaN(n))
    const desc = seqs.every((v, i) => i === 0 || seqs[i - 1] >= v)
    ok('events 按 seq 倒序', desc, seqs.slice(0, 6).join(','))
  } else {
    ok('events 按 seq 倒序', true, '行数不足 2，跳过')
  }

  // 过滤器：切到「权限」后，渲染行数应 ≤ 全量
  const chips = findAll(s, (e) => e.classList.contains('chip') && e.hasAttribute('data-ev'))
  ok('events 有 5 个过滤器', chips.length === 5, `实得 ${chips.length}`)
  const permChip = chips.find((c) => c.getAttribute('data-ev') === 'agent/permission/decision')
  if (permChip) {
    permChip.dispatch({ type: 'click' })
    await settle(250); flushRaf(); await settle(80)
    const filtered = Math.max(0, findAll(secOf('events'), (e) => e.tagName === 'TR').length - 1)
    ok('切换到「权限」过滤器后行数收敛', filtered <= renderedRows, `${renderedRows} → ${filtered}`)
    const active = findAll(secOf('events'), (e) => e.classList.contains('chip') && e.classList.contains('active'))
    ok('过滤器选中态唯一', active.length === 1 && active[0].getAttribute('data-ev') === 'agent/permission/decision')
    // 复位
    const allChip = findAll(s, (e) => e.hasAttribute('data-ev')).find((c) => c.getAttribute('data-ev') === 'all')
    if (allChip) { allChip.dispatch({ type: 'click' }); await settle(200); flushRaf() }
  }
}

// ---------- perms ----------
console.log('\n  ── perms ──')
await go('perms')
{
  const s = secOf('perms')
  const d = BACKEND_DATA.perms
  const modes = findAll(s, (e) => e.hasAttribute('data-mode'))
  ok('perms 有 4 档模式按钮', modes.length === 4, `实得 ${modes.length}`)
  const modeVals = modes.map((m) => m.getAttribute('data-mode'))
  ok('perms 档位为 default/plan/auto/bypass', ['default', 'plan', 'auto', 'bypass'].every((m) => modeVals.includes(m)), modeVals.join(','))
  // 当前档位高亮唯一
  const active = findAll(s, (e) => e.hasAttribute('data-mode') && e.classList.contains('active'))
  ok('perms 当前档位高亮唯一', active.length === 1, `实得 ${active.length}`)
  if (active.length === 1 && d.mode) {
    ok('perms 高亮档位与后端一致', active[0].getAttribute('data-mode') === d.mode, `${active[0].getAttribute('data-mode')} vs ${d.mode}`)
  }
  // 最近决策表（后端可能返回多于渲染上限；渲染侧有 PERM_RECENT_LIMIT=20 的设计上限）
  const backendRecent = (d.recent || []).length
  const trs = Math.max(0, findAll(s, (e) => e.tagName === 'TR').length - 1)
  const expectRecent = Math.min(backendRecent, 20)
  ok(`perms 最近决策行数与设计一致（后端 ${backendRecent}，上限 20 ⇒ 期望 ${expectRecent}）`,
    trs === expectRecent, `渲染 ${trs}`)
}

// ---------- plugins ----------
console.log('\n  ── plugins ──')
await go('plugins')
{
  const s = secOf('plugins')
  const d = BACKEND_DATA.plugins
  const backendRows = (d.plugins || d.items || []).length
  const trs = Math.max(0, findAll(s, (e) => e.tagName === 'TR').length - 1)
  ok(`plugins 行数与后端一致（后端 ${backendRows}）`, trs === backendRows, `渲染 ${trs}`)
  const ths = findAll(s, (e) => e.tagName === 'TH').map((e) => e.textContent.trim())
  ok('plugins 表头含 id/layer/provides/版本/状态/操作', ['id', 'layer', 'provides', '版本', '状态', '操作'].every((c) => ths.includes(c)), ths.join(','))
  const tags = findAll(s, (e) => e.classList.contains('tag'))
  ok('plugins 每行都有状态徽标', tags.length >= backendRows, `${tags.length} / ${backendRows}`)
  const acts = findAll(s, (e) => e.hasAttribute('data-plug-act'))
  ok('plugins 有启停/移除按钮', acts.length > 0, `实得 ${acts.length}`)
  ok('plugins 表格含真实插件名', /plugins\.approval/.test(s.textContent || ''))
}

// ---------- memory ----------
console.log('\n  ── memory ──')
await go('memory')
{
  const s = secOf('memory')
  const d = BACKEND_DATA.memory
  const layers = d.layers || []
  // 三个记忆层都可能为空 → 断言：结构存在（标题/表/空态三者至少其一）
  const html = s.innerHTML || ''
  ok('memory 渲染非空', html.length > 200, `${html.length} 字符`)
  const hasTable = !!s.querySelector('table')
  // MemoryView 的设计：每层渲染 div.card，未启用的层渲染 div.empty
  // （文案「该层未启用 —— 在「插件」页点「启用」后生效」），**从不用 <table>**。
  // 故此处按真实结构 + 真实文案断言，而非泛化的「暂无/空/没有」。
  const hasEmptyState =
    !!s.querySelector('.empty') || /该层未启用|未启用|暂无/.test(s.textContent || '')
  ok('memory 有表格或空态提示', hasTable || hasEmptyState, `table=${hasTable} empty=${hasEmptyState}`)
  // 层名渲染在 .card 内的 <b> 里（非 h3/h4）——按 legacy 结构断言层卡片数
  if (Array.isArray(layers) && layers.length) {
    const layerCards = findAll(s, (e) => e.classList.contains('card'))
    ok(`memory 层卡片数 ≥ 后端层数（后端 ${layers.length}）`, layerCards.length >= layers.length, `实得 ${layerCards.length}`)
    const names = layers.map((L) => L.name).filter(Boolean)
    ok('memory 渲染出后端返回的层名', names.every((n) => (s.textContent || '').includes(n)), names.join(','))
  } else {
    ok('memory 层卡片数 ≥ 后端层数', true, '后端层数为 0，跳过')
  }
}

// ---------- tools ----------
console.log('\n  ── tools ──')
await go('tools')
{
  const s = secOf('tools')
  const d = BACKEND_DATA.tools
  const backendRows = (d.tools || d.items || []).length
  // ★ tools 渲染为 .card 列表（非 table）——按 legacy 结构断言卡片数
  const cards = findAll(s, (e) => e.classList.contains('card'))
  ok(`tools 卡片数与后端一致（后端 ${backendRows}）`, cards.length === backendRows, `渲染 ${cards.length}`)
  ok('tools 渲染非空', (s.innerHTML || '').length > 200)
  // 每个卡片应含工具名与 owner 徽标
  if (backendRows > 0) {
    const first = (d.tools || [])[0]
    if (first?.name) ok(`tools 含首个工具名（${first.name}）`, (s.textContent || '').includes(first.name))
    ok('tools 卡片含 owner 徽标（tag blue）', findAll(s, (e) => e.classList.contains('tag') && e.classList.contains('blue')).length > 0)
  }
}

// ---------- settings ----------
console.log('\n  ── settings ──')
await go('settings')
{
  const s = secOf('settings')
  const d = BACKEND_DATA.settings
  const inputs = findAll(s, (e) => e.tagName === 'INPUT')
  const selects = findAll(s, (e) => e.tagName === 'SELECT')
  ok('settings 有表单控件（输入框 ≥20）', inputs.length >= 20, `输入框 ${inputs.length} / 下拉 ${selects.length}`)
  ok('settings 有 #saveSettings', !!s.querySelector('#saveSettings'))
  ok('settings 有 #resetSettings', !!s.querySelector('#resetSettings'))

  // ★ 读写回填：输入框的现值必须等于后端 values 的值
  const values = d.values || {}
  const keyChecks = [
    ['set_max_tokens_per_run', 'max_tokens_per_run'],
    ['set_compact_mode', 'compact_mode'],
  ]
  for (const [elId, key] of keyChecks) {
    const el = s.querySelector('#' + elId)
    if (!el) { ok(`settings 存在 #${elId}`, false); continue }
    const expected = values[key]
    if (expected === undefined) { ok(`settings #${elId} 回填（后端无此项，跳过）`, true); continue }
    ok(`settings #${elId} 回填 == 后端值（${expected}）`, String(el.value) === String(expected), `渲染 ${el.value}`)
  }

  // 开关类：checked 与后端一致
  const cc = s.querySelector('#set_cost_calibration')
  if (cc && values.cost_calibration !== undefined) {
    ok('settings cost_calibration 勾选态 == 后端', !!cc.checked === !!values.cost_calibration, `渲染 ${cc.checked}`)
  } else {
    ok('settings cost_calibration 勾选态', true, '后端无此项，跳过')
  }

  // ★ 放行域白名单：必须是「下拉可选」而非手填（候选项由后端 domains 给）
  const domBox = s.querySelector('#set_tool_domains')
  const domBtn = s.querySelector('#set_tool_domains_btn')
  const backendDomains = d.domains || []
  ok('settings 存在域下拉触发条 #set_tool_domains_btn', !!domBtn)
  if (!domBox) {
    ok('settings 存在 #set_tool_domains', false)
  } else if (backendDomains.length === 0) {
    ok('settings 域白名单候选项（后端无已注册域，跳过）', true)
  } else {
    // 下拉形态：面板默认收起（hidden），不占常驻版面
    ok('settings 域下拉面板默认收起（hidden）', domBox.hasAttribute('hidden'),
      '未收起 ⇒ 仍是常驻展开列表，不呈下拉形态')
    const cbs = findAll(domBox, (e) => e.classList && e.classList.contains('set_tool_domain_cb'))
    ok(`settings 域白名单渲染为复选项（后端 ${backendDomains.length} 个域）`,
      cbs.length === backendDomains.length, `实得 ${cbs.length}`)
    ok('settings 域白名单无手填文本框', findAll(domBox, (e) => e.tagName === 'INPUT' && e.type === 'text').length === 0)
    // 候选项名 == 后端域名单（防「前端自己硬编码域列表」而漂移）
    const rendered = cbs.map((el) => el.value).sort()
    const names = backendDomains.map((x) => x.name).sort()
    ok('settings 域候选项 == 后端 domains', JSON.stringify(rendered) === JSON.stringify(names), `渲染 ${rendered}`)
    // 勾选态 == 后端 values.tool_domains
    const picked = cbs.filter((el) => el.checked).map((el) => el.value).sort()
    const expected = (values.tool_domains || []).map(String).sort()
    ok('settings 域勾选态 == 后端 tool_domains',
      JSON.stringify(picked) === JSON.stringify(expected), `渲染 ${picked} / 后端 ${expected}`)
    // 触发条文案 == 已选态（DOM 顺序：≤2 个列名，更多折计数，空为「不裁剪」）
    const pickedInOrder = cbs.filter((el) => el.checked).map((el) => el.value)
    const want = pickedInOrder.length === 0
      ? '未选择（不裁剪）'
      : pickedInOrder.length <= 2
        ? pickedInOrder.join('、')
        : `已选 ${pickedInOrder.length} 个域`
    const lab = s.querySelector('#set_tool_domains_label')
    ok('settings 域下拉触发条文案 == 已选态',
      !!lab && lab.textContent === want, `渲染「${lab && lab.textContent}」/ 期望「${want}」`)
    // 下拉真的能拉下来：点触发条 ⇒ 面板 hidden 移除；再点 ⇒ 收回（点击面板外亦收回）
    if (domBtn) {
      domBtn.dispatch({ type: 'click', target: domBtn })
      await settle(60)
      ok('settings 点域下拉触发条后展开', !domBox.hasAttribute('hidden'))
      domBtn.dispatch({ type: 'click', target: domBtn })
      await settle(60)
      ok('settings 再点域下拉触发条后收起', domBox.hasAttribute('hidden'))
    }
  }
}

// ---------- nav ----------
console.log('\n  ── nav（导航与页脚）──')
{
  ok('nav 有 7 个导航项', navs.length === 7, `实得 ${navs.length}`)
  const labels = navs.map((n) => n.textContent.trim())
  ok('nav 含全部 7 个视图标签', ['会话', '事件', '权限', '插件', '记忆', '工具', '设置'].every((l) => labels.some((x) => x.includes(l))), labels.join('|'))
  // 当前激活项唯一
  const actives = navs.filter((n) => n.classList.contains('active'))
  ok('nav 激活项唯一', actives.length === 1, `实得 ${actives.length}`)
  if (actives.length === 1) {
    ok('nav 激活项与当前视图一致（settings）', actives[0].getAttribute('data-view') === 'settings', actives[0].getAttribute('data-view'))
  }
}

// ---------- 收尾 ----------
const realErrs = errors.filter((e) => !/directive hook/.test(e))
ok('全程无未捕获错误', realErrs.length === 0, realErrs.slice(0, 1).join(' ||| '))
ok('全程无 Vue 错误', win.__vueErrors.length === 0, win.__vueErrors.slice(0, 1).map((e) => e.split('\n')[0]).join(' | '))

try { rmSync(bundlePath, { force: true }) } catch { /* ignore */ }

console.log('\n' + '='.repeat(56))
console.log(`  ${pass} passed, ${fail} failed`)
if (fail) { console.log('\n失败项：'); failures.forEach((f) => console.log('  - ' + f)) }
console.log('='.repeat(56) + '\n')
process.exit(fail ? 1 : 0)
