import { defineConfig } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({
  ...base,
  outputDir: '/tmp/dirmap-browser-static-results',
  use: { ...base.use, baseURL: 'http://127.0.0.1:8768' },
  webServer: [{
    command: 'python3 -B /tmp/dirmap-continue-2743/browser_static_server_head.py',
    url: 'http://127.0.0.1:8768/api/health', reuseExistingServer: false, timeout: 60000,
  }],
});
