<template>
  <div class="top">
    <div class="brand"><div class="mark">◈</div>prism</div>
    <span class="mono" id="session"></span>
    <div class="spacer"></div>
    <span class="pill" id="model">…</span>
    <button class="chip" id="newSession" style="margin-left: 10px" @click="onNewSession">新会话</button>
  </div>
  <div class="wrap">
    <nav class="nav">
      <h6>工作台</h6>
      <button
        v-for="item in NAV_TOP"
        :key="item.view"
        class="nav-item"
        :class="{ active: current === item.view }"
        :data-view="item.view"
        @click="go(item.view)"
      >
        <span class="ic">{{ item.icon }}</span>{{ item.label }}
      </button>
      <h6>机器</h6>
      <button
        v-for="item in NAV_MACHINE"
        :key="item.view"
        class="nav-item"
        :class="{ active: current === item.view }"
        :data-view="item.view"
        @click="go(item.view)"
      >
        <span class="ic">{{ item.icon }}</span>{{ item.label }}
      </button>
      <div class="nav-foot" id="machine">…</div>
    </nav>
    <div class="main">
      <!--
        视图切换：**必须**用 `:class="{active}"` 而非 `v-show`。
        CSS 是 `section[data-view]{display:none}` + `section[data-view].active{display:flex}`
        —— 可见性由**样式表的类**决定。`v-show` 只切内联 display（显示时清空内联样式），
        会回落被 CSS 的 display:none 覆盖，导致除会话页外全部空白。
        这也与 legacy 的 `classList.toggle('active', s.dataset.view===vn)` 逐字对齐。
      -->
      <ChatView :class="{ active: current === 'chat' }" ref="chatRef" />
      <section :class="{ active: current === 'events' }" data-view="events">
        <EventsView ref="views.events" />
      </section>
      <section :class="{ active: current === 'perms' }" data-view="perms">
        <PermsView ref="views.perms" />
      </section>
      <section :class="{ active: current === 'memory' }" data-view="memory">
        <MemoryView ref="views.memory" />
      </section>
      <section :class="{ active: current === 'tools' }" data-view="tools">
        <ToolsView ref="views.tools" />
      </section>
      <section :class="{ active: current === 'settings' }" data-view="settings">
        <SettingsView ref="views.settings" />
      </section>
      <section :class="{ active: current === 'plugins' }" data-view="plugins">
        <PluginsView ref="views.plugins" />
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * App 根组件（B2.6）
 * ------------------------------------------------------------------
 * 承载导航与视图切换（legacy app.js:604-609 的 .nav-item 逻辑）。
 *
 * 与 legacy 的差异（有意为之）：
 *   - **全部 7 个视图常驻挂载**（`v-if` 会卸载组件）：
 *     · 会话页消息 DOM 由内核**命令式**维护，卸载即丢失整段对话；
 *     · 其余视图保持挂载可避免每次切换重新取数（进入时经 reload() 刷新）。
 *   - 可见性用 `:class="{active}"` 表达，与 legacy 的
 *     `classList.toggle('active', s.dataset.view===vn)`（app.js:605）逐字对齐。
 *     **不可改回 `v-show`**——CSS 的 `display:none` 是样式表规则，`v-show` 只切
 *     内联样式，会回落被覆盖 ⇒ 除会话页外全部空白（已踩坑，见方案 C.7）。
 */
import { ref, reactive, onMounted } from 'vue'
import ChatView from './views/ChatView.vue'
import EventsView from './views/EventsView.vue'
import PermsView from './views/PermsView.vue'
import MemoryView from './views/MemoryView.vue'
import ToolsView from './views/ToolsView.vue'
import SettingsView from './views/SettingsView.vue'
import PluginsView from './views/PluginsView.vue'

interface NavItem {
  view: string
  icon: string
  label: string
}

/** 导航分组（与 legacy index.html 的 .nav 结构一致） */
const NAV_TOP: NavItem[] = [{ view: 'chat', icon: '💬', label: '会话' }]
const NAV_MACHINE: NavItem[] = [
  { view: 'events', icon: '📡', label: '事件' },
  { view: 'perms', icon: '🔐', label: '权限' },
  { view: 'plugins', icon: '🔌', label: '插件' },
  { view: 'memory', icon: '🧠', label: '记忆' },
  { view: 'tools', icon: '🛠️', label: '工具' },
  { view: 'settings', icon: '⚙️', label: '设置' },
]

const current = ref<string>('chat')
const chatRef = ref<InstanceType<typeof ChatView> | null>(null)
const views = reactive<Record<string, { reload?: () => void } | null>>({
  events: null,
  perms: null,
  plugins: null,
  memory: null,
  tools: null,
  settings: null,
})

/** legacy app.js:604-609 — 切换视图；非会话页每次进入重新加载（保持数据新鲜） */
function go(v: string): void {
  current.value = v
  if (v !== 'chat') views[v]?.reload?.()
}

function onNewSession(): void {
  chatRef.value?.newSession()
}

onMounted(() => {
  // 首屏：为会话页做一次状态刷新（ChatView 自身已在 onMounted 加载）
})
</script>
