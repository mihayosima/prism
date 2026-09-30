/**
 * B4 单端口托管自检（前端产物 + /api 共存）
 * ------------------------------------------------------------------
 * 目的：把「后端单端口托管 `web/dist/`」这件事做成**常驻断言**，而不是
 * "某次手工 curl 过了就算"。B4 删掉了 `chat.py` 内联的 `_PAGE`（55KB），
 * 页面改由 `StaticFiles(html=True)` 挂载 `/` 提供 —— 这引入了三条容易
 * 悄悄坏掉的契约：
 *
 *   1. **路由优先级**：`/api/*` 必须先注册（Starlette 按注册顺序匹配），
 *      否则 StaticFiles 会把 `/api/status` 也当静态路径 → 404；
 *   2. **HTML 必须 no-store**：产物文件名带内容哈希，`index.html` 若被
 *      启发式缓存，改版后会引用**已不存在的旧 hash 文件** → 整页白屏；
 *   3. **静态资源必须免鉴权**：浏览器用 `<script src="/assets/...">` 加载
 *      产物时**无法携带 token**；若 `--token` 把静态资源也拦住 → 一开鉴权
 *      就白屏。数据面 `/api/*` 仍须校验。
 *
 * 本脚本对**一个正在运行的后端实例**发真实 HTTP 请求逐条核对上述契约。
 *
 * 用法：
 *   # 1) 先起一个托管了 dist 的实例（在本仓库根目录）
 *   python -m agent.run.chat --port 8760
 *   # 2) 跑自检
 *   PRISM_BACKEND=http://127.0.0.1:8760 node tools/check-single-port.mjs
 *   # 也可 npm run check:single
 *
 * 环境变量：
 *   PRISM_BACKEND  后端基地址（默认 http://127.0.0.1:8720）
 *   PRISM_TOKEN    若该实例启用了 --token，填入以核对「静态放行 / 数据面拦截」
 * 退出码：0 全通过 / 1 有失败 / 2 后端不可达（跳过，不算失败）。
 * 时间：2026-09-30 20:05 (GMT+8)
 */

import { readFileSync, existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')          // web/
const repo = join(root, '..')          // 仓库根

const BACKEND = (process.env.PRISM_BACKEND || 'http://127.0.0.1:8720').replace(/\/$/, '')
const TOKEN = process.env.PRISM_TOKEN || ''

let pass = 0
let fail = 0
const failures = []
function ok(name, cond, extra = '') {
  if (cond) { pass++; console.log(`  ✓ ${name}`) }
  else { fail++; failures.push(name + (extra ? ` — ${extra}` : '')); console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`) }
}
function skip(name, why) {
  console.log(`  ⊘ ${name} —— ${why}`)
}

// ---------- 0. 前置：dist 存在 + 后端可达 ----------
console.log('\n[0] 前置检查')

const distDir = join(root, 'dist')
const distIndex = join(distDir, 'index.html')
const hasDist = existsSync(distIndex)
ok('web/dist/index.html 存在（托管输入就位）', hasDist,
  hasDist ? '' : '请先 `cd web && npm run build`')

let distHtml = ''
if (hasDist) {
  distHtml = readFileSync(distIndex, 'utf8')
  ok('dist/index.html 引用至少一个 /assets/ 产物',
    /\/assets\/[^"']+\.(js|css)/.test(distHtml),
    '未找到 /assets/ 引用 —— 产物结构异常')
}

let reachable = false
try {
  const r = await fetch(BACKEND + '/api/status')
  reachable = r.ok
} catch { /* 下面统一处理 */ }

if (!reachable) {
  console.log(`\n后端不可达：${BACKEND}`)
  console.log('跳过单端口端到端断言。启动示例（在仓库根目录）：')
  console.log('  python -m agent.run.chat --port 8760')
  console.log(`  PRISM_BACKEND=http://127.0.0.1:8760 npm run check:single\n`)
  console.log('='.repeat(56))
  console.log('  0 passed, 0 failed（后端不可达，已跳过）')
  console.log('='.repeat(56) + '\n')
  process.exit(2)
}
ok('后端可达', true)

// ---------- 1. GET / 返回前端首页 ----------
console.log('\n[1] GET / —— 首页由 StaticFiles 提供（B4 核心）')

const homeRes = await fetch(BACKEND + '/')
const homeHtml = await homeRes.text()
ok('GET / → 200', homeRes.status === 200, `实得 ${homeRes.status}`)
ok('GET / content-type 为 text/html',
  (homeRes.headers.get('content-type') || '').startsWith('text/html'),
  homeRes.headers.get('content-type') || '(无)')
ok('GET / 返回的是 dist/index.html（含 Vue 挂载点 #app）',
  /<div\s+id="app"\s*>\s*<\/div>/.test(homeHtml),
  homeHtml.slice(0, 80).replace(/\s+/g, ' '))
ok('GET / 仍然带 Cache-Control: no-store（防旧 hash 文件被缓存）',
  (homeRes.headers.get('cache-control') || '').includes('no-store'),
  homeRes.headers.get('cache-control') || '(无)')
ok('页面不再内联 _PAGE 级大脚本（首页体积 = 产物级，< 3KB）',
  homeHtml.length < 3000,
  `实得 ${homeHtml.length} 字符`)

// 从真实响应里抽出被引用的资源，逐个回请（而不是猜文件名）
const assetRefs = [...homeHtml.matchAll(/\/assets\/[^"')\s]+\.(?:js|css)/g)].map((m) => m[0])

// ---------- 2. 静态资源可达 ----------
console.log('\n[2] /assets/* —— 静态资源可达')

ok('首页至少引用一个静态资源', assetRefs.length > 0, `实得 ${assetRefs.length}`)

for (const ref of assetRefs) {
  const r = await fetch(BACKEND + ref)
  const ct = r.headers.get('content-type') || ''
  const want = ref.endsWith('.js') ? 'javascript' : 'css'
  ok(`${ref} → 200`, r.status === 200, `实得 ${r.status}`)
  ok(`${ref} content-type 含 ${want}`, ct.includes(want), ct || '(无)')
}
if (assetRefs.length) {
  const cc = (await fetch(BACKEND + assetRefs[0])).headers.get('cache-control') || ''
  ok('静态资源不带 no-store（内容哈希文件应可缓存，只有 HTML 才 no-store）',
    !cc.includes('no-store'), cc || '(无 Cache-Control)')
} else {
  skip('静态资源缓存策略', '未从首页解析出任何 /assets/ 引用')
}

// ---------- 3. /api/* 未被 StaticFiles 抢走 ----------
console.log('\n[3] /api/* —— 路由优先级正确（未被 StaticFiles 吞掉）')

const statusRes = await fetch(BACKEND + '/api/status')
ok('/api/status → 200（健康探针）', statusRes.status === 200, `实得 ${statusRes.status}`)
let statusJson = null
try { statusJson = await statusRes.json() } catch { /* ignore */ }
ok('/api/status 返回 JSON（不是 HTML）',
  !!statusJson && typeof statusJson === 'object' && 'provider_label' in statusJson,
  statusJson ? JSON.stringify(statusJson).slice(0, 80) : '(非 JSON)')

const toolsRes = await fetch(BACKEND + '/api/tools' + (TOKEN ? `?token=${encodeURIComponent(TOKEN)}` : ''))
ok(`/api/tools → ${TOKEN ? '200（带 token）' : '200'}`, toolsRes.status === 200, `实得 ${toolsRes.status}`)

// ---------- 4. 未知路径 404（而非回落到首页） ----------
console.log('\n[4] 未知静态路径 → 404')

const missing = await fetch(BACKEND + '/assets/__definitely_missing__.js')
ok('/assets/不存在.js → 404', missing.status === 404, `实得 ${missing.status}`)

// ---------- 5. 鉴权与静态托管共存（--token 时） ----------
console.log('\n[5] `--token` 与静态托管共存')
if (TOKEN) {
  const h = await fetch(BACKEND + '/')                      // 无 token
  ok('无 token 时 GET / → 200（首页放行）', h.status === 200, `实得 ${h.status}`)

  const a = await fetch(BACKEND + (assetRefs[0] || '/assets/x.js'))
  ok('无 token 时 GET /assets/* → 200（<script> 无法带 token，必须放行）',
    a.status === 200, `实得 ${a.status}`)

  const apiNoTok = await fetch(BACKEND + '/api/plugins')
  ok('无 token 时 GET /api/plugins → 401（数据面仍受保护）',
    apiNoTok.status === 401, `实得 ${apiNoTok.status}`)

  const apiTok = await fetch(BACKEND + '/api/plugins?token=' + encodeURIComponent(TOKEN))
  ok('带 ?token= 时 GET /api/plugins → 200', apiTok.status === 200, `实得 ${apiTok.status}`)
} else {
  skip('无 token 时静态放行 / 数据面 401', 'PRISM_TOKEN 未设（该实例视为未启用 --token）')
  skip('带 token 时数据面 200', 'PRISM_TOKEN 未设')
  console.log('    要跑满本段，起实例时加 --token 并传入同一值，例如：')
  console.log('      python -m agent.run.chat --port 8760 --token devsecret')
  console.log('      PRISM_BACKEND=http://127.0.0.1:8760 PRISM_TOKEN=devsecret npm run check:single')
}

// ---------- 收尾 ----------
console.log('\n' + '='.repeat(56))
console.log(`  ${pass} passed, ${fail} failed`)
if (fail) { console.log('\n失败项：'); failures.forEach((f) => console.log('  - ' + f)) }
console.log('='.repeat(56) + '\n')
process.exit(fail ? 1 : 0)
