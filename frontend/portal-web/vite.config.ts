import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: {
      '/portal': { target: 'http://localhost:8082', changeOrigin: true },
      '/capi': { target: 'http://localhost:9080', changeOrigin: true },
    }
  }
})
