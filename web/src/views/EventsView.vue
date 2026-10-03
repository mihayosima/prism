<template>
  <div class="panel">
    <div class="filters" id="evFilters" @click="onFilterClick">
      <button
        v-for="f in FILTERS"
        :key="f.value"
        class="chip"
        :class="{ active: evFilter === f.value }"
        :data-ev="f.value"
      >
        {{ f.label }}
      </button>
    </div>
    <HtmlPanel :html="html" />
  </div>
</template>

<script setup lang="ts">
/**
 * EventsView（B2.4）
 * ------------------------------------------------------------------
 * 渲染委托 renderEventsHtml（纯函数，已逐字符对拍）；
 * 本组件负责数据加载与过滤器切换（legacy app.js:381-383, 600-603）。
 */
import { ref, onMounted, computed } from 'vue'
import HtmlPanel from '../components/HtmlPanel.vue'
import { renderEventsHtml } from './tables.logic'
import { loadResource } from '../api/endpoints'
import type { Row, EventsResponse } from '../api/types'

/** 过滤器标签（legacy index.html 的 #evFilters 按钮，原样） */
const FILTERS = [
  { value: 'all', label: '全部' },
  { value: 'agent/permission/decision', label: '权限' },
  // 外部工具**隔离档位自述**（plugins.sandbox）：**另立事件**（2026-10-03 由
  // `agent/sandbox/decision` 改名为 `agent/sandbox/confinement` —— 沙箱已不再做准入裁决，
  // 名字里留着 "decision" 会让人误判）。用前缀 `agent/sandbox` 匹配，两名都覆盖。
  { value: 'agent/sandbox', label: '隔离' },
  { value: 'agent/chat', label: '会话' },
  { value: 'agent/turn', label: '运行' },
] as const

const rows = ref<Row[]>([])
const evFilter = ref<string>('all')

const html = computed(() => renderEventsHtml(rows.value, evFilter.value))

async function reload(): Promise<void> {
  const j = await loadResource<EventsResponse>('events')
  rows.value = (j.rows || j.events || j.items || []) as Row[]
}

function onFilterClick(e: MouseEvent): void {
  const t = (e.target as HTMLElement).closest<HTMLElement>('.chip')
  if (!t) return
  evFilter.value = t.dataset.ev || 'all'
}

onMounted(reload)

defineExpose({ reload })
</script>
