<template>
  <div class="panel">
    <div
      class="rowline"
      style="display: flex; gap: 10px; align-items: center; margin-bottom: 12px"
    >
      <input
        v-model="name"
        placeholder="插件名（如 hello_probe）"
        style="flex: 1; min-width: 160px"
      />
      <input v-model="provides" placeholder="provides（逗号分隔，留空默认 svc）" style="flex: 1" />
      <button class="btn ghost" @click="scaffold">新增插件</button>
    </div>
    <!-- 表格内容由纯函数 renderPluginsHtml 生成，保证与 legacy 逐字符一致 -->
    <div ref="host" v-html="html" @click="onClick"></div>
  </div>
</template>

<script setup lang="ts">
/**
 * PluginsView（B2.3）
 * ------------------------------------------------------------------
 * 渲染委托给 `renderPluginsHtml`（纯函数，已逐字符对拍）；
 * 本组件只负责：数据加载、事件委托、启停/移除/脚手架交互。
 * 交互语义与 legacy app.js:447-468 一致。
 */
import { ref, onMounted, computed } from 'vue'
import {
  renderPluginsHtml,
  confirmPluginAction,
  pluginActionPayload,
} from './plugins.logic'
import { loadResource, pluginAction, getStatus } from '../api/endpoints'
import type { PluginRow, PluginsResponse, PluginAction } from '../api/types'

const PLUGIN_BUSY_MSG = '处理中…'

const rows = ref<PluginRow[]>([])
const name = ref('')
const provides = ref('')
const busy = ref(false)
const host = ref<HTMLElement | null>(null)

const html = computed(() => renderPluginsHtml(rows.value))

async function reload(): Promise<void> {
  const j = await loadResource<PluginsResponse>('plugins')
  rows.value = j.plugins || []
}

async function act(payload: { act: PluginAction; body: Record<string, unknown> | null }): Promise<void> {
  if (busy.value) return
  busy.value = true
  // 立即禁用所有插件按钮并显示「处理中…」，消除"点击后没反应"的观感（重建容器需数秒）
  host.value
    ?.querySelectorAll<HTMLButtonElement>('[data-plug-act]')
    .forEach((b) => {
      b.disabled = true
      b.dataset.lb = b.textContent || ''
      b.textContent = PLUGIN_BUSY_MSG
    })
  try {
    const j = await pluginAction(payload)
    if (j.plugins) rows.value = j.plugins as PluginRow[]
    else await reload()
    if (!j.ok || j.rebuild_error) {
      alert('注意：' + (j.rebuild_error || j.error || '') + ' （期望态已保存，实际装配可能需重启）')
    }
    // 后台重建完成后再刷新一次，使状态从「启用中（重启生效）」更新为「已启用」
    setTimeout(() => {
      reload()
      getStatus().catch(() => {})
    }, 4000)
  } catch (e) {
    alert('请求失败：' + (e as Error).message)
  } finally {
    busy.value = false
    getStatus().catch(() => {})
  }
}

function onClick(e: MouseEvent): void {
  const b = (e.target as HTMLElement).closest<HTMLElement>('[data-plug-act]')
  if (!b) return
  const actName = b.dataset.plugAct || ''
  const key = b.dataset.key || ''
  if (!confirmPluginAction(actName, key, b.dataset.enable)) return
  // legacy app.js:465-467 — enable→{id,enable:bool}；remove→{id}
  act(pluginActionPayload(actName, key, b.dataset.enable))
}

function scaffold(): void {
  const n = name.value.trim()
  if (!n) {
    alert('填插件名')
    return
  }
  const p = provides.value
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
  // legacy app.js:466-467 — 脚手架走 act:'scaffold'，body 含 name/provides
  act({ act: 'scaffold', body: { name: n, provides: p.length ? p : null } })
  name.value = ''
  provides.value = ''
}

onMounted(reload)

defineExpose({ reload })
</script>
