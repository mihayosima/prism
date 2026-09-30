/**
 * Vue SFC 类型声明（B2.6）
 * ------------------------------------------------------------------
 * 让 `tsc --noEmit` 能解析 .vue 导入（Vite 侧由 @vitejs/plugin-vue 处理）。
 * 时间：2026-09-30 11:50 (GMT+8)
 */
declare module '*.vue' {
  import type { DefineComponent } from 'vue'
  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
