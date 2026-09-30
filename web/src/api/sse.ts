/**
 * API 契约层 · SSE 客户端（B2.1）
 * ------------------------------------------------------------------
 * 将 legacy `send()` 中的 SSE 读取逻辑抽出为可复用模块，**行为逐行等价**：
 *   - 以 `\n\n` 分帧；
 *   - 仅处理 `data:` 行，`slice(5).trim()` 后 JSON.parse；
 *   - 解析失败的帧静默跳过（不中断流）；
 *   - 走 `AbortController` 支持「停止」。
 *
 * 与 legacy 的差异：仅把 `handleFrame(ev, p)` 改为「回调数组分发」，语义不变。
 * 时间：2026-09-30 11:06 (GMT+8)
 */

import { buildUrl, authHeaders } from './client'
import type {
  StartPayload,
  ProgressPayload,
  TextDeltaPayload,
  FinalPayload,
  ErrorPayload,
  ApprovalPayload,
  AskUserPayload,
} from './types'

/** SSE 事件名常量表（前后端唯一约定的显式化，B3 契约文档与之对齐） */
export const SSE_EVENTS = {
  START: 'start',
  PROGRESS: 'progress',
  THINKING: 'thinking',
  TOKEN: 'token',
  FINAL: 'final',
  CANCELLED: 'cancelled',
  APPROVAL: 'approval',
  APPROVAL_DONE: 'approval_done',
  ASK_USER: 'ask_user',
  ASK_USER_DONE: 'ask_user_done',
  ERROR: 'error',
  TOOL_CALL: 'tool_call',
} as const

export type SseEventName = (typeof SSE_EVENTS)[keyof typeof SSE_EVENTS]

/**
 * 取事件名（SSE 帧名）。
 *
 * 两个用途合一，避免 B2.6 那次的「大小写错配」类 bug（键是大写、帧名是小写，
 * 用键去查值会得到 undefined，导致所有分支判定静默失效）：
 *   - 传常量键（'TOKEN'）→ 返回帧名（'token'）；
 *   - 传帧名本身（'token'）→ 原样返回，幂等。
 *
 * @param key 常量键（大写）或帧名字面量（小写）
 */
export function sseFrameName(key: keyof typeof SSE_EVENTS | SseEventName | string): string {
  const byKey = (SSE_EVENTS as Record<string, string>)[key]
  if (byKey !== undefined) return byKey // 命中常量键
  return key // 已是帧名：原样返回（幂等）
}

/** 帧类型 → payload 的形状映射（供 handler 泛型收窄） */
export interface SsePayloadMap {
  start: StartPayload
  progress: ProgressPayload
  thinking: TextDeltaPayload
  token: TextDeltaPayload
  final: FinalPayload
  cancelled: Record<string, unknown>
  approval: ApprovalPayload
  approval_done: ApprovalPayload
  ask_user: AskUserPayload
  ask_user_done: AskUserPayload
  error: ErrorPayload
  tool_call: Record<string, unknown>
}

/** 单帧处理器签名：ev 为事件名（可能超出常量表 → string），p 为 payload */
export type SseFrameHandler = (ev: string, payload: unknown) => void

export interface StreamChatOptions {
  /** 用户消息 */
  message: string
  /** 帧回调（可注册多个，按注册顺序调用） */
  onFrame: SseFrameHandler
  /** 中止信号（对应「停止」按钮） */
  signal?: AbortSignal
  /** 端点路径，默认 /chat/stream */
  path?: string
}

/**
 * 发起流式对话并逐帧回调。
 *
 * 与 legacy 一致的异常语义：
 *   - 网络/HTTP 失败 → 抛 Error（消息形如 `HTTP 500`）；
 *   - 调用方 abort → 抛 AbortError（name === 'AbortError'），由上层决定如何收尾。
 */
export async function streamChat(opts: StreamChatOptions): Promise<void> {
  const { message, onFrame, signal, path = '/chat/stream' } = opts

  const res = await fetch(buildUrl(path), {
    method: 'POST',
    headers: authHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ message }),
    signal,
  })

  if (!res.ok || !res.body) throw new Error('HTTP ' + res.status)

  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''

  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })

    let i: number
    while ((i = buf.indexOf('\n\n')) >= 0) {
      const line = buf.slice(0, i)
      buf = buf.slice(i + 2)
      if (!line.startsWith('data:')) continue
      let frame: { event?: string; payload?: unknown }
      try {
        frame = JSON.parse(line.slice(5).trim())
      } catch {
        continue // 半截/损坏帧：跳过，不中断流
      }
      onFrame(String(frame.event ?? ''), frame.payload ?? {})
    }
  }
}
