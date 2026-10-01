/**
 * API 契约层 · HTTP 客户端（B2.1）
 * ------------------------------------------------------------------
 * 所有 HTTP 调用的唯一出口。职责：
 *   1. 统一前缀 `/api`；
 *   2. 自动注入鉴权（Bearer header；B3 启用后由 sessionStorage 提供 token）；
 *   3. 统一 JSON 解析与错误归一；
 *   4. 401 拦截：交由注册的 onUnauthorized 回调（B3 弹 token 输入框）。
 *
 * 纪律：视图/store **不得** 直接 `fetch`，一律经此模块。
 * 时间：2026-09-30 11:06 (GMT+8)
 */

import type { ApiError } from './types'
import { getSessionId } from './session'

/**
 * 后端 API 基地址（B3 起可配置）。
 *
 * 三种部署形态共用一个开关：
 *   - **dev（默认）**：留空 → `'/api'` 相对根路径，由 Vite `proxy` 转发到后端
 *     （同源，不需要 CORS）；
 *   - **前后端分离直连**：`.env.local` 设 `VITE_API_BASE=http://127.0.0.1:8720/api`
 *     —— 此时**必须**同时给后端传 `--cors-origin http://127.0.0.1:5174`；
 *   - **B4 单端口托管**：留空即可（后端同时托管 `dist/`，天然同源）。
 *
 * 注意：值末尾**不要**带 `/`（`buildUrl` 会再拼 `path`）。
 */
export const API_BASE = import.meta.env.VITE_API_BASE || '/api'

/** token 在 sessionStorage 中的键名（B3 启用） */
export const TOKEN_KEY = 'prism_token'

/** 未授权回调（B3 注册为「弹出 token 输入框」） */
type UnauthorizedHandler = () => void
let onUnauthorized: UnauthorizedHandler | null = null
export function setUnauthorizedHandler(fn: UnauthorizedHandler | null): void {
  onUnauthorized = fn
}

/** 读取当前 token：优先 sessionStorage，其次 URL `?token=`（一次性落盘） */
export function getToken(): string {
  try {
    const fromUrl = new URLSearchParams(location.search).get('token')
    if (fromUrl) {
      sessionStorage.setItem(TOKEN_KEY, fromUrl)
      // 清掉地址栏里的 token，避免复制链接泄露
      const url = new URL(location.href)
      url.searchParams.delete('token')
      history.replaceState(null, '', url.toString())
    }
    return sessionStorage.getItem(TOKEN_KEY) || ''
  } catch {
    return ''
  }
}

export function setToken(token: string): void {
  try {
    if (token) sessionStorage.setItem(TOKEN_KEY, token)
    else sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 忽略隐私模式下的存储异常 */
  }
}

/**
 * 为本项目请求构造完整 URL（含 SSE 用的 `?token=`）。
 *
 * ★ B3 直连形态的兼容点：`API_BASE` 可能是**绝对地址**
 * （`http://127.0.0.1:8720/api`）。此时必须用 `new URL(...)` 组装，
 * 否则 `URLSearchParams` 的查询串会被拼在路径后而丢失 host 语义；
 * 相对形态（`/api`）同样走 `new URL(p, base)`，两者结果一致。
 *
 * ★ base 取 `location.href`（**不是** `location.origin`）：
 *   `origin` 在部分环境/测试桩下可能缺失，`new URL(x, undefined)` 会抛
 *   `TypeError: Invalid URL`；`href` 是 `Location` 的必选字段，语义等价且更稳健。
 *   （实测：改用 origin 后，两个 DOM 桩自检脚本当场 33 项红灯。）
 */
export function buildUrl(path: string, params?: Record<string, string>): string {
  const p = path.startsWith('/') ? path : '/' + path
  const raw = API_BASE + p
  // 绝对地址：直接解析（直连后端，需后端开 CORS）
  // 相对地址：以当前页面为 base 解析（Vite proxy / 同源托管）
  const u = /^https?:\/\//i.test(raw)
    ? new URL(raw)
    : new URL(raw, location.href)
  const token = getToken()
  if (token) u.searchParams.set('token', token)
  for (const [k, v] of Object.entries(params || {})) u.searchParams.set(k, v)
  return u.toString()
}

/**
 * 构造鉴权头（无 token 时不含 Authorization，保持与改造前一致）。
 *
 * 2.2：一并注入 `X-Session-Id`（会话标识）。后端据此做快照 / 审批 / 取消的归属隔离；
 * 不传则后端落到 `{user}:{default}` 桶（等价单会话旧行为）。
 */
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const h: Record<string, string> = { ...(extra || {}) }
  const token = getToken()
  if (token) h['Authorization'] = 'Bearer ' + token
  h['X-Session-Id'] = getSessionId()
  return h
}

/** 归一后的请求错误 */
export class ApiRequestError extends Error {
  status: number
  body: unknown
  constructor(status: number, message: string, body?: unknown) {
    super(message)
    this.name = 'ApiRequestError'
    this.status = status
    this.body = body
  }
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
  /** JSON 请求体（自动序列化 + Content-Type） */
  body?: unknown
  /** 额外查询参数 */
  params?: Record<string, string>
  /** 中止信号（send 的"停止"按钮） */
  signal?: AbortSignal
  /** 额外请求头 */
  headers?: Record<string, string>
}

/**
 * 核心请求方法：返回解析后的 JSON。
 * 失败时抛 ApiRequestError；401 额外触发 onUnauthorized。
 */
export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, params, signal, headers } = opts

  const init: RequestInit = {
    method,
    signal,
    headers: authHeaders(
      body !== undefined ? { 'Content-Type': 'application/json', ...(headers || {}) } : headers,
    ),
  }
  if (body !== undefined) init.body = JSON.stringify(body)

  const res = await fetch(buildUrl(path, params), init)

  if (res.status === 401) {
    onUnauthorized?.()
    throw new ApiRequestError(401, '未授权：缺少或无效的 token', await safeJson(res))
  }

  if (!res.ok) {
    const payload = await safeJson(res)
    const msg =
      (payload as ApiError | null)?.error || (payload as ApiError | null)?.detail || 'HTTP ' + res.status
    throw new ApiRequestError(res.status, msg, payload)
  }

  return (await res.json()) as T
}

/** GET 语法糖 */
export function get<T>(path: string, params?: Record<string, string>, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: 'GET', params, signal })
}

/** POST 语法糖 */
export function post<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: 'POST', body, signal })
}

/** 安全 JSON 解析：非 JSON 响应不抛异常，返回 null */
async function safeJson(res: Response): Promise<unknown> {
  try {
    const text = await res.text()
    return text ? JSON.parse(text) : null
  } catch {
    return null
  }
}
