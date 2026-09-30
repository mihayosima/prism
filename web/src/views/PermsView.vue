<template>
  <HtmlPanel :html="html" @click="onClick" />
</template>

<script setup lang="ts">
/**
 * PermsView（B2.4）
 * ------------------------------------------------------------------
 * 渲染委托 renderPermsHtml（纯函数，已逐字符对拍）；
 * 本组件负责加载 + 会话档位切换（legacy app.js:384-397, 611-617）。
 * 档位按钮的 chip 选中态由 legacy 的 setModeUI 维护，此处按 legacy 语义
 * 在切换成功后整页 reload（mode 由服务端权威返回）。
 */
import { ref, onMounted, computed } from 'vue'
import HtmlPanel from '../components/HtmlPanel.vue'
import { renderPermsHtml, type PermsData } from './tables.logic'
import { loadResource, setApprovalMode } from '../api/endpoints'
import type { ApprovalMode } from '../api/types'

const data = ref<PermsData>({})
const html = computed(() => renderPermsHtml(data.value))

async function reload(): Promise<void> {
  const j = await loadResource<PermsData>('perms')
  data.value = j as PermsData
}

async function onClick(e: MouseEvent): Promise<void> {
  const b = (e.target as HTMLElement).closest<HTMLElement>('#modeBar [data-mode]')
  if (!b) return
  const m = b.dataset.mode as ApprovalMode
  // legacy app.js:613 — bypass 需二次确认（确认后才带 confirm:true）
  let confirmFlag = false
  if (m === 'bypass') {
    if (!confirm('bypass 将跳过所有审批确认（高危），确定切换？')) return
    confirmFlag = true
  }
  try {
    const j = await setApprovalMode(m, confirmFlag)
    if (!j.ok) {
      alert('切换失败：' + (j.error || ''))
      return
    }
    await reload()
  } catch (err) {
    alert('请求失败：' + (err as Error).message)
  }
}

onMounted(reload)

defineExpose({ reload })
</script>
