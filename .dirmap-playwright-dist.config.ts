import { defineConfig } from '@playwright/test';
import base from './playwright.config';

// The standard layout (teacher page from Vite, student pages from web/dist),
// with the API serving a disposable, already-upgraded copy of the workbench.
export default defineConfig({
  ...base,
  outputDir: '/tmp/dirmap-browser-static-results',
  webServer: [(base.webServer as any[])[0], {
    command: 'python3 -B /tmp/dirmap-continue-2743/browser_dist_server.py',
    url: 'http://127.0.0.1:8768/api/health', reuseExistingServer: false, timeout: 60000,
  }],
});
