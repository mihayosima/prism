/**
 * ASR 前端接线契约自检（阶段 2）
 * ------------------------------------------------------------------
 * 为什么需要它：本仓前端**没有 DOM 测试库**（未装 jsdom/happy-dom），
 * 且历史上出现过"编译通过、构建通过、浏览器静默失效"的故障
 * （见 `check-sse-contract.mjs` 的 `sseFrameName` 案例）。ASR 侧同类坑有三处，
 * 都不是 `tsc` 能拦住的：
 *
 *   1. **WS 的 token 走 `?token=`**：浏览器 `WebSocket` 无法自定义 header，
 *      后端 `/api/asr/ws` 自校验（`chat.py::_register_asr::_token_ok`）——
 *      若有人"优化"成 header 形式，开 `--token` 的服务会直接 4401，
 *      而**类型检查与构建全绿**。
 *   2. **vite proxy 必须 `ws: true`**：缺它时 WS 握手被当普通 HTTP 处理，
 *      前端只会"连不上 → 静默降级"，没有任何报错指向 proxy。
 *   3. **定稿只回填草稿、绝不自动发送**：`send()` 的自动调用在聊天场景是
 *      替用户说话（产品红线），而它是行为问题、不是类型问题。
 *
 * 与既有 `check-*.mjs` 同款做法：**读源码文本 + 静态断言**，不依赖后端在跑。
 * 真连后端的帧序验证是**人工/一次性**动作，不进 `npm run check`
 * （既有 `check-single-port.mjs` 属"要起后端"的集成检查，语义不同）。
 *
 * 用法：
 *   node tools/check-asr-contract.mjs
 *   （pnpm/npm）npm run check:asr
 *
 * 退出码：0 全通过；1 有失败项。
 * 时间：2026-10-14 23:12 (GMT+8)
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
    failures.push(name + (extra ? ' — ' + extra : ''))
    console.log(`  ✗ ${name}${extra ? ' — ' + extra : ''}`)
  }
}

const read = (p) => {
  try {
    return readFileSync(join(root, p), 'utf8')
  } catch {
    return ''
  }
}

const asr = read('src/api/asr.ts')
const logic = read('src/api/asr.logic.ts')
const chatView = read('src/views/ChatView.vue')
const vite = read('vite.config.ts')
const types = read('src/api/types.ts')
const pkg = read('package.json')

// ---------------------------------------------------------------
// 1) WS token 必须走 query（header 形式会被后端 4401）
// ---------------------------------------------------------------
console.log('\n[1] WS 鉴权接线（token 走 query）')
{
  ok('asr.ts 读取 token（getToken）', /getToken/.test(asr))
  ok(
    'WS URL 注入 token 查询参数',
    /searchParams\.set\(\s*['"]token['"]/.test(asr),
    '未找到 searchParams.set("token"…) —— 浏览器 WebSocket 不能带 header，缺它会 4401',
  )
  ok('WS 连接经 buildUrl 拼地址（与 API_BASE 开关一致）', /buildUrl\(\s*['"]\/asr\/ws/.test(asr))
  ok('未把 token 写进 WS 的 header（WebSocket API 不支持）', !/new WebSocket\([^)]*headers/i.test(asr))
}

// ---------------------------------------------------------------
// 2) vite proxy 必须透传 WS
// ---------------------------------------------------------------
console.log('\n[2] dev 期 WS 透传')
{
  ok('vite.config.ts 存在 /api proxy', /['"]\/api['"]/.test(vite))
  ok(
    'proxy 开启 ws: true',
    /ws\s*:\s*true/.test(vite),
    '缺 ws:true ⇒ dev 期 WS 握手被当普通 HTTP，前端只会静默降级',
  )
}

// ---------------------------------------------------------------
// 3) 定稿只回填草稿，绝不自动发送
// ---------------------------------------------------------------
console.log('\n[3] 定稿不自动发送（产品红线）')
{
  const handler = chatView.match(/function onVoiceFinal[\s\S]*?\n}/)
  ok('ChatView 有 onVoiceFinal 处理函数', !!handler)
  ok(
    'onVoiceFinal 只写 input.value（回填草稿）',
    !!handler && /input\.value\s*=/.test(handler[0]),
  )
  ok(
    'onVoiceFinal 绝不调用 send()',
    !!handler && !/\bsend\(\)/.test(handler[0]),
    '在 onVoiceFinal 里调 send() = 替用户说话，属产品红线',
  )
  ok('onVoiceFinal 后聚焦输入框（便于用户核对/修改）', !!handler && /focus\(\)/.test(handler[0]))
}

// ---------------------------------------------------------------
// 4) 降级链顺序与诚实性
// ---------------------------------------------------------------
console.log('\n[4] 降级链（本地 → Web Speech → 纯文本）')
{
  ok('pickStrategy 存在', /export function pickStrategy/.test(logic))
  ok('Web Speech 探测含 webkit 前缀与标准名', /webkitSpeechRecognition/.test(logic) && /SpeechRecognition/.test(logic))
  ok(
    '降级到 Web Speech 时提示"音频将出本机"',
    /云端/.test(logic),
    '本地私有是首版选型；静默降级会违背用户预期',
  )
  ok('纯文本兜底存在', /'text'/.test(logic))
  // 顺序：先判 engineReady 再回落
  const pick = logic.match(/export function pickStrategy[\s\S]*?\n}/)
  ok(
    'pickStrategy 先判本地（engineReady）再回落',
    !!pick && /engineReady/.test(pick[0]) && /webSpeechSupported/.test(pick[0]),
  )
}

// ---------------------------------------------------------------
// 5) 文件上传不得手工设 Content-Type（boundary 会被破坏）
// ---------------------------------------------------------------
console.log('\n[5] 文件上传（FormData 语义）')
{
  ok('transcribeFile 用 FormData', /new FormData\(\)/.test(asr))
  const fn = asr.match(/export async function transcribeFile[\s\S]*?\n}/)
  ok(
    '未手工设 Content-Type（boundary 必须由浏览器生成）',
    !!fn && !/['"]Content-Type['"]\s*:/.test(fn[0]),
    "手设 Content-Type 会让后端无法解析 multipart 边界",
  )
  ok('文件上传仍带鉴权头', !!fn && /authHeaders\(\)/.test(fn[0]))
}

// ---------------------------------------------------------------
// 6) DTO 已登记进唯一真源
// ---------------------------------------------------------------
console.log('\n[6] DTO 真源（api/types.ts）')
{
  for (const name of ['AsrSegment', 'AsrFileResponse', 'AsrStatusResponse', 'AsrWsFrame']) {
    ok(`types.ts 登记 ${name}`, new RegExp(`export (interface|type) ${name}\\b`).test(types))
  }
  ok(
    'AsrSegment 字段与后端 to_dict 逐字一致（snake_case）',
    /start_sec/.test(types) && /end_sec/.test(types) && !/startSec/.test(types),
    '后端未做 camel 转换，字段名对不上会静默拿到 undefined',
  )
}

// ---------------------------------------------------------------
// 7) AudioWorklet 降级路径存在（单端口 CSP 会拦 blob:）
// ---------------------------------------------------------------
console.log('\n[7] 采集降级（worklet → ScriptProcessorNode）')
{
  ok('优先 AudioWorklet', /addModule\(/.test(asr))
  ok(
    'worklet 失败有降级分支（ScriptProcessorNode）',
    /createScriptProcessor/.test(asr) && /catch/.test(asr),
    '后端静态页 CSP 为 script-src self（未放行 blob:），worklet 会被拦',
  )
  ok('采集按 16kHz 打开 AudioContext（省掉自研重采样）', /sampleRate:\s*16000/.test(asr))
  ok('降级类型在 DTO 中登记', /AsrCaptureKind/.test(types))
}

// ---------------------------------------------------------------
// 8) 契约脚本自身挂进 npm run check
// ---------------------------------------------------------------
console.log('\n[8] 挂进 npm run check')
{
  ok('package.json 有 check:asr', /"check:asr"/.test(pkg))
  ok('npm run check 包含 check:asr', /"check"\s*:\s*"[^"]*check:asr/.test(pkg))
}

// ---------------------------------------------------------------
console.log(`\nASR 契约自检：${pass} 通过，${fail} 失败`)
if (fail) {
  console.log('失败项：')
  for (const f of failures) console.log('  - ' + f)
  process.exit(1)
}
process.exit(0)
