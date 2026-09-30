<template>
  <div class="panel" ref="host" v-html="html" @click="onClick"></div>
</template>

<script setup lang="ts">
/**
 * SettingsView（B2.3）
 * ------------------------------------------------------------------
 * 表单 HTML 由 `renderSettingsHtml`（纯函数，已 10 种分支逐字符对拍）生成；
 * 本组件负责：加载、保存/恢复默认两条交互，以及保存前的字段收集。
 * 交互语义与 legacy app.js:550-575 一致（含数值/浮点/布尔/枚举四类收集）。
 */
import { ref, onMounted, computed } from 'vue'
import { renderSettingsHtml, SETTINGS_NUMERIC_KEYS, isFloatKey } from './settings.logic'
import { loadResource, saveSettings as apiSave, resetSettings as apiReset } from '../api/endpoints'
import type { SettingsResponse } from '../api/types'

const settings = ref<SettingsResponse>({})
const host = ref<HTMLElement | null>(null)
const html = computed(() => renderSettingsHtml(settings.value))

/** 从 DOM 收集 patch（legacy app.js:550-561，逐项等价） */
function collectPatch(): Record<string, unknown> {
  const patch: Record<string, unknown> = {}
  for (const k of SETTINGS_NUMERIC_KEYS) {
    const el = host.value?.querySelector<HTMLInputElement>('#set_' + k)
    if (el && el.value !== '') {
      patch[k] = isFloatKey(k) ? parseFloat(el.value) : parseInt(el.value, 10)
    }
  }
  const cc = host.value?.querySelector<HTMLInputElement>('#set_cost_calibration')
  if (cc) patch.cost_calibration = cc.checked // F1 开关
  const lt = host.value?.querySelector<HTMLInputElement>('#set_lazy_tools')
  if (lt) patch.lazy_tools = lt.checked // L1 开关
  const al = host.value?.querySelector<HTMLInputElement>('#set_alignment_enabled')
  if (al) patch.alignment_enabled = al.checked // P0-2 续接前缀对齐
  const cmx = host.value?.querySelector<HTMLSelectElement>('#set_compact_mode')
  if (cmx) patch.compact_mode = cmx.value // P2 治理模式（枚举）
  const pv = host.value?.querySelector<HTMLSelectElement>('#set_provider')
  if (pv) patch.provider = pv.value // 审批插件三项
  const tv = host.value?.querySelector<HTMLInputElement>('#set_timeout_s')
  if (tv && tv.value !== '') patch.timeout_s = parseInt(tv.value, 10)
  const av = host.value?.querySelector<HTMLInputElement>('#set_ask')
  if (av)
    patch.ask = av.value
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
  return patch
}

function setMsg(text: string): void {
  const el = host.value?.querySelector<HTMLElement>('#setMsg')
  if (el) el.textContent = text
}

async function reload(): Promise<void> {
  const j = await loadResource<SettingsResponse>('settings')
  settings.value = (j.settings || j) as SettingsResponse
}

async function doSave(): Promise<void> {
  setMsg('保存中…')
  try {
    const r = await apiSave(collectPatch())
    if (!r.ok) {
      setMsg('失败：' + (r.error || ''))
      return
    }
    setMsg('已保存并重建生效 ✓')
    settings.value = (r.settings || {}) as SettingsResponse
  } catch (e) {
    setMsg('失败：' + (e as Error).message)
  }
}

async function doReset(): Promise<void> {
  if (!confirm('恢复默认参数？当前覆盖（成本护栏 / 上下文管理 / 审批插件）将被清除。')) return
  setMsg('恢复中…')
  try {
    const r = await apiReset()
    if (!r.ok) {
      setMsg('失败：' + (r.error || ''))
      return
    }
    setMsg('已恢复默认 ✓')
    settings.value = (r.settings || {}) as SettingsResponse
  } catch (e) {
    setMsg('失败：' + (e as Error).message)
  }
}

function onClick(e: MouseEvent): void {
  const t = e.target as HTMLElement
  if (t.closest('#saveSettings')) doSave()
  else if (t.closest('#resetSettings')) doReset()
}

onMounted(reload)

defineExpose({ reload })
</script>
