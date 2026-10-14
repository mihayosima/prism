<template>
  <div class="asr-file">
    <label class="pick">
      <input type="file" accept="audio/*,.wav,.mp3,.flac,.ogg,.m4a" @change="onPick" />
      <span>{{ busy ? '转写中…' : '选择录音文件转文字' }}</span>
    </label>
    <p v-if="hint" class="hint" :class="{ err: !!error }">{{ hint }}</p>

    <div v-if="result" class="meta">
      <span>{{ result.segments.length }} 段</span>
      <span>·</span>
      <span>{{ formatDuration(result.duration_sec) }}</span>
      <button class="mini" @click="copyAll">复制全文</button>
    </div>

    <ol v-if="result" class="segs">
      <li v-for="s in result.segments" :key="s.index">
        <span class="ts">{{ formatDuration(s.start_sec) }}–{{ formatDuration(s.end_sec) }}</span>
        <span class="tx">{{ s.text || '（无内容）' }}</span>
      </li>
    </ol>
  </div>
</template>

<script setup lang="ts">
/**
 * TranscriptPanel（阶段 2 · 模式 F 长录音转写）
 * ------------------------------------------------------------------
 * 上传录音 → 展示 VAD 分段结果（时间戳 + 文本）。
 *
 * 职责边界：只做「展示 + 复制」。**不做摘要/写入等下游动作**——
 *   那些属插件边界之外（ASR 只负责"音频→文本"）。
 *
 * 诚实性：引擎未就绪时**明确报错**，不显示"未识别到语音"这类
 *   会把故障说成"没说话"的提示（后端 503 与 400 都在此原样呈现）。
 *
 * 时间：2026-10-14 23:06 (GMT+8)
 */
import { ref } from 'vue'
import { transcribeFile } from '../api/asr'
import { formatDuration } from '../api/asr.logic'
import type { AsrFileResponse } from '../api/types'

const emit = defineEmits<{ done: [r: AsrFileResponse] }>()

const busy = ref(false)
const hint = ref('')
const error = ref('')
const result = ref<AsrFileResponse | null>(null)

async function onPick(ev: Event): Promise<void> {
  const input = ev.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  busy.value = true
  hint.value = ''
  error.value = ''
  try {
    const r = await transcribeFile(file)
    result.value = r
    if (r.segments.length === 0) {
      // 空段要**区分**于故障：说明音频里没检测到人声
      hint.value = '未在音频中检测到语音段（可确认录音是否包含人声）'
    }
    emit('done', r)
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
    hint.value = error.value
  } finally {
    busy.value = false
    // 允许重复选择同一文件（否则 input 值不变，不会再触发 change）
    input.value = ''
  }
}

async function copyAll(): Promise<void> {
  if (!result.value) return
  const text = result.value.segments.map((s) => s.text).join('\n')
  try {
    await navigator.clipboard.writeText(text)
    hint.value = '已复制全文到剪贴板'
    error.value = ''
  } catch {
    hint.value = '复制失败（浏览器拒绝剪贴板权限），请手动选择文本'
    error.value = 'clipboard'
  }
}
</script>

<style scoped>
.asr-file {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
}
.pick {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
  color: var(--accent, #0969da);
}
.pick input {
  display: none;
}
.pick span {
  border: 1px solid var(--line, #d0d7de);
  border-radius: 6px;
  padding: 3px 10px;
}
.hint {
  margin: 0;
  color: #6b7280;
  font-size: 12px;
}
.hint.err {
  color: #b62324;
}
.meta {
  display: flex;
  align-items: center;
  gap: 6px;
  color: #6b7280;
  font-size: 12px;
}
.mini {
  border: 1px solid var(--line, #d0d7de);
  background: var(--panel, #fff);
  border-radius: 6px;
  font-size: 12px;
  padding: 1px 8px;
  cursor: pointer;
}
.segs {
  margin: 0;
  padding-left: 18px;
  max-height: 340px;
  overflow: auto;
}
.segs li {
  margin-bottom: 5px;
}
.ts {
  color: #6b7280;
  font-size: 11px;
  margin-right: 6px;
  font-variant-numeric: tabular-nums;
}
</style>
