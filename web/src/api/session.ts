/**
 * API 契约层 · 会话标识（阶段 2.2 多会话）
 * ------------------------------------------------------------------
 * 后端 2.2 起**不再有进程级全局会话**：会话由请求头 `X-Session-Id` 决定
 * （tenant/user 由服务端配置决定，客户端不可指定——可伪造身份 ⇒ 不下放）。
 *
 * v2（2026-10-02 01:30）：**会话粒度由「浏览器级」改为「标签级」**。
 *   原实现把标识存在 localStorage ⇒ **同源多标签共享同一会话**，与「多标签 = 多会话」
 *   的直觉相悖。现策略（URL 优先，标签存储兜底）：
 *     1. `?sid=<id>` —— 显式、可分享、可恢复（地址栏即身份；把链接复制到别处 = 同一会话）；
 *     2. 标签存储（sessionStorage）—— 每个标签一份，刷新保持，新标签另起一份；
 *     3. 都没有 → 生成新 id，并**回写地址栏**（用户看得见自己在哪个会话，便于分享/恢复）。
 *   ★ 与 `?token=` 的区别：token 读入后立刻从地址栏抹掉（防泄露）；
 *     sid **必须保留**——标签的身份就靠它。
 *
 * 字符集纪律：后端白名单 `[A-Za-z0-9_.-]{1,64}`，**禁止 `:` 与 glob 通配符**
 * （否则前缀扫描会越权命中别人的会话）。URL 传入的值在此**同规则过滤**，
 * 非法即忽略——不抛错，也不把非法值送去后端挨 400。
 *
 * 时间：2026-10-02 01:30 (GMT+8)
 */

/** 标签存储键名（v1 为 localStorage 的同名键，v2 起改用标签存储） */
export const SESSION_KEY = 'prism_session_id'

/** 地址栏参数名 */
export const SESSION_PARAM = 'sid'

/** 与后端 `core/session.py` 同源的白名单 */
const VALID = /^[A-Za-z0-9_.-]{1,64}$/

/** 存储不可用（隐私模式/沙箱）时的进程内兜底，保证同一标签多次调用稳定 */
let memFallback: string | null = null

function genId(): string {
  const rnd = Math.random().toString(36).slice(2, 10)
  return 's-' + Date.now().toString(36) + '-' + rnd
}

function isValid(v: string | null | undefined): boolean {
  return !!v && VALID.test(String(v))
}

function readStore(): string | null {
  try {
    const v = sessionStorage.getItem(SESSION_KEY)
    return isValid(v) ? String(v) : null
  } catch {
    return isValid(memFallback) ? memFallback : null
  }
}

function writeStore(v: string): void {
  memFallback = v
  try {
    sessionStorage.setItem(SESSION_KEY, v)
  } catch {
    /* 忽略存储异常：本次仍在进程内生效 */
  }
}

/** 读地址栏 `?sid=`（非法值视作未携带） */
function readUrl(): string | null {
  try {
    const v = new URLSearchParams(location.search).get(SESSION_PARAM)
    return isValid(v) ? String(v) : null
  } catch {
    return null
  }
}

/** 把当前会话写回地址栏（保留其它参数，如 `?token=`） */
function writeUrl(v: string): void {
  try {
    const u = new URL(location.href)
    u.searchParams.set(SESSION_PARAM, v)
    history.replaceState(null, '', u.toString())
  } catch {
    /* 地址栏不可写（沙箱/桩环境）：不影响会话本身，仅失去可分享性 */
  }
}

/**
 * 读取当前会话标识：**URL > 标签存储 > 新建**。
 *
 * 新建后回写地址栏，使会话可见、可分享、刷新后仍落在同一会话。
 */
export function getSessionId(): string {
  const fromUrl = readUrl()
  if (fromUrl) {
    writeStore(fromUrl)
    return fromUrl
  }
  const cur = readStore()
  if (cur) {
    writeUrl(cur)
    return cur
  }
  const fresh = genId()
  writeStore(fresh)
  writeUrl(fresh)
  return fresh
}

/**
 * 轮换会话标识（「新会话」用）；返回轮换后的新值。
 *
 * 旧会话的数据**仍留在服务端**——本标签只是不再指向它；需要回去时把地址栏
 * `?sid=` 改回旧值即可（这也是本策略相对 v1 的额外收益：v1 轮换后前端无从恢复）。
 */
export function rotateSessionId(): string {
  const fresh = genId()
  writeStore(fresh)
  writeUrl(fresh)
  return fresh
}
