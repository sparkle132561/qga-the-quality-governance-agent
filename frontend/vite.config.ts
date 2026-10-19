import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({mode}) => {
const apiTarget = loadEnv(mode, '.', 'QUALITY_').QUALITY_API_TARGET || 'http://127.0.0.1:8000'
if (!['http://127.0.0.1:8000', 'http://127.0.0.1:8050'].includes(apiTarget)) throw new Error('仅允许本机开发或协议演示后端')

return {
  plugins: [vue()],
  server: { proxy: { '/api': apiTarget } },
}
})
