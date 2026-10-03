<template>
  <div class="panel" ref="host" v-html="html" @click="onClick" @change="onChange"></div>
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
import {
  loadResource,
  saveSettings as apiSave,
  resetSettings as apiReset,
  resetSessionSettings as apiResetSession,
} from '../api/endpoints'
import type { SettingsResponse, SettingsScope } from '../api/types'

const settings = ref<SettingsResponse>({})
const host = ref<HTMLElement | null>(null)
/** G2 写入作用域：container（默认，全局）/ session（仅本会话） */
const scope = ref<SettingsScope>('container')
const html = computed(() => renderSettingsHtml(settings.value, scope.value))

/** 会话作用域下，仅允许提交「可会话化」的键（否则后端 400）。 */
function allow(k: string): boolean {
  if (scope.value !== 'session') return true
  return ((settings.value.scopable || []) as string[]).includes(k)
}

/** 从 DOM 收集 patch（legacy app.js:550-561，逐项等价；G2：按作用域过滤） */
function collectPatch(): Record<string, unknown> {
  const patch: Record<string, unknown> = {}
  for (const k of SETTINGS_NUMERIC_KEYS) {
    if (!allow(k)) continue
    const el = host.value?.querySelector<HTMLInputElement>('#set_' + k)
    if (el && el.value !== '') {
      patch[k] = isFloatKey(k) ? parseFloat(el.value) : parseInt(el.value, 10)
    }
  }
  const cc = host.value?.querySelector<HTMLInputElement>('#set_cost_calibration')
  if (cc && allow('cost_calibration')) patch.cost_calibration = cc.checked // F1 开关
  const lt = host.value?.querySelector<HTMLInputElement>('#set_lazy_tools')
  if (lt && allow('lazy_tools')) patch.lazy_tools = lt.checked // L1 开关
  const al = host.value?.querySelector<HTMLInputElement>('#set_alignment_enabled')
  if (al && allow('alignment_enabled')) patch.alignment_enabled = al.checked // P0-2 续接前缀对齐
  const tde = host.value?.querySelector<HTMLInputElement>('#set_tool_domains_enabled')
  if (tde && allow('tool_domains_enabled')) patch.tool_domains_enabled = tde.checked // G 域级裁剪总开关
  // G 放行域白名单：多选勾选态。仅在控件确实渲染时提交 —— 域清单为空
  // （后端未返回 domains）时若提交 []，会把用户已有配置静默清空。
  const tdcs = host.value?.querySelectorAll<HTMLInputElement>('.set_tool_domain_cb')
  if (tdcs && tdcs.length > 0 && allow('tool_domains')) {
    patch.tool_domains = Array.from(tdcs)
      .filter((el) => el.checked)
      .map((el) => el.value)
  }
  const cmx = host.value?.querySelector<HTMLSelectElement>('#set_compact_mode')
  if (cmx && allow('compact_mode')) patch.compact_mode = cmx.value // P2 治理模式（枚举）
  const pv = host.value?.querySelector<HTMLSelectElement>('#set_provider')
  if (pv && allow('provider')) patch.provider = pv.value // 审批插件三项
  const tv = host.value?.querySelector<HTMLInputElement>('#set_timeout_s')
  if (tv && tv.value !== '' && allow('timeout_s')) patch.timeout_s = parseInt(tv.value, 10)
  const av = host.value?.querySelector<HTMLInputElement>('#set_ask')
  if (av && allow('ask'))
    patch.ask = av.value
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
  // 准入名单三项（逗号分隔 → 字符串数组；后端 list 分支会再规整一遍）。
  // ★ 2026-10-03 收口：这三项 + default 的归属从 plugins.sandbox 改到 **plugins.guard**
  // （sys/guard.py 是策略唯一持有者，也是 LLM_PRE_CALL 的唯一挂载者）。
  // 注意：只提交**控件确实渲染**的键 —— 对应插件未启用时整段不渲染，若此时提交空数组
  // 会把用户已有的配置静默清空（与 tool_domains 同款陷阱，故此处逐个判存在）。
  for (const k of ['targets', 'allow', 'deny']) {
    const el = host.value?.querySelector<HTMLInputElement>('#set_' + k)
    if (el && allow(k))
      patch[k] = el.value
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean)
  }
  const sdv = host.value?.querySelector<HTMLSelectElement>('#set_default')
  if (sdv && allow('default')) patch.default = sdv.value
  // 沙箱**隔离档位**（2026-10-03 B1：沙箱只做 confinement，不再判准入）。
  // 同样只在控件渲染时提交 —— 未启用时提交会清掉已有档位。
  const smv = host.value?.querySelector<HTMLSelectElement>('#set_mode')
  if (smv && allow('mode')) patch.mode = smv.value
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
    const r = await apiSave(collectPatch(), scope.value)
    if (!r.ok) {
      setMsg('失败：' + (r.error || ''))
      return
    }
    setMsg(scope.value === 'session' ? '已保存到本会话，立即生效 ✓' : '已保存并重建生效 ✓')
    settings.value = (r.settings || {}) as SettingsResponse
  } catch (e) {
    setMsg('失败：' + (e as Error).message)
  }
}

async function doReset(): Promise<void> {
  if (!confirm('恢复默认参数？当前**容器级**覆盖（成本护栏 / 上下文管理 / 审批插件）将被清除。')) return
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

/** G2：「重置为继承」——只清**本会话**覆盖，逐键回落容器/默认（不重建容器） */
async function doResetSession(): Promise<void> {
  if (!confirm('清除**本会话**的全部参数覆盖？各参数将逐键回落到容器级/默认值。')) return
  setMsg('清除中…')
  try {
    const r = await apiResetSession()
    if (!r.ok) {
      setMsg('失败：' + (r.error || ''))
      return
    }
    setMsg('本会话覆盖已清除，参数回落继承 ✓')
    settings.value = (r.settings || {}) as SettingsResponse
  } catch (e) {
    setMsg('失败：' + (e as Error).message)
  }
}

/** G 放行域下拉：把已选项同步到触发条文案（点选即刻反馈，不必等保存） */
function syncDomainLabel(): void {
  const box = host.value?.querySelector<HTMLElement>('#set_tool_domains')
  const lab = host.value?.querySelector<HTMLElement>('#set_tool_domains_label')
  if (!box || !lab) return
  const picked = Array.from(box.querySelectorAll<HTMLInputElement>('.set_tool_domain_cb'))
    .filter((el) => el.checked)
    .map((el) => el.value)
  lab.textContent = picked.length
    ? picked.length <= 2
      ? picked.join('、')
      : `已选 ${picked.length} 个域`
    : '未选择（不裁剪）'
}

/** G 放行域下拉：展开/收起浮层（不传 force 即取反） */
function toggleDomainPanel(force?: boolean): void {
  const box = host.value?.querySelector<HTMLElement>('#set_tool_domains')
  const btn = host.value?.querySelector<HTMLElement>('#set_tool_domains_btn')
  if (!box || !btn) return
  const open = force === undefined ? box.hasAttribute('hidden') : force
  if (open) {
    syncDomainLabel()
    box.removeAttribute('hidden')
  } else {
    box.setAttribute('hidden', '')
  }
  btn.setAttribute('aria-expanded', open ? 'true' : 'false')
}

/** 下拉面板内的勾选变化 ⇒ 同步触发条文案；作用域切换 ⇒ 换数据源重新渲染 */
function onChange(e: Event): void {
  const t = e.target as HTMLElement
  if (!t) return
  if ((t as HTMLSelectElement).id === 'set_scope') {
    const next = (t as HTMLSelectElement).value
    if (next === 'session' || next === 'container') scope.value = next
    return
  }
  if (typeof t.closest === 'function' && t.closest('#set_tool_domains')) syncDomainLabel()
}

function onClick(e: MouseEvent): void {
  const t = e.target as HTMLElement
  if (!t || typeof t.closest !== 'function') return
  if (t.closest('#saveSettings')) doSave()
  else if (t.closest('#resetSettings')) doReset()
  else if (t.closest('#resetSessionSettings')) doResetSession()
  else if (t.closest('#set_tool_domains_btn')) toggleDomainPanel()
  else if (!t.closest('#set_tool_domains')) toggleDomainPanel(false) // 点击面板外 ⇒ 收起
}

onMounted(reload)

defineExpose({ reload })
</script>
