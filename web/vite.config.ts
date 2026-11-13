// Builds the page into the gateway package (contract 1): index.html plus content-hashed assets/.
// `npm run dev` proxies the WebSocket and the HTTP routes to a gateway on port 8765
// (the real one, or dev/mock-gateway.mjs).
import { svelte } from '@sveltejs/vite-plugin-svelte';
import { defineConfig } from 'vitest/config';

const gateway = process.env.BT_GATEWAY ?? 'http://localhost:8765';

export default defineConfig({
  plugins: [svelte()],
  build: {
    outDir: '../bt_server_gateway/bt_server_gateway/static',
    emptyOutDir: true,
    assetsDir: 'assets',
    target: 'es2022',
  },
  server: {
    proxy: {
      '/ws': { target: gateway.replace(/^http/, 'ws'), ws: true },
      '/upload': gateway,
      '/download': gateway,
      '/download-log': gateway,
    },
  },
  test: {
    include: ['test/**/*.test.ts'],
    environment: 'node',
  },
});
