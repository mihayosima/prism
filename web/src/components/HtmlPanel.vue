<template>
  <div class="panel" ref="host" v-html="html" @click="emit('click', $event)"></div>
</template>

<script setup lang="ts">
/**
 * HtmlPanel（B2.4）
 * ------------------------------------------------------------------
 * 通用「纯函数 HTML 挂载面板」：把 v-html 与 ref 暴露封装成一处，
 * 供 Events / Perms / Memory / Tools 四个表格类视图复用。
 * 不承载业务逻辑——HTML 由各视图的纯函数生成。
 */
import { ref } from 'vue'

defineProps<{ html: string }>()
const emit = defineEmits<{ click: [MouseEvent] }>()
const host = ref<HTMLElement | null>(null)

/** 供父组件读取面板内元素（事件委托/状态同步用） */
function query<T extends Element>(sel: string): T | null {
  return host.value?.querySelector<T>(sel) ?? null
}
function queryAll<T extends Element>(sel: string): T[] {
  return host.value ? Array.from(host.value.querySelectorAll<T>(sel)) : []
}

defineExpose({ query, queryAll, host })
</script>
