import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// Dev proxy to coloom-server (default port 4444); in prod FastAPI serves web/dist/.
const backend = process.env.COLOOM_SERVER ?? 'http://localhost:4444'

export default defineConfig({
  plugins: [svelte()],
  server: {
    proxy: {
      '/weaves': backend,
      '/events': backend,
      '/presets': backend,
      '/setups': backend,
      '/profiles': backend,
      '/ws': { target: backend, ws: true },
    },
  },
})
