import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
// Self-host frontend. Served by the coordinator (FastAPI) at `/` in production;
// in dev, Vite proxies `/api` + `/ws` straight to the coordinator process.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  // The single-container coordinator (uvicorn) — REST + agent WS live on the
  // same origin. Override with VITE_COORDINATOR_URL when the dev proxy should
  // hit a coordinator on a different host/port.
  const coordinator = env.VITE_COORDINATOR_URL || 'http://127.0.0.1:8000'

  return {
    // SPA is served from the coordinator root.
    base: '/',
    plugins: [react()],
    server: {
      port: 5173,
      // Loopback only. The dev server proxies /api and /ws straight through to
      // the coordinator, so binding it to 0.0.0.0 hands every host on the local
      // network an unauthenticated path to it — a real exposure on shared or
      // untrusted Wi-Fi. Vite's default is localhost; pass `--host` explicitly
      // for the occasional deliberate "open it on my phone" session.
      proxy: {
        '/api': {
          target: coordinator,
          // MUST stay false: `/api/recorder/connect` derives the agent gateway WS
          // URL from the incoming Host header. `changeOrigin: true` would rewrite
          // Host to the internal upstream, so a host-side agent would be handed an
          // unreachable `ws://<upstream>/...`. Keeping the original Host makes the
          // coordinator return a reachable loopback `ws://localhost:5173/ws/ai-gateway`.
          changeOrigin: false,
          ws: true,
          rewrite: (path) => path,
        },
        // Agent WS ingress + streaming — served by the coordinator itself
        // (in-process fleet dispatcher), same origin as /api.
        '/ws': {
          target: coordinator,
          changeOrigin: false,
          ws: true,
        },
      },
    },
    build: {
      outDir: 'dist',
    // Do NOT ship source maps in production — they expose readable source,
    // comments, and internal structure to anyone hitting the static host.
    // 'hidden' still emits maps for upload to an error tracker but omits the
    // //# sourceMappingURL comment so they are not auto-discoverable.
    sourcemap: false,
    rollupOptions: {
      output: {
        // Single vendor chunk. Splitting the React ecosystem across chunks (react in
        // vendor-react, react-i18next/@headlessui/qrcode.react elsewhere) caused a
        // cross-chunk init-order crash — "Cannot read properties of undefined (reading
        // 'createContext')" — a react-dependent chunk evaluated before React was ready.
        // One node_modules chunk lets the bundler order init correctly (react first).
        manualChunks(id) {
          if (id.includes('node_modules')) return 'vendor';
        },
      },
    },
    },
  }
})
