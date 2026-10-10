import { defineConfig } from '@playwright/test';

// An isolated stack: its own database, API (short lease timings), worker and Vite server.
const E2E_DB = process.env.E2E_DATABASE_URL ?? 'postgresql://annotation:annotation@127.0.0.1:55432/annotation_e2e';
const env = {
  ...process.env, DATABASE_URL: E2E_DB, EDIT_LEASE_SECONDS: '8', EDIT_HEARTBEAT_SECONDS: '2', RENDER_DPI: '100',
  APP_PUBLIC_ORIGIN: 'http://127.0.0.1:5175', WORKER_POLL_SECONDS: '0.5',
};

export default defineConfig({
  testDir: 'e2e',
  timeout: 90_000,
  workers: 1,
  use: {baseURL: 'http://127.0.0.1:5175', viewport: {width: 1440, height: 900}, trace: 'retain-on-failure'},
  webServer: [
    {command: '.venv/bin/python e2e/setup_db.py && .venv/bin/uvicorn apps.api.app.annotation_main:app --host 127.0.0.1 --port 8011', url: 'http://127.0.0.1:8011/api/annotation/health/ready', env, reuseExistingServer: false},
    {command: '.venv/bin/python -m workers.annotation_worker.main', env, reuseExistingServer: false, wait: {stderr: /annotation worker started/}},
    {command: 'npx vite --host 127.0.0.1 --port 5175', url: 'http://127.0.0.1:5175', env: {...env, API_ORIGIN: 'http://127.0.0.1:8011'}, reuseExistingServer: false},
  ],
});
