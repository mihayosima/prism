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
  // G7 外部工具准入（plugins.sandbox）：**另立事件**，故「权限」视图不收它，
  // 在此单列一个过滤项（filterEvents 用 startsWith 匹配，前缀即可）。
  { value: 'agent/sandbox', label: '沙箱' },
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
