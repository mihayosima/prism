<template>
  <!--
    注意：`active` 类**交由父组件（App.vue）经 :class 注入**，此处不得硬编码，
    否则本视图会永远处于可见态（与其它视图的切换互斥关系被打破）。
    CSS：section[data-view]{display:none} + section[data-view].active{display:flex}
  -->
  <section data-view="chat">
    <div class="chat">
      <div class="messages" ref="msgBox"></div>
      <div class="modebar" id="modebar" :class="{ open: modeOpen }">
        <button
          class="modehd"
          id="modehd"
          type="button"
          :aria-expanded="modeOpen"
          title="点击展开/收起权限档位"
          @click="modeOpen = !modeOpen"
        >
          <span class="mlabel">权限档位</span>
          <span class="modecur" id="modecur" :class="{ danger: mode === 'bypass' }">{{ MODE_SHORT[mode] || mode }}</span>
          <span class="modecv">▾</span>
        </button>
        <div class="modebd" id="modebd">
          <button
            v-for="m in MODE_ORDER"
            :key="m"
            class="chip"
            :data-mode="m"
            :title="MODE_TITLE[m]"
            @click="switchMode(m)"
          >
            {{ MODE_LABEL_SHORT[m] }}
          </button>
          <span class="mlabel" id="modehint">{{ MODE_LABEL[mode] || mode }}</span>
        </div>
      </div>
      <div class="inputrow">
        <textarea
          v-model="input"
          id="txt"
          rows="2"
          placeholder="告诉 Agent 你要做什么……"
          @keydown="onKeydown"
        ></textarea>
        <button class="btn" id="send" :disabled="false" @click="send">{{ sendLabel }}</button>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
/**
 * ChatView（B2.6）
 * ------------------------------------------------------------------
 * 组装流式内核（useStreamMessage）+ 卡片内核（useCards）+ SSE 客户端。
 * 与 legacy 的 send()/handleFrame()/addMsg() 逐行等价（app.js:174-215）。
 *
 * 关键：本组件以「命令式内核 + Vue 壳」方式工作——消息 DOM 由内核直接
 * 生成（保住增量渲染性能特征），Vue 只负责输入框/按钮/档位条的响应式。
 */
import { ref, onMounted, computed, nextTick } from 'vue'
import { createStreamKernel, type StreamKernel } from '../stores/useStreamMessage'
import { createCardKernel, type CardKernel } from '../stores/useCards'
import { messageHtml, stepRowHtml } from '../components/messages'
import { esc } from '../components/markdown'
import { streamChat, getStatus, getSession, resetSession, answerApproval, getApprovalPending, setApprovalMode } from '../api/endpoints'
import { sseFrameName } from '../api/sse'
import type { ApprovalMode } from '../api/types'

// ---------- 档位文案（legacy app.js:576-577 原样）----------
const MODE_ORDER: ApprovalMode[] = ['default', 'plan', 'auto', 'bypass']
const MODE_LABEL: Record<string, string> = {
  default: '默认',
  plan: '规划：模型只读浏览，计划需你批准',
  auto: '审批：非只读工具都要你确认',
  bypass: '跳过审批（高危）',
}
const MODE_SHORT: Record<string, string> = { default: '默认', plan: '规划', auto: '审批', bypass: '跳过' }
const MODE_LABEL_SHORT: Record<string, string> = { default: '默认', plan: '规划', auto: '审批', bypass: '跳过' }
const MODE_TITLE: Record<string, string> = {
  default: '',
  plan: '只读浏览，工具被裁剪，模型只能提交计划等你批',
  auto: '非只读工具都要你审批',
  bypass: '跳过一切审批（高危）',
}

const msgBox = ref<HTMLElement | null>(null)
const input = ref('')
const sendLabel = ref('发送')
const modeOpen = ref(false)
const mode = ref<string>('默认')

// ---------- 内核（在 mount 后绑定容器）----------
let kernel: StreamKernel | null = null
let cards: CardKernel | null = null
let streaming = false
let ctrl: AbortController | null = null

const atBottom = (): boolean => {
  const m = msgBox.value
  if (!m) return true
  return m.scrollHeight - m.scrollTop - m.clientHeight < 48
}
/** 滚动跟随：force=用户主动行为；否则仅贴底时跟随（不把上翻的视图拽回） */
const follow = (force: boolean): void => {
  const m = msgBox.value
  if (!m) return
  if (force || atBottom()) m.scrollTop = m.scrollHeight
}

/** 追加一条静态消息（legacy addMsg，app.js:49-68） */
function addMsg(who: string, text: string, evid: Record<string, unknown> | null): void {
  const m = msgBox.value
  if (!m) return
  const d = document.createElement('div')
  d.className = 'msg ' + who
  d.innerHTML = messageHtml(who, text, evid as never)
  m.appendChild(d)
  follow(true)
}

/** 答复留痕（legacy addUserNote，app.js:219-223） */
function addUserNote(text: string): void {
  if (!text) return
  addMsg('user', text, null)
}

// ---------- SSE 帧分发（legacy handleFrame，app.js:174-185）----------
// 注 1：sseFrameName 收「常量键」（大写），返回帧名（小写）。勿传帧名——虽已做幂等兜底，
//       但统一用常量键能让错配在 tsc/vue-tsc 阶段就暴露。
// 注 2：★ 载荷层次 ★ 后端 progress 桥（agent/sys/progress.py:_pusher）把内核事件载荷包成
//       payload = { source, data: <业务对象> } —— 故一切业务字段都在 **p.data** 里，
//       绝不能直接读 p.call_id / p.tool（那会静默取到 undefined，卡片不弹且不报错）。
function handleFrame(ev: string, p: Record<string, unknown>): void {
  const k = kernel!
  // 统一从 payload.data 取业务对象（legacy 的 p.data 语义）
  const d = (p?.data ?? {}) as Record<string, never>
  if (ev === sseFrameName('PROGRESS')) k.useSeg().addStep(d)
  else if (ev === sseFrameName('THINKING')) k.useSeg().appendThinking((d as { text?: string }).text || '')
  else if (ev === sseFrameName('TOKEN')) k.useSeg().appendToken((d as { text?: string }).text || '')
  else if (ev === sseFrameName('FINAL')) k.useSeg().finish((p.text as string) || '', (p.evidence as never) || null)
  else if (ev === sseFrameName('CANCELLED')) {
    /* 由 useSeg 收尾 */
  } else if (ev === sseFrameName('APPROVAL')) {
    k.sealSeg()
    cards!.showApproval(d)
  } else if (ev === sseFrameName('APPROVAL_DONE')) cards!.dismissApproval(d)
  else if (ev === sseFrameName('ASK_USER')) {
    k.sealSeg()
    cards!.showAskUser(d)
  } else if (ev === sseFrameName('ASK_USER_DONE')) cards!.dismissAskUser(d)
  else if (ev === sseFrameName('ERROR')) k.useSeg().finish('请求失败：' + (p.error || ''), null)
}

// ---------- 发送（legacy send，app.js:186-215）----------
async function send(): Promise<void> {
  if (streaming && ctrl) {
    ctrl.abort()
    return
  }
  const txt = input.value.trim()
  if (!txt) return
  addMsg('user', txt, null)
  input.value = ''
  streaming = true
  ctrl = new AbortController()
  sendLabel.value = '停止'
  kernel!.beginTurnStream()
  try {
    await streamChat({
      message: txt,
      signal: ctrl.signal,
      onFrame: (ev, payload) => handleFrame(ev, payload as Record<string, unknown>),
    })
  } catch (e) {
    if ((e as Error)?.name === 'AbortError') kernel!.useSeg().finish('（已取消）', null)
    else kernel!.useSeg().finish('请求失败：' + ((e as Error)?.message || String(e)), null)
  } finally {
    streaming = false
    ctrl = null
    sendLabel.value = '发送'
    nextTick(() => (document.querySelector('#txt') as HTMLTextAreaElement | null)?.focus())
  }
}

function onKeydown(e: KeyboardEvent): void {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    send()
  }
}

// ---------- 档位切换（legacy switchMode，app.js:586-595）----------
async function switchMode(m: ApprovalMode): Promise<void> {
  try {
    let confirmFlag = false
    if (m === 'bypass') {
      if (!confirm('bypass 将跳过所有审批确认（高危），确定切换？')) return
      confirmFlag = true
    }
    const j = await setApprovalMode(m, confirmFlag)
    if (!j.ok) {
      alert('切换失败：' + (j.error || ''))
      return
    }
    mode.value = MODE_SHORT[j.mode || m] || m
    modeOpen.value = false // 选完自动收起
  } catch (e) {
    alert('请求失败：' + (e as Error).message)
  }
}

// ---------- 会话/状态（legacy loadStatus/loadSession）----------
async function loadStatus(): Promise<void> {
  try {
    const j = await getStatus()
    const modelEl = document.querySelector('#model')
    const sessEl = document.querySelector('#session')
    const machEl = document.querySelector('#machine')
    if (modelEl) modelEl.textContent = j.provider_label + ' · ' + j.model
    if (sessEl) sessEl.textContent = 'session ' + j.session_id
    if (machEl)
      machEl.textContent = 'v' + j.version + '\n预算 ' + (j.budget ? j.budget.spent + '/' + j.budget.max : '-')
  } catch {
    /* 忽略 */
  }
}

async function loadSession(): Promise<void> {
  try {
    const j = (await getSession()) as { history?: Array<{ role?: string; content?: string }> }
    if (msgBox.value) msgBox.value.innerHTML = ''
    ;(j.history || []).forEach((m) => addMsg(m.role === 'user' ? 'user' : 'agent', m.content || '', null))
  } catch {
    /* 忽略 */
  }
}

async function newSession(): Promise<void> {
  await resetSession()
  if (msgBox.value) msgBox.value.innerHTML = ''
  await loadSession()
}

/** 刷新后在途弹窗/问询卡（legacy app.js:620-624） */
async function restorePending(): Promise<void> {
  try {
    const j = await getApprovalPending()
    if (j.mode) mode.value = MODE_SHORT[j.mode] || j.mode
    ;(j.items || []).forEach((it) => {
      const q = it.req || {}
      if (q.kind === 'ask_user') cards!.showAskUser(q)
      else if (q.call_id) cards!.showApproval(q)
    })
  } catch {
    /* 忽略 */
  }
}

onMounted(async () => {
  kernel = createStreamKernel({
    container: () => msgBox.value!,
    follow,
    renderStep: stepRowHtml,
  })
  cards = createCardKernel({
    container: () => msgBox.value!,
    scrollMsg: () => follow(true),
    addUserNote,
    submitAnswer: (body) => answerApproval(body as never),
  })
  await loadStatus()
  await loadSession()
  await restorePending()
})

defineExpose({
  newSession,
  refreshStatus: loadStatus,
  // 供 App 在切回会话页时补一次状态刷新
  refreshSession: loadSession,
})
</script>
