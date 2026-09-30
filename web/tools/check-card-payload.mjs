/**
 * 卡片载荷分层契约自检（回归用例）
 * ------------------------------------------------------------------
 * 为什么需要它：B2.6 出现过「人机交互卡片不弹出」的故障——
 * 后端 progress 桥（agent/sys/progress.py:_pusher）把内核事件载荷统一包成
 *   frame.payload = { source: <内核事件名>, data: <业务对象> }
 * 而 ChatView.vue 的 handleFrame 把**外层 p** 直接传给了 cards.showApproval(d)：
 *   cards.showApproval(p)            // ❌ p.call_id === undefined
 * showApproval 首行 `if (!d.call_id) return` 于是恒真 ⇒ 卡片静默不弹、且不报错。
 *
 * 【第二次故障 · 2026-10-01】上条修复把它做成了「统一解包 d = p.data」。这对
 * approval/ask_user 是对的，但 **progress 帧的 addStep 需要外层 source**（→ 步骤名）
 * 与 data（→ 载荷）——统一解包后：
 *   ① 步骤名取不到 p.source ⇒ 退化为常量 'progress'；
 *   ② 载荷取不到 p.data    ⇒ 恒为 {}（用户可见症状：「推理过程的载荷是空的」）。
 * 为何潜伏：当时的回归脚本**只驱动了出故障的那两帧**（approval/ask_user），
 * 从未驱动 progress ⇒ 同类问题在其它分支复发时无人守卫。故本次把 progress 帧
 * 一并纳入驱动，并加静态断言钉住 progress 分支的实参形态。
 *
 * 本脚本在 node 中执行**真实 vite 产物**（配 DOM 桩），用真实帧结构
 *   { event:'approval', payload:{ source, data:{ call_id, tool, ... } } }
 * 驱动，断言：
 *   1. 审批卡真的插入了 DOM（.hitcard / .hityes 等）；
 *   2. 澄清卡真的插入了 DOM（.hitopt 等）；
 *   3. progress 帧的步骤卡标题取自 payload.source、载荷取自 payload.data（非 {}）；
 *   4. 反向验证能力：改回旧写法（卡片传外层 p / progress 传解包后的 d）⇒ 必须红灯。
 *
 * 用法：node --max-old-space-size=2048 tools/check-card-payload.mjs
 *       npm run check:cards
 * 退出码：0 全通过 / 1 有失败。
 * 时间：2026-10-01 00:20 (GMT+8)
 */

import { readFileSync, writeFileSync, existsSync, rmSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { tmpdir } from 'node:os'
import vm from 'node:vm'

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
// 第一部分：静态检查 · 卡片载荷必须从 payload.data 取
// ============================================================
console.log('\n[1] ChatView.vue · 卡片载荷取层')

const chatSrc = readFileSync(join(root, 'src/views/ChatView.vue'), 'utf8')
// 剥离注释（本文件注释里大量出现 p.data 的说明文字，必须排除以免误判）
const chatCode = chatSrc
  .replace(/<!--[\s\S]*?-->/g, '')
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^\s*\/\/.*$/gm, '')

// showApproval/dismissApproval/showAskUser/dismissAskUser 的实参不得是裸 p / payload
const badCardCall = [...chatCode.matchAll(/cards!\?\.(?:showApproval|dismissApproval|showAskUser|dismissAskUser)\(([^)]*)\)/g)]
  .map((m) => m[1].trim())
  .filter((arg) => /^(p|payload)$/.test(arg))
ok('卡片调用未直接传外层 payload', badCardCall.length === 0, badCardCall.join(', '))

// 必须存在统一的取层语句
ok('存在 payload.data 取层（d = p.data）', /\bconst\s+d\s*=\s*\(?\s*p\??\.data/.test(chatCode))

// ★ 例外：progress 帧的 addStep 要读**外层** source（作步骤名）与 data（作载荷），
//   故必须传完整 payload。若误传统一解包后的 d ⇒ 步骤名退化为 'progress'、载荷恒为 {}。
//   （这正是统一解包修复 B2.6 时引入的副作用，见脚本头部注释。）
ok('progress 帧的 addStep 不得传解包后的 d', !/\.addStep\(\s*d\b/.test(chatCode),
  (chatCode.match(/\.addStep\(([^)]*)\)/) || [])[1] || '')
ok('progress 帧的 addStep 传完整 payload（p）', /\.addStep\(\s*p\b/.test(chatCode),
  (chatCode.match(/\.addStep\(([^)]*)\)/) || [])[1] || '')

// ============================================================
// 第二部分：运行检查 · 真实产物 + DOM 桩，用真实帧结构驱动
// ============================================================
console.log('\n[2] 运行检查 · 真实产物 + DOM 桩')

let bundlePath = join(tmpdir(), 'prism-card-check-bundle.js')
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
      lib: { entry: 'src/main.ts', formats: ['iife'], name: 'PrismCardDiag', fileName: () => 'diag.js' },
    },
  })
  const chunks = Array.isArray(res) ? res.flatMap((r) => r.output || []) : res.output || []
  let code = chunks.filter((c) => c.type === 'chunk').map((c) => c.code).join('\n')

  // 抓被 Vue 吞掉的异常（logError 默认 rethrow，落到 uncaughtException 会被 process.on 吞）
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
  console.log('\n产物缺失，跳过运行检查。')
  console.log(`\n${pass} passed, ${fail} failed`)
  process.exit(1)
}

// ---------- DOM 桩（与 check-view-switch.mjs 同款最小实现）----------
class ClassList {
  constructor(el) { this.el = el; this.set = new Set() }
  add(...c) { c.forEach((x) => x && this.set.add(x)); this.sync() }
  remove(...c) { c.forEach((x) => this.set.delete(x)); this.sync() }
  toggle(c, f) { const has = this.set.has(c); const want = f === undefined ? !has : !!f; want ? this.set.add(c) : this.set.delete(c); this.sync(); return want }
  contains(c) { return this.set.has(c) }
  sync() { this.el._attr.class = [...this.set].join(' ') }
  get value() { return [...this.set].join(' ') }
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
  constructor(t) { super(8); this.data = String(t) }
  get textContent() { return this.data }
  set textContent(v) { this.data = String(v) }
  _cloneSelf() { return new CommentNode(this.data) }
}
class Fragment extends Node {
  constructor() { super(11) }
  _cloneSelf() { return new Fragment() }
}

function escapeRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') }

class Element extends Node {
  constructor(tag) {
    super(1)
    this.tagName = String(tag).toUpperCase()
    this._attr = {}
    this._listeners = {}
    this._style = {}
    this.classList = new ClassList(this)
    this.value = ''
    this.disabled = false
  }
  // Vue insertStaticContent 依赖 <template> 的 .content（惰性 DocumentFragment）
  get content() {
    if (this.tagName !== 'TEMPLATE') return undefined
    if (!this._content) {
      this._content = new Fragment()
      this.childNodes.slice().forEach((n) => this._content.appendChild(n))
    }
    return this._content
  }
  get className() { return this._attr.class || '' }
  set className(v) { this._attr.class = String(v); this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean)) }
  get id() { return this._attr.id || '' }
  set id(v) { this._attr.id = String(v) }
  get style() {
    const self = this
    return new Proxy(self._style, {
      set(t, k, v) { t[k] = v; return true },
      get(t, k) { return t[k] },
      ownKeys(t) { return Reflect.ownKeys(t) },
      getOwnPropertyDescriptor(t, k) { return { enumerable: true, configurable: true, value: t[k] } },
    })
  }
  setAttribute(k, v) {
    this._attr[k] = String(v)
    if (k === 'class') this.classList.set = new Set(String(v).split(/\s+/).filter(Boolean))
    if (k === 'id') this._attr.id = String(v)
  }
  getAttribute(k) { return this._attr[k] === undefined ? null : this._attr[k] }
  hasAttribute(k) { return this._attr[k] !== undefined }
  removeAttribute(k) { delete this._attr[k] }
  addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn) }
  removeEventListener(t, fn) { const a = this._listeners[t] || []; const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1) }
  dispatchEvent(e) {
    e.target = e.target || this
    let node = this
    while (node) {
      const a = node._listeners?.[e.type] || []
      for (const fn of [...a]) fn.call(node, e)
      node = node.parentNode
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
    if (this.tagName === 'INPUT' || this.tagName === 'TEXTAREA') return this.value
    return this.childNodes.map((n) => n.textContent || '').join('')
  }
  set textContent(v) { this.childNodes = []; this.appendChild(new TextNode(v)) }
  get innerHTML() {
    if (this._raw !== undefined) return this._raw
    return this.childNodes.map(serialize).join('')
  }
  set innerHTML(v) {
    this.childNodes = []
    this._raw = undefined
    parseInto(this, String(v))
  }
  _cloneSelf() {
    const e = new Element(this.tagName)
    Object.assign(e._attr, this._attr)
    e.classList.set = new Set(this.classList.set)
    e.value = this.value
    e.disabled = this.disabled
    if (this._content) e._content = this._content.cloneNode(true)
    return e
  }
  insertAdjacentHTML(pos, html) {
    const frag = new Fragment()
    parseInto(frag, String(html))
    if (pos === 'beforeend') frag.childNodes.slice().forEach((n) => this.appendChild(n))
    else if (pos === 'afterbegin') frag.childNodes.slice().reverse().forEach((n) => this.insertBefore(n, this.firstChild))
    else this.appendChild(frag)
  }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null }
  querySelectorAll(sel) {
    const out = []
    collect(this, sel.trim(), out)
    return out
  }
  closest(sel) {
    let n = this
    while (n && n.nodeType === 1) { if (matches(n, sel)) return n; n = n.parentNode }
    return null
  }
  getRootNode() {
    let n = this
    while (n.parentNode) n = n.parentNode
    return n
  }
  get scrollHeight() { return 1000 }
  get scrollTop() { return 0 }
  set scrollTop(_v) {}
  get clientHeight() { return 600 }
  focus() {}
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

// 极简 HTML 解析（够用即可：卡片 HTML 是规整的嵌套标签）
const TAG_RE = /<\/?([a-zA-Z][\w-]*)((?:\s+[\w-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+))?)*)\s*(\/?)>/g
function parseAttrs(s) {
  const a = {}
  const re = /([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/g
  let m
  while ((m = re.exec(s))) a[m[1]] = m[2] ?? m[3] ?? m[4] ?? ''
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
    const top = stack[stack.length - 1]
    if (full.startsWith('</')) {
      if (stack.length > 1) stack.pop()
      continue
    }
    const el = new Element(tag)
    Object.entries(parseAttrs(attrs)).forEach(([k, v]) => el.setAttribute(k, decodeEnt(v)))
    top.appendChild(el)
    // <template> 的子节点进 .content（与浏览器一致），否则 Vue 静态提升路径取不到
    if (!selfClose && !VOID_TAGS.has(tag.toLowerCase())) {
      if (el.tagName === 'TEMPLATE') { if (!el._content) el._content = new Fragment(); stack.push(el._content) }
      else stack.push(el)
    }
  }
  const tail = html.slice(last)
  if (tail) stack[stack.length - 1].appendChild(new TextNode(decodeEnt(tail)))
}
function decodeEnt(s) {
  return s.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ')
}

// ---------- 组装 window ----------
const document = {
  createElement: (t) => new Element(t),
  createElementNS: (_ns, t) => new Element(t),
  createTextNode: (t) => new TextNode(t),
  createComment: (t) => new CommentNode(t),
  createDocumentFragment: () => new Fragment(),
  querySelector: (s) => docRoot.querySelector(s),
  querySelectorAll: (s) => docRoot.querySelectorAll(s),
  getElementById: (id) => docRoot.querySelector('#' + id),
  addEventListener: () => {},
  removeEventListener: () => {},
  body: new Element('body'),
  documentElement: new Element('html'),
  readyState: 'complete',
}
const docRoot = document.body
// main.ts 挂载到 #app —— 必须预置该节点，否则 createApp().mount('#app') 静默失败
const appRoot = new Element('div')
appRoot.setAttribute('id', 'app')
document.body.appendChild(appRoot)
const errors = []
globalThis.window = globalThis
globalThis.document = document
globalThis.SVGElement = class SVGElement extends Element {}
globalThis.Element = Element
globalThis.Node = Node
globalThis.Text = TextNode
globalThis.Comment = CommentNode
globalThis.DocumentFragment = Fragment
globalThis.Document = function Document() {}
globalThis.ShadowRoot = function ShadowRoot() {}
globalThis.HTMLInputElement = Element
globalThis.HTMLTextAreaElement = Element
globalThis.HTMLButtonElement = Element
globalThis.HTMLElement = Element
globalThis.URLSearchParams = URLSearchParams
globalThis.requestAnimationFrame = (fn) => setTimeout(() => fn(Date.now()), 0)
globalThis.cancelAnimationFrame = (id) => clearTimeout(id)
globalThis.getComputedStyle = () => ({ getPropertyValue: () => '' })
// node 22 的 globalThis.navigator 只有 getter，需用 defineProperty 覆盖
try {
  Object.defineProperty(globalThis, 'navigator', { value: { userAgent: 'node' }, configurable: true })
} catch { /* 已有可用 navigator 则忽略 */ }
globalThis.location = { href: 'http://127.0.0.1:5174/', origin: 'http://127.0.0.1:5174' }
globalThis.Event = class Event { constructor(t) { this.type = t } }
globalThis.__vueErrors = []

// fetch 桩：首屏接口返回最小可用数据；SSE 由测试直接驱动 onFrame，不走网络
const fetchCalls = []
globalThis.fetch = async (url, opts = {}) => {
  const u = String(url)
  fetchCalls.push(u)
  const json = (o) => ({ ok: true, status: 200, json: async () => o, text: async () => JSON.stringify(o) })
  if (u.includes('/api/status')) return json({ provider_label: 'p', model: 'm', session_id: 's1', version: 'v', budget: null })
  if (u.includes('/api/session')) return json({ history: [] })
  if (u.includes('/api/approval/pending')) return json({ items: [], enabled: true, mode: 'default' })
  return json({})
}

// ---------- 执行产物 ----------
const sandbox = Object.create(globalThis)
// 让沙箱以宿主 globalThis 为原型：AbortController / URLSearchParams / TextDecoder 等
// node 内置全局自动可见，不必逐个列举（真实浏览器里它们本来就存在）。
// 但仍需显式赋值 window/globalThis/self 指向自身，并把 DOM 桩放进来。
sandbox.globalThis = sandbox
sandbox.window = sandbox
sandbox.self = sandbox
sandbox.document = document
sandbox.SVGElement = globalThis.SVGElement
sandbox.Element = Element
sandbox.Node = Node
sandbox.Text = TextNode
sandbox.Comment = CommentNode
sandbox.DocumentFragment = Fragment
sandbox.Document = globalThis.Document
sandbox.ShadowRoot = globalThis.ShadowRoot
sandbox.HTMLElement = Element
sandbox.HTMLInputElement = Element
sandbox.HTMLTextAreaElement = Element
sandbox.HTMLButtonElement = Element
sandbox.fetch = globalThis.fetch
// ★ 动态代理：后续会替换 globalThis.fetch（拦截流式接口），
//   若此处绑定的是旧函数，沙箱里就永远拿不到新桩（曾因此误判为 HTTP 200 失败）。
Object.defineProperty(sandbox, 'fetch', {
  configurable: true,
  get() { return globalThis.fetch },
  set(v) { globalThis.fetch = v },
})
sandbox.__vueErrors = []
const ctx = vm.createContext(sandbox)
process.on('uncaughtException', (e) => errors.push((e && e.stack) || String(e)))

try {
  vm.runInContext(readFileSync(bundlePath, 'utf8'), ctx, { filename: 'bundle.js' })
} catch (e) {
  errors.push('bundle run: ' + ((e && e.stack) || String(e)))
}

// 等待 onMounted 里的 await（loadStatus/loadSession/restorePending）
await new Promise((r) => setTimeout(r, 120))

const msgBox = () => document.body.querySelector('.messages')
ok('ChatView 已挂载（.messages 存在）', !!msgBox(), errors[0] || '')

// ---------- 驱动真实帧结构 ----------
// ★ 关键：payload 是 { source, data } 外层包装（agent/sys/progress.py:_pusher）
function frame(event, biz) {
  return { event, payload: { source: 'agent/' + event, data: biz } }
}

// ★ progress 帧（REACT_STEP_* / TASKS_POST_EXECUTE 等走它）：
//   source 是**真实内核事件名**（不是帧名），data 是业务对象。
//   注意：progress 帧的 source 不可用 frame() 的 'agent/'+event 拼法——那样会造出
//   'agent/progress' 这种不存在的内核事件名，掩盖「步骤名取自 source」的断言强度。
const progressFrame = {
  event: 'progress',
  payload: {
    source: 'agent/react/step_end',
    data: { step: 2, tool: 'fs.write', ok: true, ms: 12 },
  },
}

const approvalFrame = frame('approval', {
  call_id: 'call-abc-1',
  tool: 'fs.write',
  risk: 'medium',
  kwargs: { path: '/tmp/x.txt' },
})

const askFrame = frame('ask_user', {
  call_id: 'call-abc-2',
  kind: 'ask_user',
  options: [{ v: 'a', label: '选项 A' }, { v: 'b', label: '选项 B' }],
  multi: false,
  question: '要选哪个？',
})

// 由于 onFrame 由 streamChat 内部回调触发，这里用「直接调用组件暴露的能力」不可行，
// 改为：通过 fetch 桩拦截 /chat/stream 的流式响应 —— 用 ReadableStream 喂真实帧。
//
// ★ 线格式（与 agent/run/chat.py 的 SSE 输出一致）：**每帧一行 `data: <JSON>`**，
//   JSON 自身带 `event` 与 `payload` 字段；**不是**标准的 `event: xxx\ndata: {...}` 双行格式。
//   （sse.ts 只认 `data:` 行并 JSON.parse，再从 JSON 里取 frame.event。）
const enc = new TextEncoder()
function sseBody(frames) {
  const text = frames.map((f) => `data: ${JSON.stringify(f)}\n\n`).join('')
  return {
    ok: true, status: 200,
    body: {
      getReader() {
        let done = false
        return {
          async read() {
            if (done) return { done: true, value: undefined }
            done = true
            return { done: false, value: enc.encode(text) }
          },
          async cancel() {},
          releaseLock() {},
        }
      },
    },
    text: async () => text,
  }
}

globalThis.fetch = async (url, opts = {}) => {
  const u = String(url)
  fetchCalls.push(u)
  const json = (o) => ({ ok: true, status: 200, json: async () => o, text: async () => JSON.stringify(o) })
  // ★ 线格式：每帧一行 `data: <JSON>`，JSON 自带 event/payload；不是标准双行 SSE
  if (u.includes('/chat/stream')) return sseBody([progressFrame, approvalFrame, askFrame])
  if (u.includes('/api/status')) return json({ provider_label: 'p', model: 'm', session_id: 's1', version: 'v', budget: null })
  if (u.includes('/api/session')) return json({ history: [] })
  if (u.includes('/api/approval/pending')) return json({ items: [], enabled: true, mode: 'default' })
  return json({})
}

// 触发发送：填输入框 + 点 #send
const txtEl = document.body.querySelector('#txt')
const sendEl = document.body.querySelector('#send')
ok('#txt 与 #send 存在', !!txtEl && !!sendEl)
if (txtEl && sendEl) {
  // textarea 走 v-model，需先派发 input 事件把值同步进 ref
  txtEl.value = '请写个文件'
  txtEl.dispatchEvent({ type: 'input', target: txtEl })
  await new Promise((r) => setTimeout(r, 30))
  sendEl.dispatchEvent({ type: 'click', target: sendEl })
  await new Promise((r) => setTimeout(r, 400))
}

// ---------- progress 帧：步骤卡的「来源名 + 载荷」 ----------
// 这是 B2.6 统一解包引入的副作用：addStep 被传了解包后的 d ⇒
//   ① 标题取不到 p.source，退化为常量 'progress'；
//   ② 载荷取不到 p.data，恒为 {}（用户可见的「载荷是空的」）。
const stepEls = document.body.querySelectorAll('.step')
ok('progress 帧渲染出步骤卡（.step）', stepEls.length >= 1, `实得 ${stepEls.length}`)
const step0 = stepEls[0] || null
const nmEl = step0 && step0.querySelector('.name')
ok('步骤卡标题取自 payload.source（去 agent/ 前缀 + ·step 后缀）',
  !!nmEl && nmEl.textContent === 'react/step_end · step 2', `实得「${nmEl && nmEl.textContent}」`)
const preEl = step0 && step0.querySelector('pre')
const preTxt = preEl ? preEl.textContent : ''
ok('步骤卡载荷取自 payload.data（非空且含业务字段）',
  preTxt.trim() !== '{}' && /fs\.write/.test(preTxt), `实得「${preTxt}」`)

const cardsInDom = (sel) => document.body.querySelectorAll(sel)

const hitcards = cardsInDom('.hitcard')
ok('审批/澄清卡已插入 DOM（.hitcard）', hitcards.length >= 2, `实得 ${hitcards.length}`)
ok('审批卡按钮存在（.hityes/.hitno）', !!document.body.querySelector('.hityes') && !!document.body.querySelector('.hitno'))
ok('审批卡展示工具名（tool · fs.write）', /fs\.write/.test(document.body.innerHTML))
ok('澄清卡选项行存在（.hitopt 或 .apopt）', !!document.body.querySelector('.hitopt') || !!document.body.querySelector('.apopt'))

const cardErrs = errors.filter((e) => !/directive hook/.test(e))
ok('驱动期间无未捕获错误', cardErrs.length === 0, cardErrs.slice(0, 1).join(' ||| '))
ok('驱动期间无 Vue 错误', ctx.__vueErrors.length === 0, ctx.__vueErrors.slice(0, 1).map((e) => e.split('\n')[0]).join(' | '))

// 清理临时产物
try { rmSync(bundlePath, { force: true }) } catch { /* 不影响结论 */ }

console.log('\n' + '='.repeat(56))
console.log(`  ${pass} passed, ${fail} failed`)
if (fail) { console.log('\n失败项：'); failures.forEach((f) => console.log('  - ' + f)) }
console.log('='.repeat(56) + '\n')
process.exit(fail ? 1 : 0)
