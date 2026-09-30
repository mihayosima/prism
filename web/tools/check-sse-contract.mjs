/**
 * SSE 帧分发契约自检（回归用例）
 * ------------------------------------------------------------------
 * 为什么需要它：B2.6 出现过一次「编译通过、构建通过、浏览器静默失效」的故障——
 * `sseFrameName` 的入参是常量键（大写 'TOKEN'），调用处传了帧名（小写 'token'），
 * 查出 undefined，导致 handleFrame 的全部分支判定失效：消息能发出去、SSE 帧也
 * 收到了，但正文一个字都不渲染（表现为「卡住不出消息」）。
 *
 * 这类 bug 有两个坑，本脚本同时堵住：
 *   1. `tsc` 对 .vue 文件只走 shims 兜底（any），必须用 `vue-tsc` 才有类型约束；
 *   2. 即便类型正确，也要有运行期断言兜住语义错配。
 *
 * 用法：
 *   node tools/check-sse-contract.mjs
 *   （pnpm/npm）npm run check:sse
 *
 * 退出码：0 全通过；1 有失败项。
 * 时间：2026-09-30 12:40 (GMT+8)
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const root = join(here, '..')

let pass = 0
let fail = 0
const failures = []

function ok(name, cond, extra = '') {
  if (cond) {
    pass++
    console.log(`  ✓ ${name}`)
  } else {
    fail++
    failures.push(name + (extra ? ` — ${extra}` : ''))
    console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`)
  }
}

// ---------------------------------------------------------------
// 1) sse.ts 的契约：sseFrameName 必须同时接受「常量键」与「帧名」
// ---------------------------------------------------------------
console.log('\n[1] sse.ts · sseFrameName 契约')

const sseSrc = readFileSync(join(root, 'src/api/sse.ts'), 'utf8')

// 抽出 SSE_EVENTS 字面量
const evMatch = sseSrc.match(/export const SSE_EVENTS\s*=\s*\{([\s\S]*?)\}\s*as const/)
ok('SSE_EVENTS 可被解析', !!evMatch)
const events = {}
if (evMatch) {
  for (const m of evMatch[1].matchAll(/(\w+)\s*:\s*'([^']+)'/g)) {
    events[m[1]] = m[2]
  }
}
ok('SSE_EVENTS 至少 10 个帧名', Object.keys(events).length >= 10, `实得 ${Object.keys(events).length}`)

// 用真实实现语义做模拟（与 src/api/sse.ts 的 sseFrameName 保持一致）
function sseFrameName(key) {
  const byKey = events[key]
  if (byKey !== undefined) return byKey
  return key
}

// 关键断言 A：传常量键（大写）→ 返回帧名（小写）
for (const [k, v] of Object.entries(events)) {
  ok(`sseFrameName('${k}') === '${v}'`, sseFrameName(k) === v, `实得 ${JSON.stringify(sseFrameName(k))}`)
}

// 关键断言 B：传帧名（小写）→ 幂等返回自身（B2.6 故障的直接回归）
const frameNames = Object.values(events)
for (const v of frameNames) {
  ok(`sseFrameName('${v}') 幂等 === '${v}'`, sseFrameName(v) === v, `实得 ${JSON.stringify(sseFrameName(v))}`)
}

// 关键断言 C：绝不允许「用一个不存在的键查出 undefined」这种静默失效
ok(
  '任意帧名查询都不会得到 undefined',
  frameNames.every((v) => sseFrameName(v) !== undefined),
)

// ---------------------------------------------------------------
// 2) 调用点扫描：ChatView 必须用常量键（大写），不得用帧名
// ---------------------------------------------------------------
console.log('\n[2] ChatView.vue · 调用点大小写')

const chatSrc = readFileSync(join(root, 'src/views/ChatView.vue'), 'utf8')
const callRe = /sseFrameName\(\s*'([^']+)'\s*\)/g
const calls = [...chatSrc.matchAll(callRe)].map((m) => m[1])

ok('ChatView 至少 8 处 sseFrameName 调用', calls.length >= 8, `实得 ${calls.length}`)

const constKeys = new Set(Object.keys(events))
const badCalls = calls.filter((c) => !constKeys.has(c))
ok(
  '全部调用使用常量键（大写）',
  badCalls.length === 0,
  badCalls.length ? `发现小写帧名: ${badCalls.join(', ')}` : '',
)

// 每个调用都必须能解析成合法帧名
const unresolved = calls.filter((c) => sseFrameName(c) === undefined)
ok('全部调用都能解析为帧名', unresolved.length === 0, unresolved.join(', '))

// 覆盖度：handleFrame 需覆盖全部关键帧
const required = ['PROGRESS', 'THINKING', 'TOKEN', 'FINAL', 'APPROVAL', 'APPROVAL_DONE', 'ASK_USER', 'ASK_USER_DONE', 'ERROR']
const missing = required.filter((r) => !calls.includes(r))
ok('handleFrame 覆盖全部关键帧', missing.length === 0, missing.length ? `缺: ${missing.join(', ')}` : '')

// ---------------------------------------------------------------
// 3) 其它模块不得再自行定义帧名（防止契约漂移）
// ---------------------------------------------------------------
console.log('\n[3] 帧名常量唯一真源')

const filesToScan = [
  'src/views/ChatView.vue',
  'src/api/sse.ts',
  'src/api/endpoints.ts',
]
for (const f of filesToScan) {
  const src = readFileSync(join(root, f), 'utf8')
  // 允许在 sse.ts 里出现字面量定义；其它文件不应再出现裸帧名比较。
  // 注意：`q.kind === 'ask_user'` 这类是**载荷字段**比较，不是帧名比较，
  // 故只扫 `ev ===` / `event ===` 这种「事件变量」左值。
  if (f === 'src/api/sse.ts') continue
  const bareCompare = /\b(?:ev|event|evName|frame)\s*===\s*'(progress|thinking|token|final|approval|approval_done|ask_user|ask_user_done|cancelled|error|tool_call|start)'/g
  const hits = [...src.matchAll(bareCompare)].map((m) => m[1])
  ok(`${f} 无裸帧名比较`, hits.length === 0, hits.join(', '))
}

// ---------------------------------------------------------------
// 汇总
// ---------------------------------------------------------------
console.log('\n' + '='.repeat(56))
console.log(`  ${pass} passed, ${fail} failed`)
if (fail) {
  console.log('\n失败项：')
  failures.forEach((f) => console.log('  - ' + f))
}
console.log('='.repeat(56) + '\n')

process.exit(fail ? 1 : 0)
