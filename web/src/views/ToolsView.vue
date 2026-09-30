<template>
  <HtmlPanel :html="html" />
</template>

<script setup lang="ts">
/**
 * ToolsView（B2.4）
 * ------------------------------------------------------------------
 * 渲染委托 renderToolsHtml（纯函数，已逐字符对拍）。
 * 纯展示型视图，无交互；仅负责加载。
 */
import { ref, onMounted, computed } from 'vue'
import HtmlPanel from '../components/HtmlPanel.vue'
import { renderToolsHtml } from './tables.logic'
import { loadResource } from '../api/endpoints'
import type { ToolRow, ToolsResponse } from '../api/types'

const rows = ref<ToolRow[]>([])
const html = computed(() => renderToolsHtml(rows.value))

async function reload(): Promise<void> {
  const j = await loadResource<ToolsResponse>('tools')
  rows.value = j.tools || j.items || []
}

onMounted(reload)

defineExpose({ reload })
</script>
