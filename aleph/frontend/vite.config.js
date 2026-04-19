import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

// Aleph frontend served under /aleph/ on the VM (Apache proxies
// /aleph/* → static dist/, /aleph/api/* → FastAPI on 8765).
// Multi-page build: index.html (main app, auth-gated) + login.html (public).
export default defineConfig({
  plugins: [react()],
  base: '/aleph/',
  build: {
    outDir: 'dist',
    sourcemap: false,
    rollupOptions: {
      input: {
        main: resolve(__dirname, 'index.html'),
        login: resolve(__dirname, 'login.html'),
      },
    },
  },
  server: {
    proxy: {
      '/aleph/api': {
        target: 'http://127.0.0.1:8765',
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/aleph\/api/, ''),
      },
    },
  },
});
