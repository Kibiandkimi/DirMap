import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser', fullyParallel: false, workers: 1, timeout: 30000,
  outputDir: '/tmp/dirmap-browser-results', reporter: [['list']],
  use: { baseURL: 'http://127.0.0.1:5174', headless: true, viewport: { width: 1440, height: 980 },
    launchOptions: { executablePath: process.env.DIRMAP_BROWSER_PATH || undefined },
  },
  webServer: [{ command: 'DIRMAP_API_ORIGIN=http://127.0.0.1:8768 npm run dev -- --host 127.0.0.1 --port 5174 --strictPort',
    url: 'http://127.0.0.1:5174', reuseExistingServer: false },
    { command: 'python3 -B tools/browser_server.py', url: 'http://127.0.0.1:8768/api/health', reuseExistingServer: false }],
});
