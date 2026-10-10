import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  root: 'apps/review',
  server: {
    port: 5174,
    strictPort: true,
    proxy: { '/api': process.env.API_ORIGIN ?? 'http://127.0.0.1:8010' },
  },
  build: { outDir: '../../dist/review', emptyOutDir: true },
  test: { environment: 'jsdom', setupFiles: ['./src/test-setup.ts'] },
});
