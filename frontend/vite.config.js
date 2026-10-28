import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  base: '/workbench/',
  server: {
    proxy: Object.fromEntries(
      ['/api', '/health', '/models', '/predict', '/samples', '/experiments']
        .map(path => [path, 'http://127.0.0.1:8765']),
    ),
  },
})
