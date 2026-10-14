/**
 * B3 API 契约自检（CORS / Token / API_BASE 直连）
 * ------------------------------------------------------------------
 * 目的：把 `docs/refactor/SSE事件契约-2026-09-30.md` 与 B3 的两项后端开关
 * （`--cors-origin` / `--token`）以及前端 `VITE_API_BASE` 直连能力做成**常驻断言**，
 * 而不是"某次手工 curl 过了就算"。
 *
 * 三段检查：
 *   A. 静态：前端源码里 API_BASE 必须读 env；不得回退成硬编码常量；
 *      buildUrl 必须能处理绝对地址（`http://...`）。
 *   B. 契约文档：`SSE事件契约-*.md` 存在，且帧名表与 `sse.ts` 的 SSE_EVENTS 双向一致。
 *   C. 运行（可选，需后端）：若 PRISM_BACKEND_AUTH 可用，则验证
 *        无 token → 401、Bearer → 200、?token= → 200、OPTIONS 预检放行、
 *        CORS 头精确回显。后端不可达时**跳过并明确标注**（不算失败）。
 *
 * 用法：
 *   node tools/check-api-contract.mjs
 *   npm run check:api
 * 环境变量：
 *   PRISM_BACKEND       默认 http://127.0.0.1:8720（不可用时跳过 C 段）
 *   PRISM_TOKEN         该实例的 token（留空则 C 段按"未启用鉴权"核对）
 *   PRISM_ORIGIN        期望被 CORS 放行的来源（留空则跳过 CORS 断言）
 * 退出码：0 全通过 / 1 有失败。
 * 时间：2026-09-30 13:25 (GMT+8)
 */

import { readFileSync, existsSync, readdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')
const repo = join(root, '..')

const BACKEND = (process.env.PRISM_BACKEND || 'http://127.0.0.1:8720').replace(/\/$/, '')
const TOKEN = process.env.PRISM_TOKEN || ''
const ORIGIN = process.env.PRISM_ORIGIN || ''

let pass = 0
let fail = 0
const failures = []
function ok(name, cond, extra = '') {
  if (cond) { pass++; console.log(`  ✓ ${name}`) }
  else { fail++; failures.push(name + (extra ? ` — ${extra}` : '')); console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`) }
}

const read = (p) => readFileSync(p, 'utf8')

// ============================================================
// A. 静态：前端 API_BASE 可配置 + buildUrl 支持绝对地址
// ============================================================
console.log('\n[A] 前端 API_BASE 可配置（B3.3）')
const clientPath = join(root, 'src', 'api', 'client.ts')
const client = read(clientPath)

ok('API_BASE 读 import.meta.env.VITE_API_BASE',
  /export\s+const\s+API_BASE\s*=\s*import\.meta\.env\.VITE_API_BASE\s*\|\|\s*'\/api'/.test(client),
  '未找到 `export const API_BASE = import.meta.env.VITE_API_BASE || \'/api\'`')

ok('API_BASE 不再是硬编码常量',
  !/export\s+const\s+API_BASE\s*=\s*'\/api'\s*$/m.test(client),
  '仍存在硬编码 `export const API_BASE = \'/api\'`')

ok('buildUrl 处理绝对地址（http(s)://）',
  /https?:\\?\/\\?\//.test(client) && /new URL\(/.test(client),
  'buildUrl 未用 new URL 解析，直连形态下 host 会丢失')

ok('authHeaders 注入 Bearer',
  /Authorization.*Bearer/.test(client), '未找到 Bearer 注入')

ok('buildUrl 不再注入 ?token=（鉴权走 Authorization 头，防进日志）',
  !/searchParams\.set\('token'/.test(client), 'buildUrl 仍在 URL 里拼 token（会进代理/访问日志）')

ok('.env.example 存在（三种部署形态有据可依）',
  existsSync(join(root, '.env.example')))

if (existsSync(join(root, '.env.example'))) {
  const envEx = read(join(root, '.env.example'))
  ok('.env.example 声明 VITE_API_BASE', /VITE_API_BASE/.test(envEx))
  ok('.env.example 说明三种形态', ['形态 A', '形态 B', '形态 C'].every((s) => envEx.includes(s)),
    '缺少形态说明（A 同源 / B 直连 / C 单端口）')
}

// ============================================================
// B. 契约文档与 sse.ts 帧名双向一致
// ============================================================
console.log('\n[B] SSE 契约文档对齐（B3.4）')
const docsDir = join(repo, 'docs', 'refactor')
const contractFile = existsSync(docsDir)
  ? readdirSync(docsDir).find((f) => /^SSE事件契约-.*\.md$/.test(f))
  : undefined
ok('契约文档存在（docs/refactor/SSE事件契约-*.md）', !!contractFile,
  contractFile ? '' : `未在 ${docsDir} 找到`)
if (contractFile) {
  const doc = read(join(docsDir, contractFile))
  ok('文档含"编制时间"', /编制时间：/.test(doc))
  ok('文档含"更新记录"', /##\s*更新记录/.test(doc))
  ok('文档强调载荷层次陷阱', /载荷层次|payload\.data/.test(doc))
  ok('文档说明非标准双行 SSE', /非\s*\*?\*?标准|不是\*?\*?标准|单行/.test(doc))

  // sse.ts 的帧名（值）必须都在文档里出现
  const sse = read(join(root, 'src', 'api', 'sse.ts'))
  const block = sse.match(/export const SSE_EVENTS\s*=\s*\{([\s\S]*?)\}\s*as const/)
  ok('解析出 sse.ts SSE_EVENTS 常量表', !!block)
  if (block) {
    const names = [...block[1].matchAll(/:\s*'([^']+)'/g)].map((m) => m[1])
    ok(`SSE_EVENTS 有 12 个帧名（实得 ${names.length}）`, names.length === 12, names.join(','))
    const missing = names.filter((n) => !doc.includes('`' + n + '`'))
    ok('每个帧名都在契约文档中登记', missing.length === 0,
      missing.length ? '缺失: ' + missing.join(',') : '')
  }

  // 后端 progress.py 的 FRAME_TYPES 值必须都能在文档里找到
  const progPath = join(repo, 'agent', 'sys', 'progress.py')
  if (existsSync(progPath)) {
    const prog = read(progPath)
    const ft = prog.match(/FRAME_TYPES:\s*dict\[str,\s*str\]\s*=\s*\{([\s\S]*?)\n\}/)
    ok('解析出 progress.py FRAME_TYPES', !!ft)
    if (ft) {
      const vals = [...ft[1].matchAll(/:\s*"([^"]+)"/g)].map((m) => m[1])
      // ★ 7 类（2026-10-15）：新增 `Events.TODO_PROGRESS → "todo"`（规划完成度帧）。
      //   这个数字**故意写死**——它的作用就是逼着"后端加帧 ⇒ 契约文档必须同步登记"；
      //   本次它成功抓出了文档漏登记 todo 的欠账（已补 §3.13），不是该放宽的断言。
      ok(`FRAME_TYPES 映射 7 类（实得 ${vals.length}）`, vals.length === 7, vals.join(','))
      const miss2 = vals.filter((v) => !doc.includes('`' + v + '`'))
      ok('每个映射值都在契约文档中登记', miss2.length === 0,
        miss2.length ? '缺失: ' + miss2.join(',') : '')
    }
  }
}

// ============================================================
// C. 运行：真实后端（可选）
// ============================================================
console.log('\n[C] 真实后端鉴权 / CORS（可选）')
const realFetch = globalThis.fetch
let backendUp = false
try {
  const r = await realFetch(`${BACKEND}/api/status`, { signal: AbortSignal.timeout(3000) })
  backendUp = r.ok
} catch { /* 下面标注 */ }

if (!backendUp) {
  console.log(`  ⊘ 跳过：后端不可达（${BACKEND}）—— 属"未验证"而非失败`)
  console.log('    若要核验，请先启动带 CORS/Token 的实例：')
  console.log(`    python -m agent.run.chat --cors-origin http://127.0.0.1:5174 --token <T>`)
  console.log(`    再：PRISM_BACKEND=http://127.0.0.1:8720 PRISM_TOKEN=<T> \\`)
  console.log(`        PRISM_ORIGIN=http://127.0.0.1:5174 npm run check:api`)
} else {
  const hdr = TOKEN ? { Authorization: 'Bearer ' + TOKEN } : {}
  const q = (p) => (TOKEN ? `${p}${p.includes('?') ? '&' : '?'}token=${encodeURIComponent(TOKEN)}` : p)

  if (TOKEN) {
    const r1 = await realFetch(`${BACKEND}/api/tools`)
    ok('无 token → 401', r1.status === 401, `实得 ${r1.status}`)

    const r2 = await realFetch(`${BACKEND}/api/tools`, { headers: hdr })
    ok('Bearer → 200', r2.status === 200, `实得 ${r2.status}`)

    const r3 = await realFetch(`${BACKEND}/api/tools${q('?')}`.replace('/api/tools?', '/api/tools?'))
    ok('?token= → 200', r3.status === 200, `实得 ${r3.status}`)

    const r4 = await realFetch(`${BACKEND}/api/tools`, {
      headers: { Authorization: 'Bearer definitely-wrong' } })
    ok('错误 token → 401', r4.status === 401, `实得 ${r4.status}`)

    // 放行清单
    const r5 = await realFetch(`${BACKEND}/api/status`)
    ok('/api/status 免鉴权放行', r5.status === 200, `实得 ${r5.status}`)
    const r6 = await realFetch(`${BACKEND}/`)
    ok('/ 免鉴权放行', r6.status === 200, `实得 ${r6.status}`)

    // 预检必须放行（不带 Authorization）
    const r7 = await realFetch(`${BACKEND}/api/tools`, {
      method: 'OPTIONS',
      headers: { 'Access-Control-Request-Method': 'POST', ...(ORIGIN ? { Origin: ORIGIN } : {}) } })
    ok('OPTIONS 预检放行（200/204）', r7.status === 200 || r7.status === 204, `实得 ${r7.status}`)

    const r8 = await realFetch(`${BACKEND}/api/plugins${q('?')}`.replace('/api/plugins?', '/api/plugins?'))
    let n = 0
    try { n = ((await r8.json()).plugins || []).length } catch { /* ignore */ }
    ok('token 通道数据面可用（/api/plugins 有插件）', r8.status === 200 && n > 0,
      `status=${r8.status} plugins=${n}`)
  } else {
    console.log('  ⊘ PRISM_TOKEN 未设 —— 跳过鉴权断言（该实例视为未启用 --token）')
    const r = await realFetch(`${BACKEND}/api/tools`)
    ok('未设 token → 直接 200（默认不鉴权）', r.status === 200, `实得 ${r.status}`)
  }

  if (ORIGIN) {
    const r = await realFetch(`${BACKEND}/api/status`, { headers: { Origin: ORIGIN } })
    const ao = r.headers.get('access-control-allow-origin')
    const ac = r.headers.get('access-control-allow-credentials')
    ok('CORS 合法来源精确回显', ao === ORIGIN, `实得 ${ao}`)
    ok('CORS allow-credentials=true', ac === 'true', `实得 ${ac}`)

    const r2 = await realFetch(`${BACKEND}/api/status`, {
      headers: { Origin: 'http://evil.example' } })
    ok('CORS 非法来源不回显 allow-origin',
      r2.headers.get('access-control-allow-origin') === null,
      `实得 ${r2.headers.get('access-control-allow-origin')}`)
  } else {
    console.log('  ⊘ PRISM_ORIGIN 未设 —— 跳过 CORS 断言')
  }
}

// ============================================================
console.log('\n' + '='.repeat(56))
console.log(`  ${pass} passed, ${fail} failed`)
if (fail) { console.log('\n失败项：'); failures.forEach((f) => console.log('  - ' + f)) }
console.log('='.repeat(56) + '\n')
process.exit(fail ? 1 : 0)
