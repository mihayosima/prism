<template>
  <div class="asr-voice">
    <button
      class="mic"
      :class="{ live: listening, busy: loading }"
      :disabled="disabled"
      :title="hint"
      @click="toggle"
    >
      <svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">
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

    <!-- 字幕胶囊：partial 灰（实时改写）、final 深（定稿） -->
    <span v-if="partial || draft" class="capsule" :class="{ final: !partial && !!draft }">
      {{ partial || draft }}
    </span>
    <span v-if="listening" class="hint-line">{{ notice }}</span>
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
const partial = ref('')
const draft = ref('')
const strategy = ref<AsrStrategy>('text')
const notice = ref('')

let stream: ReturnType<typeof openAsrStream> | null = null
let capture: CaptureHandle | null = null
/** Web Speech 的识别实例（降级路径） */
let speech: { stop: () => void; abort: () => void } | null = null

const hint = computed(() => (props.disabled ? '当前不可用' : '点击开始语音输入（再点一次结束）'))
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
  const s = pickStrategy({
    wsSupported: typeof WebSocket !== 'undefined',
    webSpeechSupported,
    engineReady: st.available === true && st.status?.state === 'ready',
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
        draft.value += t
        partial.value = ''
        // ★ 只抛给父组件回填，**不触发发送**
        emit('final', t)
      },
      onError: (msg) => {
        notice.value = strategyNotice('webspeech', msg)
        emit('unavailable', msg)
      },
      onClose: () => {
        if (listening.value) void stopAll()
      },
    })
    stream.ws.onopen = () => stream?.stop.call(null) // 保持连接；stop 由用户再次点击触发
  } catch (e) {
    loading.value = false
    const msg = e instanceof Error ? e.message : String(e)
    // 麦克风被拒 / 非安全上下文 / 无设备 ⇒ 降级 Web Speech
    if (webSpeechSupported) {
      notice.value = strategyNotice('webspeech', msg)
      startWebSpeech()
    } else {
      emit('unavailable', msg)
    }
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
        draft.value += text
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
.asr-voice {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  flex: 0 0 auto;
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
.capsule {
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: #6b7280; /* partial：灰=未定稿 */
  background: rgba(0, 0, 0, 0.04);
  border-radius: 10px;
  padding: 2px 8px;
}
.capsule.final {
  color: #1f2328; /* final：深=定稿 */
}
.hint-line {
  font-size: 11px;
  color: #6b7280;
  max-width: 320px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
</style>
