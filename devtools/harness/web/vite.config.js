import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `global` / `process.env` shims: the Amplify liveness streaming client
// expects Node-ish globals in the browser.
export default defineConfig({
  plugins: [react()],
  define: {
    global: 'window',
    'process.env': {},
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8900',
    },
  },
})
