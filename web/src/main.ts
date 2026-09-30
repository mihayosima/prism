/**
 * 应用入口（B2.6：切换到 Vue 根组件）
 * ------------------------------------------------------------------
 * B1 阶段用 `await import('./legacy/app.js')` 过渡；B2.6 起改为挂载 App.vue。
 *
 * 历史说明：`legacy/`（原 `app.js` + `_PAGE.original.html`）已于 B2.6 第二步删除，
 * 迁移期对拍基准移至 `web/_backup_legacy/`（不入版本库，仅本地留档）。
 * 如需回退到 B1 的纯静态实现，从该备份恢复即可。
 *
 * 时间：2026-09-30 12:26 (GMT+8)
 */
import { createApp } from 'vue'
import './styles/app.css'
import App from './App.vue'
import * as apiClient from './api/client'

createApp(App).mount('#app')

/**
 * 自检挂钩（仅当构建期 define `__PRISM_TEST__` 为 true 时生效）。
 *
 * 目的：让 `tools/check-direct-connect.mjs` 能**真正走到** `client.ts` 的
 * 请求组装 + 401 拦截路径，而不是在测脚本里"复刻"一遍（复刻 = 假绿）。
 * 生产构建（`npm run build`）不设该标志 ⇒ 整个 if 为假，摇树可移除。
 *
 * 注意：用**静态** import（而非动态）——自检构建是 `lib + iife` 单文件，
 * 动态 import 会要求代码分割而构建失败。
 */
declare const __PRISM_TEST__: boolean | undefined
if (typeof __PRISM_TEST__ !== 'undefined' && __PRISM_TEST__) {
  ;(globalThis as unknown as Record<string, unknown>).__prismApi = apiClient
}
