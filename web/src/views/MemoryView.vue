<template>
  <HtmlPanel :html="html" />
</template>

<script setup lang="ts">
/**
 * MemoryView（B2.4）
 * ------------------------------------------------------------------
 * 渲染委托 renderMemoryHtml（纯函数，已逐字符对拍）。
 * 纯展示型视图，无交互；仅负责加载。
 */
import { ref, onMounted, computed } from 'vue'
import HtmlPanel from '../components/HtmlPanel.vue'
import { renderMemoryHtml, type MemoryData } from './tables.logic'
import { loadResource } from '../api/endpoints'

const data = ref<MemoryData>({})
const html = computed(() => renderMemoryHtml(data.value))

async function reload(): Promise<void> {
  const j = await loadResource<MemoryData>('memory')
  data.value = j as MemoryData
}

onMounted(reload)

defineExpose({ reload })
</script>
