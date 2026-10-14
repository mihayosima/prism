<template>
  <div class="asr-voice">
    <!-- 字幕胶囊**只在实时改写（partial）时出现**：定稿文本已回填输入框，
         这里再挂着就是重复信息（用户明确不要停留）⇒ final 一到即消失 -->
    <span v-if="partial" class="capsule">
      {{ partial }}
    </span>
    <span v-if="listening && notice" class="hint-line">{{ notice }}</span>

    <!-- 点击开始录音，**再次点击停止**（toggle）。录音中图标切换为"停止"方块，
         让"现在处于录音中、点这里结束"一眼可读 -->
    <button
      class="mic"
      :class="{ live: listening, busy: loading }"
      :disabled="disabled"
      :title="hint"
      :aria-pressed="listening"
      @click="toggle"
    >
      <!-- 录音中：停止方块（再点一次结束） -->
      <svg v-if="listening" viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
        <rect x="6.5" y="6.5" width="11" height="11" rx="2" fill="currentColor" />
      </svg>
      <!-- 空闲/加载中：麦克风 -->
      <svg v-else viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
        <path
          d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3z"
          fill="currentColor"
        />
        <path
          d="M18 11a1 1 0 1 0-2 0 4 4 0 0 1-8 0 1 1 0 1 0-2 0 6 6 0 0 0 5 5.9V19H9a1 1 0 1 0 0 2h6a1 1 0 1 0 0-2h-2v-2.1A6 6 0 0 0 18 11z"
          fill="currentColor"
        />
      </svg>
    </button>
  </div>
</template>

<script setup lang="ts">
/**
 * VoiceButton（阶段 2 · 模式 R 实时语音输入）
 * ------------------------------------------------------------------
 * 麦克风按钮 + 实时字幕胶囊。三态：空闲 / 引擎加载中 / 录音中。
 *
 * ★ 硬约束：**定稿文本只通过 `final` 事件交给父组件回填草稿，绝不自动发送**。
 *   语音识别会出错，自动发出去等于替用户说了话——这是产品红线。
 *
 * 降级链（`asr.logic.ts::pickStrategy`）：
 *   本地 WS → 浏览器 Web Speech（提示音频将出本机）→ 纯文本
 *
 * 时间：2026-10-14 23:02 (GMT+8)
 */
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import {
  getAsrStatus,
  openAsrStream,
  startRealtimeCapture,
  type CaptureHandle,
} from '../api/asr'
import {
  isSpeechApiAvailable,
  pickStrategy,
  strategyNotice,
  type AsrStrategy,
} from '../api/asr.logic'

const props = defineProps<{ disabled?: boolean }>()
/** 定稿文本（父组件回填到输入框草稿） */
const emit = defineEmits<{ final: [text: string]; unavailable: [msg: string] }>()

const listening = ref(false)
const loading = ref(false)
const partial = ref('')   // 实时改写（未定稿）；定稿后即清空 ⇒ 胶囊自动消失
const strategy = ref<AsrStrategy>('text')
const notice = ref('')

let stream: ReturnType<typeof openAsrStream> | null = null
let capture: CaptureHandle | null = null
/** Web Speech 的识别实例（降级路径） */
let speech: { stop: () => void; abort: () => void } | null = null

const hint = computed(() => {
  if (props.disabled) return '当前不可用'
  if (listening.value) return '点击停止录音'
  if (loading.value) return '本地引擎加载中…'
  return '点击开始语音输入（再点一次结束）'
})
const webSpeechSupported = isSpeechApiAvailable()

async function toggle(): Promise<void> {
  if (listening.value || loading.value) {
    await stopAll()
    return
  }
  await startAll()
}

/** 只读探测引擎能力，决定走哪条路 */
async function decide(): Promise<{ strategy: AsrStrategy; reason: string }> {
  const st = await getAsrStatus()
  // ★ **idle ≠ 不可用**：引擎是**懒加载**的，后端刚启动（或久未使用）时 `state` 恒为
  //   `idle`，含义只是"还没加载"——连上 WS 就会触发加载并回 `loading → ready`
  //   （前端已有 loading 态承接）。此前只认 `ready`，导致**每次重启后端后第一次点
  //   麦克风必然被判成不可用 ⇒ 直接降级浏览器识别**（实测：status 返回
  //   `available:true / state:'idle'`，却被当成引擎不可用）。
  //   只有 `unavailable`（缺包/缺模型/加载失败）才真的不能走本地通道。
  const state = String(st.status?.state || '')
  const engineReady =
    st.available === true && (state === 'ready' || state === 'idle' || state === 'loading')
  const s = pickStrategy({
    wsSupported: typeof WebSocket !== 'undefined',
    webSpeechSupported,
    engineReady,
  })
  return { strategy: s, reason: st.status?.reason || st.error || '' }
}

async function startAll(): Promise<void> {
  const { strategy: s, reason } = await decide()
  strategy.value = s
  notice.value = strategyNotice(s, reason)
  if (s === 'text') {
    emit('unavailable', notice.value)
    return
  }
  if (s === 'webspeech') {
    startWebSpeech()
    return
  }
  await startLocal()
}

/** 路径 1：本地引擎（WS + 麦克风） */
async function startLocal(): Promise<void> {
  loading.value = true
  try {
    capture = await startRealtimeCapture((pcm) => stream?.send(pcm))
    stream = openAsrStream({
      onLoading: () => {
        loading.value = true
        notice.value = '本地引擎加载中…'
      },
      onReady: () => {
        loading.value = false
        listening.value = true
        notice.value = '正在聆听（本地引擎，音频不出本机）'
      },
      onPartial: (t) => {
        partial.value = t
      },
      onFinal: (t) => {
        // 定稿：清空 partial ⇒ 胶囊消失（文本已在输入框里，不必再挂一份）
        partial.value = ''
        // ★ 只抛给父组件回填，**不触发发送**
        emit('final', t)
      },
      onError: (msg) => {
        // ★ 本地通道失败必须**真降级**：此前只弹提示 + 发 unavailable，既不收尾
        //   listening、也不真的切到 Web Speech ⇒ 按钮显示"录音中"却不出字。
        void degrade(msg)
      },
      onClose: () => {
        // 用户主动停止时 `listening` 已置 false ⇒ 这里只处理**意外关闭**
        // （进程重启 / 网络断开 / 后端插件被停用）；加载中就断连更要收尾，
        // 否则 loading 永远为真，按钮卡在"引擎加载中"。
        if (listening.value || loading.value) void degrade('连接已断开')
      },
    })
    // ★ 这里**绝不能**在 `ws.onopen` 里发 stop（曾误写 `stream?.stop.call(null)`）：
    //   `stop()` 的语义是发送 `{"type":"stop"}` 控制帧让后端收尾 ⇒ onopen 即发
    //   等于"一点开始就立刻结束"，用户看到的就是"按一下说半句"而不是
    //   「点击开始 / 再点停止」。收尾只由用户第二次点击 `toggle → stopAll` 触发。
  } catch (e) {
    loading.value = false
    const msg = e instanceof Error ? e.message : String(e)
    // 麦克风被拒 / 非安全上下文 / 无设备 ⇒ 与 onError 走同一条降级路径
    await degrade(msg)
  }
}

/**
 * 本地通道失败后的**真降级**：释放本地资源 → 切 Web Speech（可用时）→ 否则报不可用。
 *
 * ★ 只被本地路径的 `onError` / 意外 `onClose` 调用；Web Speech 自己的 `onerror`
 *   直接报不可用，**不再调这里** ⇒ 不会形成无限降级循环。
 */
async function degrade(reason: string): Promise<void> {
  listening.value = false
  loading.value = false
  partial.value = ''
  await releaseLocal()
  if (webSpeechSupported) {
    strategy.value = 'webspeech'
    notice.value = strategyNotice('webspeech', reason)
    startWebSpeech()
    return
  }
  strategy.value = 'text'
  notice.value = strategyNotice('text')
  emit('unavailable', reason ? `本地引擎不可用（${reason}），且当前浏览器不支持语音识别` : strategyNotice('text'))
}

/** 释放本地通道资源（WS + 麦克风），不动状态位 */
async function releaseLocal(): Promise<void> {
  try {
    stream?.close()
  } catch {
    /* 已关闭 */
  }
  stream = null
  if (capture) {
    try {
      await capture.stop()
    } catch {
      /* 忽略重复释放 */
    }
    capture = null
  }
}

/** 路径 2：浏览器 Web Speech（音频出本机，notice 已明示） */
function startWebSpeech(): void {
  const w = window as unknown as Record<string, unknown>
  const Ctor = (w.webkitSpeechRecognition || w.SpeechRecognition) as
    | (new () => {
        lang: string
        continuous: boolean
        interimResults: boolean
        onresult: ((e: { resultIndex: number; results: ArrayLike<{ 0: { transcript: string }; isFinal: boolean }> }) => void) | null
        onend: (() => void) | null
        onerror: (() => void) | null
        start: () => void
        stop: () => void
        abort: () => void
      })
    | undefined
  if (!Ctor) {
    emit('unavailable', '当前浏览器不支持语音识别，请用文字输入')
    return
  }
  const rec = new Ctor()
  rec.lang = 'zh-CN'
  rec.continuous = true
  rec.interimResults = true
  rec.onresult = (e) => {
    let interim = ''
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const r = e.results[i]
      const text = r[0].transcript
      if (r.isFinal) {
        emit('final', text)
      } else {
        interim += text
      }
    }
    partial.value = interim
  }
  rec.onend = () => {
    if (listening.value) void stopAll()
  }
  rec.onerror = () => {
    emit('unavailable', '浏览器语音识别出错')
  }
  rec.start()
  speech = rec
  listening.value = true
  notice.value = strategyNotice('webspeech')
}

async function stopAll(): Promise<void> {
  listening.value = false
  loading.value = false
  partial.value = ''
  try {
    stream?.stop()
    stream?.close()
  } catch {
    /* 已关闭 */
  }
  stream = null
  try {
    speech?.stop()
  } catch {
    /* 已停止 */
  }
  speech = null
  if (capture) {
    await capture.stop()
    capture = null
  }
}

onBeforeUnmount(() => {
  void stopAll()
})

// 父组件禁用时（如正在跑一轮对话）立即收尾，避免麦克风一直开着
watch(
  () => props.disabled,
  (v) => {
    if (v && listening.value) void stopAll()
  },
)
</script>

<style scoped>
/* 布局契约：本组件落在 ChatView 的 `.inputrow`（textarea 与发送按钮之间的右侧
   按钮组）。字幕胶囊 / 提示行**悬浮**在按钮上方（absolute，不占 flex 空间），
   向左展开，避免把输入行挤高 */
.asr-voice {
  position: relative;
  display: inline-flex;
  align-items: flex-end;
  flex: 0 0 auto;
  padding-bottom: 7px; /* 与发送按钮（约 43px 高）视觉居中对齐（30px 圆钮） */
}
.mic {
  width: 30px;
  height: 30px;
  border-radius: 50%;
  border: 1px solid var(--line, #d0d7de);
  background: var(--panel, #fff);
  color: var(--fg, #1f2328);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  transition: border-color 0.12s, color 0.12s;
}
.mic:hover:not(:disabled) {
  border-color: var(--brand2, #4c6ef5);
  color: var(--brand-ink, #3b5bdb);
}
.mic:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.mic.live {
  color: #b62324;
  border-color: #b62324;
  animation: pulse 1.2s ease-in-out infinite;
}
.mic.busy {
  color: #9a6700;
  border-color: #d4a72c;
}
@keyframes pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(182, 35, 36, 0.5); }
  50% { box-shadow: 0 0 0 6px rgba(182, 35, 36, 0); }
}
/* 字幕胶囊：partial 灰（实时改写）、final 深（定稿）——悬浮在按钮上方 */
.capsule {
  position: absolute;
  bottom: calc(100% + 6px);
  right: 0;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: #6b7280; /* partial：灰=未定稿 */
  background: var(--panel, #fff);
  border: 1px solid var(--line, #e5e7eb);
  border-radius: 10px;
  padding: 3px 9px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  z-index: 5;
}
.hint-line {
  position: absolute;
  bottom: calc(100% + 6px);
  right: 0;
  font-size: 11px;
  color: #6b7280;
  background: var(--panel, #fff);
  border: 1px solid var(--line, #e5e7eb);
  border-radius: 10px;
  padding: 3px 9px;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  z-index: 5;
}
</style>
