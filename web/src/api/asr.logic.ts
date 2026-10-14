/**
 * ASR 纯逻辑层（阶段 2）
 * ------------------------------------------------------------------
 * 与 `settings.logic.ts` 同款定位：**把可测的判断从组件里剥出来**，
 * 使降级矩阵这类"错了就静默失效"的逻辑能被单测覆盖。
 *
 * 为什么单独一个文件（本仓前端既有约定）：
 *   - 视图/store 不得直接 fetch（纪律见 `api/client.ts`），接线都在 `asr.ts`；
 *   - 本文件**零 DOM 依赖**、零网络，故 vitest 无需 jsdom/happy-dom
 *     （本仓**没有**装这两个包——`package.json` 只有 vue/vite/vitest/vue-tsc/typescript）。
 *
 * 降级链（方案 §3.3，**顺序不可换**）
 * ----------------------------------
 *   1. 本地引擎（WS `/api/asr/ws`）——音频不出本机，首选；
 *   2. 浏览器 Web Speech API——本地引擎未就绪/WS 不可达时兜底（**音频会出本机**，
 *      浏览器侧实现；故产品上应让用户知情，不静默切换）；
 *   3. 纯文本输入。
 *
 * 时间：2026-10-14 22:55 (GMT+8)
 */

import type { AsrWsFrame } from './types'

/** 降级策略 */
export type AsrStrategy = 'local' | 'webspeech' | 'text'

/** 能力探测结果（由 `asr.ts` 在运行时填充） */
export interface AsrCapabilities {
  /** WebSocket 可用（几乎恒 true；保留是因为测试要构造"不支持"） */
  wsSupported: boolean
  /** `webkitSpeechRecognition` / `SpeechRecognition` 存在 */
  webSpeechSupported: boolean
  /** 后端引擎就绪（`GET /api/asr/status` 的 `available` + `state==='ready'`） */
  engineReady: boolean
}

/**
 * 依能力选策略。
 *
 * ★ 判定顺序即优先级，**不可调换**：
 *   引擎就绪 ⇒ 本地（隐私最优）；
 *   否则浏览器支持 ⇒ Web Speech（功能可用但音频出本机）；
 *   否则 ⇒ 纯文本（明说"不可用"，不假装还能语音）。
 *
 * 刻意**不**把"WS 可达性"与"引擎就绪"合成一个布尔：二者失败时的用户提示不同
 * （前者是网络/代理问题，后者是"服务端没装 sherpa-onnx 或模型没下完"），
 * 合并后前端只能给出含混的"语音不可用"。
 */
export function pickStrategy(caps: AsrCapabilities): AsrStrategy {
  if (caps.wsSupported && caps.engineReady) return 'local'
  if (caps.webSpeechSupported) return 'webspeech'
  return 'text'
}

/**
 * 面向用户的降级说明（**诚实优先**）。
 *
 * 切到 Web Speech 时**必须告知音频将上传到浏览器厂商的云端**——
 * 本仓首版选型是"本地私有"，静默降级等于违背用户预期。
 */
export function strategyNotice(strategy: AsrStrategy, engineReason = ''): string {
  switch (strategy) {
    case 'local':
      return '语音输入：本地引擎（音频不出本机）'
    case 'webspeech':
      return engineReason
        ? `语音输入：已降级为浏览器识别（${engineReason}）；注意音频将上传至浏览器厂商云端`
        : '语音输入：已降级为浏览器识别；注意音频将上传至浏览器厂商云端'
    default:
      return '语音输入不可用（本地引擎未就绪，且当前浏览器不支持语音识别），请用文字输入'
  }
}

/**
 * 探测 Web Speech API。
 *
 * 两个实现名都要查：Chrome/Edge 是 `webkitSpeechRecognition`，标准名是
 * `SpeechRecognition`（Firefox 曾实现后又移除，Safari 部分版本有）。
 * 挂到 `window` 上取，故**必须在浏览器环境调用**（单测用注入对象替代）。
 */
export function isSpeechApiAvailable(win: unknown = typeof window === 'undefined' ? undefined : window): boolean {
  if (!win || typeof win !== 'object') return false
  const w = win as Record<string, unknown>
  return typeof w.webkitSpeechRecognition === 'function' || typeof w.SpeechRecognition === 'function'
}

/**
 * 累积 `final` 帧文本。
 *
 * 实时通道里 `partial` 是"当前这句的实时改写"（会被反复覆盖），
 * `final` 才是定稿增量 ⇒ 草稿 = 所有 final 拼接。
 * ⚠ 后端 `SherpaStreamingSession` 在 endpoint 命中时**已 reset**，
 * 故 final 之间不重叠，直接拼接即可（重叠会导致重复字）。
 */
export function concatFinals(texts: string[]): string {
  return texts.join('')
}

/** 秒 → `mm:ss` / `h:mm:ss`（分段列表与胶囊显示用） */
export function formatDuration(sec: number): string {
  const s = Math.max(0, Math.floor(Number(sec) || 0))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const r = s % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return h > 0 ? `${h}:${pad(m)}:${pad(r)}` : `${pad(m)}:${pad(r)}`
}

/** 帧类型守卫（WS `message` 事件里 JSON 解析后先过它，避免脏数据进状态机） */
export function isAsrFrame(x: unknown): x is AsrWsFrame {
  if (!x || typeof x !== 'object') return false
  const t = (x as { type?: unknown }).type
  if (typeof t !== 'string') return false
  return t === 'loading' || t === 'ready' || t === 'partial' || t === 'final' || t === 'error'
}

/** 引擎是否处于"可服务"状态（`state === 'ready'` 才算；idle 需先触发加载） */
export function isEngineServable(state: string | undefined): boolean {
  return state === 'ready'
}
