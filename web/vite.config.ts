import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 前后端分离（方案 B）开发配置：
// - 前端 dev server 默认 5173（本机 5173 被占用时用 --port 5174）；
// - /api 与 SSE 走 proxy 到后端 8720 → **dev 期同源，免 CORS**。这是**默认形态**。
//
// B3 补的两种部署形态（与 src/api/client.ts 的 API_BASE 开关配套）：
//   · 直连分离：`.env.local` 设 VITE_API_BASE=http://127.0.0.1:8720/api，
//     此时**绕过本 proxy**，改由后端 `--cors-origin http://127.0.0.1:5174` 放行跨域；
//   · B4 单端口：后端 StaticFiles 托管 dist/，与 /api 同源，proxy 不参与。
// 详见 web/.env.example。
export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8720',
        changeOrigin: true,
        // ★ 语音输入的 WS（/api/asr/ws）在 dev 期必须靠这个透传。
        //   缺它 ⇒ WS 握手被 proxy 当普通 HTTP 处理、前端拿不到 upgrade ⇒ 静默降级。
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
