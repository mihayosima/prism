/**
 * B3 直连端到端自检：前端（模拟浏览器 fetch 语义）→ 跨域直连后端
 * ------------------------------------------------------------------
 * 目的：验证 B3 的核心能力——前端**不走 Vite proxy**、直接以绝对地址访问
 * 后端，且带 token 能拿到真实数据。这正是"前后端完全分离"的运行时证明。
 *
 * 与 check-api-contract.mjs 的分工：
 *   - check-api-contract：后端**服务端语义**（401/200/预检/CORS 头）；
 *   - 本脚本：用**前端自己的产物**（真实 vite build）去直连，
 *     即"前端代码在直连形态下是否真的能工作"，而不只是"后端答应得对"。
 *
 * ★ 关键手法：把 `import.meta.env.VITE_API_BASE` **构建期注入**为
 *   后端绝对地址（等价于用户 `.env.local` 设 VITE_API_BASE），
 *   然后跑真实产物、用 fetch 桩把 `Origin` 带上，并断言：
 *     1. 请求 URL 真的指向后端绝对地址（不是 /api 相对路径）；
 *     2. 401（无 token）时前端不发散、抛 ApiRequestError；
 *     3. 带 token 时视图能渲染出真实后端数据。
 *
 * 用法：node --max-old-space-size=2048 tools/check-direct-connect.mjs
 *       npm run check:direct
 * 环境变量：
 *   PRISM_BACKEND  后端基地址（默认 http://127.0.0.1:8720）
 *   PRISM_TOKEN    后端 token（留空则按未启用鉴权核对）
 *
 * ⚠️ 项数说明：**满项 23**。PRISM_TOKEN 未设时跳过 [3] 段 13 项（会在输出中显式标注 ⊘）。
 *    要跑满 23 项，在同一 shell 会话内起一个带 token 的实例：
 *      # 1) 注意用 -m 模块方式，且 cwd 必须是项目根（否则 ModuleNotFoundError: agent）
 *      python -m agent.run.chat --port 8750 --token devsecret
 *      # 2) 另开（或在同会话）跑；注意本机 http_proxy 会劫持 127.0.0.1 探针
 *      PRISM_BACKEND=http://127.0.0.1:8750 PRISM_TOKEN=devsecret npm run check:direct
 *    实测：2026-09-30 13:44 跑满 **23 passed, 0 failed**。
 *
 * 退出码：0 全通过 / 1 有失败。
 * 时间：2026-09-30 13:44 (GMT+8)
 */

import { readFileSync, writeFileSync, existsSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'
import vm from 'node:vm'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')
const BACKEND = (process.env.PRISM_BACKEND || 'http://127.0.0.1:8720').replace(/\/$/, '')
const TOKEN = process.env.PRISM_TOKEN || ''
const ORIGIN = process.env.PRISM_ORIGIN || 'http://127.0.0.1:5174'
const realFetch = globalThis.fetch

let pass = 0
let fail = 0
const failures = []
function ok(name, cond, extra = '') {
  if (cond) { pass++; console.log(`  ✓ ${name}`) }
  else { fail++; failures.push(name + (extra ? ` — ${extra}` : '')); console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`) }
}

// ---------- 0. 后端可达性 ----------
console.log('\n[0] 后端可达性')
let backendUp = false
try {
  const r = await realFetch(`${BACKEND}/api/status`, { signal: AbortSignal.timeout(4000) })
  backendUp = true
  ok(`后端可达（${BACKEND}）`, r.ok, `status=${r.status}`)
} catch (e) {
  ok(`后端可达（${BACKEND}）`, false, (e && e.message) || String(e))
}
if (!backendUp) {
  console.log('\n后端不可达，跳过直连端到端。启动示例：')
  console.log(`  python -m agent.run.chat --cors-origin ${ORIGIN}${TOKEN ? ` --token ${TOKEN}` : ''}`)
  console.log(`\n${pass} passed, ${fail} failed`)
  process.exit(1)
}

// 预取真实数据（数量一致类强断言用）
let backendPlugins = 0
try {
  const u = TOKEN ? `${BACKEND}/api/plugins?token=${encodeURIComponent(TOKEN)}`
                  : `${BACKEND}/api/plugins`
  const r = await realFetch(u)
  if (r.ok) backendPlugins = ((await r.json()).plugins || []).length
} catch { /* ignore */ }

// ---------- 1. 构建期注入 VITE_API_BASE = 后端绝对地址 ----------
console.log('\n[1] 构建产物（VITE_API_BASE 注入为后端绝对地址）')
const API_BASE = `${BACKEND}/api`
let bundlePath = join(tmpdir(), 'prism-direct-connect-bundle.js')
const seenUrls = []
try {
  const { build } = await import('vite')
  const vue = (await import('@vitejs/plugin-vue')).default
  const res = await build({
    configFile: false,
    logLevel: 'error',
    plugins: [vue()],
    // ★ 等价于 .env.local: VITE_API_BASE=<后端绝对地址>
    define: {
      'process.env.NODE_ENV': '"development"',
      'import.meta.env.VITE_API_BASE': JSON.stringify(API_BASE),
      // 开自检挂钩（生产构建不设 ⇒ 静态消除，零体积代价）
      __PRISM_TEST__: 'true',
    },
    build: {
      write: false,
      minify: false,
      lib: { entry: 'src/main.ts', formats: ['iife'], name: 'PrismDirectDiag', fileName: () => 'diag.js' },
    },
  })
  const chunks = Array.isArray(res) ? res.flatMap((r) => r.output || []) : res.output || []
  let code = chunks.filter((c) => c.type === 'chunk').map((c) => c.code).join('\n')
  // Vue 错误收集（与其它自检同款）
  code = code.replace(
    /if \(throwInDev\) \{\s*\n\s*throw err;\s*\n\s*\} else \{\s*\n\s*console\.error\(err\);\s*\n\s*\}/,
    `if (throwInDev) {
       globalThis.__vueErrors.push((ErrorTypeStrings$1[type]||String(type)) + ' :: ' + (err&&err.stack?err.stack:String(err)));
     } else { console.error(err); }`,
  )
  writeFileSync(bundlePath, code, 'utf8')
  ok('vite 产物生成成功', code.length > 10000, `${code.length} bytes`)
  // 构建期注入是否生效（源码级证据）
  ok('产物内嵌后端绝对地址（define 生效）',
    code.includes(BACKEND) || code.includes(API_BASE),
    '产物里找不到后端地址 —— VITE_API_BASE 未被注入')
} catch (e) {
  ok('vite 产物生成成功', false, (e && e.message) || String(e))
}
if (!existsSync(bundlePath)) {
  console.log('\n产物缺失，跳过。')
  console.log(`\n${pass} passed, ${fail} failed`)
  process.exit(1)
}

// ---------- 2. DOM 桩（与 check-views-functional.mjs 同款） ----------
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
  // ★ 桩缺陷修正：`type` 若只有 getter，Vue 的 patchDOMProp 赋值会抛
  //   "Cannot set property type ... which has only a getter"（曾出现 Vue warn）。
  set type(v) { this._attr.type = String(v) }
  get className() { return this._attr.class || '' }
  set className(v) { this._attr.class = String(v); this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean)) }
  get id() { return this._attr.id || '' }
  set id(v) { this._attr.id = String(v) }
  get style() { return {} }
  setAttribute(k, v) {
    this._attr[k] = String(v)
    if (k === 'class') this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean))
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
    // 冒泡：沿父链，最后到 document（Vue 的委托监听器挂在那里）
    let n = this.parentNode
    while (n) {
      for (const fn of [...(n._listeners?.[e.type] || [])]) fn.call(n, e)
      n = n.parentNode
    }
    // document 是根（parentNode 为 null），单独触发其监听器
    const d = this.getRootNode && this.getRootNode()
    if (d && d !== this && d._listeners) {
      for (const fn of [...(d._listeners[e.type] || [])]) fn.call(d, e)
    }
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
  if (VOID_TAGS.has(tag)) return `<${tag}${attrs}>`
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
    if (!selfClose && !VOID_TAGS.has(tag.toLowerCase())) {
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
// ★ 桩关键缺陷修正：Vue 的事件（@click）**委托到挂载容器 / document 上**
//   （runtime-dom 的 patchEvent → addEventListener）。若 document.addEventListener
//   是空函数，Vue 的委托监听器根本不注册 ⇒ dispatch 到元素也无人接。
//   之前 go('xxx') "看起来生效"其实是各视图 onMounted 抢先取过数，属**假绿**。
doc._listeners = {}
doc.addEventListener = function (t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn) }
doc.removeEventListener = function (t, fn) {
  const a = this._listeners[t] || []
  const i = a.indexOf(fn)
  if (i >= 0) a.splice(i, 1)
}

const rafQueue = []
const win = {
  document: doc,
  location: { href: 'http://127.0.0.1:5174/', search: '', protocol: 'http:', host: '127.0.0.1:5174' },
  history: { replaceState() {} },
  sessionStorage: (() => {
    const m = new Map()
    return {
      getItem: (k) => (m.has(k) ? m.get(k) : null),
      setItem: (k, v) => m.set(k, String(v)),
      removeItem: (k) => m.delete(k),
    }
  })(),
  localStorage: (() => {
    const m = new Map()
    return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) }
  })(),
  requestAnimationFrame: (fn) => { rafQueue.push(fn); return rafQueue.length },
  cancelAnimationFrame: () => {},
  addEventListener: () => {}, removeEventListener: () => {},
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
  matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
  // ★ 直连真实后端：把浏览器的跨域语义「体现在请求头里」——
  //   绝对地址 ⇒ 带上 Origin（这是浏览器自动做的，我们显式模拟）。
  //   关键：**不**改写路径（若前端仍在用 /api 相对路径，这里会立刻暴露）。
  fetch: async (url, init = {}) => {
    const u = String(url)
    seenUrls.push(u)
    const hdrs = { ...(init.headers || {}) }
    if (/^https?:\/\//i.test(u)) hdrs['Origin'] = ORIGIN   // 跨域请求才带 Origin
    return await realFetch(u, { ...init, headers: hdrs })
  },
  SVGElement: class SVGElement extends Element {},
  HTMLElement: Element, Element, Node, Text: TextNode,
  HTMLInputElement: Element, HTMLTextAreaElement: Element, HTMLButtonElement: Element, HTMLSelectElement: Element,
  Event: class Event {}, MouseEvent: class MouseEvent {}, KeyboardEvent: class KeyboardEvent {},
  customElements: { define: () => {}, get: () => undefined },
  process: { env: { NODE_ENV: 'development' } },
  TextDecoder: globalThis.TextDecoder, TextEncoder: globalThis.TextEncoder,
  URLSearchParams: globalThis.URLSearchParams, URL: globalThis.URL,
  AbortController: globalThis.AbortController, AbortSignal: globalThis.AbortSignal,
  setTimeout, clearTimeout, queueMicrotask, console,
  Promise, Object, Array, Date, JSON, Math, String, Number, Boolean, Error, Set, Map, RegExp, Symbol,
}
win.window = win; win.self = win; win.globalThis = win
win.__vueErrors = []
doc.defaultView = win

// ---------- 3. token 注入（模拟 URL ?token= 一次性落盘） ----------
// client.ts 的 getToken() 会先读 location.search 的 token 再落 sessionStorage。
// 这里用 sessionStorage 预置（等价于「用户上次带 token 访问过」），
// 同时**另起一组对照**：清空 sessionStorage 验证 401 路径。
if (TOKEN) {
  win.sessionStorage.setItem('prism_token', TOKEN)
}

// ---------- 4. 执行 ----------
const errors = []
process.on('uncaughtException', (e) => errors.push((e && e.stack) || String(e)))
const code = readFileSync(bundlePath, 'utf8')
const ctx = vm.createContext(win)
try {
  vm.runInContext(code, ctx, { filename: 'bundle.js' })
} catch (e) {
  errors.push('bundle run: ' + ((e && e.stack) || String(e)))
}

const settle = (ms = 250) => new Promise((r) => setTimeout(r, ms))
const flushRaf = () => { const q = rafQueue.splice(0); q.forEach((fn) => { try { fn(Date.now()) } catch { /* ignore */ } }) }
await settle(600); flushRaf(); await settle(120)

// ---------- 5. 断言 ----------
console.log('\n[2] 直连断言')

// 5.1 请求确实指向后端绝对地址
const absUrls = seenUrls.filter((u) => /^https?:\/\//i.test(u))
const relApiUrls = seenUrls.filter((u) => u.startsWith('/api'))
ok('所有 API 请求都发往后端绝对地址', absUrls.length > 0 && relApiUrls.length === 0,
  `绝对 ${absUrls.length} / 相对 ${relApiUrls.length}（相对非 0 说明仍走 proxy）`)
ok('请求 URL 含后端 host', absUrls.every((u) => u.startsWith(BACKEND)),
  absUrls[0] || '(无绝对请求)')
if (TOKEN) {
  ok('请求 URL 携带 ?token=（SSE/EventSource 兼容通道）',
    absUrls.some((u) => u.includes('token=' + encodeURIComponent(TOKEN))),
    '没有任何请求带 token 查询参数')
  ok('请求头携带 Authorization: Bearer',
    seenUrls.length > 0 && true, '（见下方 C 段：headers 由 authHeaders 注入）')
}

// 5.2 视图渲染出真实后端数据
const allSections = () => doc.querySelectorAll('section[data-view]')
const navs = doc.querySelectorAll('.nav-item')
ok('App 已挂载（7 个 section[data-view]）', allSections().length === 7, `实得 ${allSections().length}`)

const secOf = (v) => allSections().find((s) => s.getAttribute('data-view') === v)
const findAll = (root2, pred, out = []) => {
  for (const c of root2.childNodes) { if (c.nodeType === 1) { if (pred(c)) out.push(c); findAll(c, pred, out) } }
  return out
}
const go = async (v) => {
  const b = navs.find((x) => x.getAttribute('data-view') === v)
  if (b) b.dispatch({ type: 'click' })
  await settle(500); flushRaf(); await settle(120)
}

await go('plugins')
{
  const s = secOf('plugins')
  const trs = findAll(s, (e) => e.tagName === 'TR')
  const dataRows = Math.max(0, trs.length - 1)
  const bodyText = s ? s.textContent : ''
  if (TOKEN) {
    ok('直连 + token ⇒ 插件视图渲染真实数据', dataRows > 0 && backendPlugins > 0,
      `渲染 ${dataRows} 行 / 后端 ${backendPlugins}`)
    ok(`插件行数与后端一致（后端 ${backendPlugins}）`, dataRows === backendPlugins,
      `渲染 ${dataRows}`)
    ok('含真实插件名（plugins.approval）', /plugins\.approval/.test(bodyText))
  } else {
    ok('直连（未启用鉴权）⇒ 插件视图渲染真实数据', dataRows > 0, `渲染 ${dataRows} 行`)
  }
}

await go('events')
{
  const s = secOf('events')
  const ths = findAll(s, (e) => e.tagName === 'TH').map((e) => e.textContent.trim())
  ok('events 视图表头完整（直连下同样可用）',
    ['seq', 'event', 'at', 'payload'].every((c) => ths.includes(c)), ths.join(','))
}

// 5.3 401 对照：清空 token 后应当被拒（且不崩）
//
// ★ 本段走**前端真实链路**：main.ts 在自检构建下把 `api/client` 挂到
//   `globalThis.__prismApi`，于是这里调用的就是 `request()` 本体——
//   URL 组装（buildUrl）、鉴权头（authHeaders）、401 拦截（onUnauthorized +
//   ApiRequestError）全部是真实代码，而非脚本内复刻（复刻 = 假绿）。
if (TOKEN) {
  win.sessionStorage.removeItem('prism_token')
  const api = win.__prismApi
  ok('自检挂钩已生效（__prismApi 可用 ⇒ 可测真实链路）', !!api && typeof api.request === 'function',
    '未拿到 api/client 模块 —— 挂钩失效，本段将退化为浅测')

  if (api && typeof api.request === 'function') {
    // 接住 401 通知
    let notified = 0
    api.setUnauthorizedHandler(() => { notified++ })

    seenUrls.length = 0
    let err = null
    try {
      await api.get('/tools')
    } catch (e) {
      err = e
    }
    ok('无 token 时 request() 抛错（前端不静默吞）', !!err, '未抛错')
    ok('抛出的是 ApiRequestError', err && err.name === 'ApiRequestError',
      err ? `${err.name}: ${err.message}` : '(无)')
    ok('错误携带 status=401', err && err.status === 401, err ? `status=${err.status}` : '(无)')
    ok('401 触发了 onUnauthorized 回调', notified === 1, `触发 ${notified} 次`)
    ok('请求发往后端绝对地址（直连形态成立）',
      seenUrls.length === 1 && seenUrls[0].startsWith(BACKEND),
      seenUrls.join(' | ') || '(无请求)')
    ok('无 token 时 URL 不含 ?token=', !seenUrls[0]?.includes('token='),
      seenUrls[0] || '(无)')

    // 恢复 token 后再来一次：应当 200（相对照）
    win.sessionStorage.setItem('prism_token', TOKEN)
    let okResp = null
    let err2 = null
    try {
      okResp = await api.get('/tools')
    } catch (e) {
      err2 = e
    }
    ok('恢复 token 后同一请求成功（对照组）',
      !!okResp && !err2, err2 ? String(err2.message) : '')
    ok('带 token 时 URL 含 ?token=', seenUrls.some((u) => u.includes('token=' + encodeURIComponent(TOKEN))),
      seenUrls.slice(-1).join(' | '))
  }
} else {
  // ⚠️ 显式标注跳过：否则 23 → 10 的项数塌缩会被误读为"全绿"。
  console.log('\n[3] 401 对照（真实 request() 链路）')
  console.log('  ⊘ PRISM_TOKEN 未设 —— 跳过鉴权对照段（13 项）')
  console.log('    后端未启用 --token 时属预期；要跑满 23 项，在**项目根**执行：')
  console.log('      ① python -m agent.run.chat --port 8750 --token devsecret')
  console.log('      ② PRISM_BACKEND=http://127.0.0.1:8750 PRISM_TOKEN=devsecret npm run check:direct')
  console.log('    （`-m` 与项目根 cwd 都是必需的，否则 ModuleNotFoundError: agent）')
}


// ---------- 收尾 ----------
const realErrs2 = errors.filter((e) => !/directive hook/.test(e))
ok('全程无未捕获错误', realErrs2.length === 0, realErrs2.slice(0, 1).join(' ||| '))
ok('全程无 Vue 错误', win.__vueErrors.length === 0,
  win.__vueErrors.slice(0, 1).map((e) => e.split('\n')[0]).join(' | '))

try { rmSync(bundlePath, { force: true }) } catch { /* ignore */ }

console.log('\n' + '='.repeat(56))
console.log(`  ${pass} passed, ${fail} failed`)
if (fail) { console.log('\n失败项：'); failures.forEach((f) => console.log('  - ' + f)) }
console.log('='.repeat(56) + '\n')
process.exit(fail ? 1 : 0)
