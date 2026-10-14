/**
 * API 契约层 · ASR 客户端（阶段 2）
 * ------------------------------------------------------------------
 * 模式 R（实时）：`openAsrStream()`（WS）+ `startRealtimeCapture()`（麦克风采集）
 * 模式 F（长录音）：`transcribeFile()`
 * 能力探测：`getAsrStatus()`
 *
 * 纪律沿用本仓既有约定：**不裸 fetch**，除一处例外并显式说明（见下）。
 *
 * ★ 三条"不这么写就会静默失效"的约束（均已实测/读码确认）
 * --------------------------------------------------------------
 * 1. **WS 的 token 只能走 query**：浏览器 `WebSocket` API **无法自定义请求头**，
 *    而 Starlette 的全局 http 中间件**不覆盖 WS scope** ⇒ 后端 `/api/asr/ws`
 *    自行校验（`chat.py::_register_asr::_token_ok`，query 或 Authorization 头均可）。
 *    故此处必须 `?token=`；**若"优化"成 header 形式，开 `--token` 的服务会直接 4401**。
 * 2. **`transcribeFile` 是全仓唯一绕过 `client.ts::request()` 的调用**：
 *    文件上传必须用 `FormData` 且**不能**手工设 `Content-Type`
 *    （boundary 由浏览器生成，手设会让后端解析失败）。故此处直接 `fetch` +
 *    `authHeaders()`，并在下方注释标明这是**有意为之**的例外。
 * 3. **AudioWorklet 在单端口部署下可能被 CSP 拦死**：后端给静态页面的 CSP 是
 *    `script-src 'self'`（见 `chat.py::_SPAStaticFiles`），**未放行 `blob:`**，
 *    而 worklet 模块只能以 `blob:` URL 加载 ⇒ `addModule` 会失败。
 *    故捕获异常并**降级 `ScriptProcessorNode`**（已废弃但兼容面最广）。
 *
 * 时间：2026-10-14 22:58 (GMT+8)
 */

import { buildUrl, authHeaders, getToken } from './client'
import type { AsrFileResponse, AsrStatusResponse, AsrWsFrame, AsrCaptureKind } from './types'
import { isAsrFrame } from './asr.logic'

// ---------------------------------------------------------------- 能力探测

/**
 * `GET /api/asr/status`。
 *
 * ★ 该端点在**插件未启用时返回 503**（后端刻意如此：宁可明确不可用，也不假装就绪），
 * 与本仓"HTTP 错误一律抛 `ApiRequestError`"的约定不同 ⇒ 这里**捕获并归一为
 * `{available:false, error}`**，让调用方只需判 `available`，不必写 try/catch。
 */
export async function getAsrStatus(): Promise<AsrStatusResponse> {
  try {
    const res = await fetch(buildUrl('/asr/status'), { headers: authHeaders() })
    if (res.status === 503) {
      const body = (await res.json().catch(() => null)) as { error?: string } | null
      return { ok: false, available: false, error: body?.error || 'ASR 插件未启用' }
    }
    if (!res.ok) return { ok: false, available: false, error: 'HTTP ' + res.status }
    return (await res.json()) as AsrStatusResponse
  } catch (e) {
    return { ok: false, available: false, error: e instanceof Error ? e.message : String(e) }
  }
}

// ---------------------------------------------------------------- 模式 F

/**
 * `POST /api/asr/transcribe-file`：上传录音 → 分段转写结果。
 *
 * ⚠ 有意不走 `client.ts::request()`：文件上传需 `FormData` 且**不可**手工设
 * `Content-Type`（boundary 必须由浏览器生成）。鉴权仍复用 `authHeaders()`。
 */
export async function transcribeFile(file: File | Blob, opts: { signal?: AbortSignal } = {}): Promise<AsrFileResponse> {
  const form = new FormData()
  form.append('file', file, (file as File).name || 'audio.wav')
  const res = await fetch(buildUrl('/asr/transcribe-file'), {
    method: 'POST',
    headers: authHeaders(), // ★ 不设 Content-Type
    body: form,
    signal: opts.signal,
  })
  const body = (await res.json().catch(() => null)) as Record<string, unknown> | null
  if (!res.ok) {
    throw new Error(String(body?.error || ('HTTP ' + res.status)))
  }
  return body as unknown as AsrFileResponse
}

// ---------------------------------------------------------------- 模式 R

export interface AsrStreamHandlers {
  onLoading?: () => void
  onReady?: () => void
  /** 实时改写的半成品（会被覆盖） */
  onPartial?: (text: string) => void
  /** 定稿增量（追加到草稿） */
  onFinal?: (text: string) => void
  /** 引擎不可用等原因（前端据此降级 Web Speech） */
  onError?: (msg: string) => void
  /** 连接关闭（含正常 stop） */
  onClose?: () => void
}

/**
 * 连本地 WS 并开始流式识别。
 *
 * 返回的句柄含 `send(pcm)` / `stop()` / `close()`；`send` 传 **Int16Array**（16kHz mono）。
 * 帧序由后端保证：`loading → ready → (partial|final)* → stop 后结束`。
 */
export function openAsrStream(handlers: AsrStreamHandlers): { ws: WebSocket; send: (pcm: Int16Array) => void; stop: () => void; close: () => void } {
  // ★ token 走 query（见模块注释约束 1）
  const url = new URL(buildUrl('/asr/ws'))
  const token = getToken()
  if (token) url.searchParams.set('token', token)
  const ws = new WebSocket(url.toString())
  ws.binaryType = 'arraybuffer'

  ws.onmessage = (ev: MessageEvent) => {
    // 服务端只发 JSON 文本帧；脏数据/半截帧一律跳过（不中断流）
    if (typeof ev.data !== 'string') return
    let frame: unknown
    try {
      frame = JSON.parse(ev.data)
    } catch {
      return
    }
    if (!isAsrFrame(frame)) return
    switch (frame.type) {
      case 'loading':
        handlers.onLoading?.()
        break
      case 'ready':
        handlers.onReady?.()
        break
      case 'partial':
        handlers.onPartial?.(frame.text)
        break
      case 'final':
        handlers.onFinal?.(frame.text)
        break
      case 'error':
        handlers.onError?.(frame.error)
        break
    }
  }
  ws.onerror = () => handlers.onError?.('WebSocket 连接失败')
  ws.onclose = () => handlers.onClose?.()

  return {
    ws,
    send(pcm: Int16Array) {
      if (ws.readyState === WebSocket.OPEN) ws.send(pcm.buffer as ArrayBuffer)
    },
    stop() {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'stop' }))
    },
    close() {
      try {
        ws.close()
      } catch {
        /* 已关闭 */
      }
    },
  }
}

// ---------------------------------------------------------------- 麦克风采集

/** AudioWorklet 处理器源码（以 blob 加载，见模块注释约束 3） */
const WORKLET_SOURCE = `
class PcmCapture extends AudioWorkletProcessor {
  process(inputs) {
    const ch = inputs[0] && inputs[0][0]
    if (ch && ch.length) {
      // 转 int16 后回传主线程（后端只吃 int16 PCM）
      const out = new Int16Array(ch.length)
      for (let i = 0; i < ch.length; i++) {
        const s = Math.max(-1, Math.min(1, ch[i]))
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff
      }
      this.port.postMessage(out.buffer, [out.buffer])
    }
    return true
  }
}
registerProcessor('pcm-capture', PcmCapture)
`

export interface CaptureHandle {
  kind: AsrCaptureKind
  /** 停止采集并释放麦克风 */
  stop: () => Promise<void>
}

/**
 * 打开麦克风，以 16kHz mono Int16 回调送 PCM 分片。
 *
 * ★ 优先 `AudioWorklet`，失败（**含单端口部署的 CSP 拦 `blob:`**）降级
 *   `ScriptProcessorNode`（已废弃但兼容面最广，Safari 亦可用）。
 *
 * 安全上下文要求：需 `https://` 或 `localhost`（`getUserMedia` 的硬性要求）。
 */
export async function startRealtimeCapture(
  onPcm: (pcm: Int16Array) => void,
): Promise<CaptureHandle> {
  // ★ 直接指定 16kHz：浏览器会重采样，省掉我们自己做重采样（也是最省 CPU 的做法）
  const ctx = new AudioContext({ sampleRate: 16000 })
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
  const src = ctx.createMediaStreamSource(stream)
  const sink = ctx.createMediaStreamDestination() // 静音兜底：避免某些浏览器因无输出而挂起
  src.connect(sink)

  try {
    const blob = new Blob([WORKLET_SOURCE], { type: 'application/javascript' })
    const url = URL.createObjectURL(blob)
    try {
      await ctx.audioWorklet.addModule(url)
    } finally {
      URL.revokeObjectURL(url)
    }
    const node = new AudioWorkletNode(ctx, 'pcm-capture')
    node.port.onmessage = (ev: MessageEvent) => {
      const buf = ev.data as ArrayBuffer
      if (buf) onPcm(new Int16Array(buf))
    }
    src.connect(node)
    return {
      kind: 'worklet',
      stop: async () => {
        try {
          node.disconnect()
          src.disconnect()
          for (const t of stream.getTracks()) t.stop()
          await ctx.close()
        } catch {
          /* 忽略重复释放 */
        }
      },
    }
  } catch {
    // ★ 降级路径（CSP / 不支持 worklet / addModule 失败）
    const proc = ctx.createScriptProcessor(4096, 1, 1)
    proc.onaudioprocess = (ev) => {
      const ch = ev.inputBuffer.getChannelData(0)
      const out = new Int16Array(ch.length)
      for (let i = 0; i < ch.length; i++) {
        const s = Math.max(-1, Math.min(1, ch[i]))
        out[i] = s < 0 ? s * 0x8000 : s * 0x7fff
      }
      onPcm(out)
    }
    src.connect(proc)
    proc.connect(ctx.destination)
    return {
      kind: 'scriptprocessor',
      stop: async () => {
        try {
          proc.disconnect()
          src.disconnect()
          for (const t of stream.getTracks()) t.stop()
          await ctx.close()
        } catch {
          /* 忽略重复释放 */
        }
      },
    }
  }
}
