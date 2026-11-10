import { defineConfig } from '@playwright/test';
import base from './playwright.config';

export default defineConfig({
  ...base,
  outputDir: '/tmp/dirmap-browser-static-results',
  use: { ...base.use, baseURL: 'http://127.0.0.1:5174' },
  webServer: [{
    command: 'python3 -B /tmp/dirmap-continue-2743/browser_dual_static.py',
    url: 'http://127.0.0.1:5174/api/health', reuseExistingServer: false, timeout: 60000,
  }],
});
