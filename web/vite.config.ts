import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';

const schemas = JSON.parse(readFileSync(new URL('../dirmap/schemas.json', import.meta.url), 'utf8'));
const apiOrigin = process.env.DIRMAP_API_ORIGIN || 'http://127.0.0.1:8765';

export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  define: { __DIRMAP_ROUTING_SCHEMA__: JSON.stringify(schemas.routing), __DIRMAP_TERRAIN_SCHEMA__: JSON.stringify(schemas.terrain) },
  plugins: [{
    name: 'versioned-offline-shell', apply: 'build', enforce: 'post',
    generateBundle(_options, bundle) {
      const source = readFileSync(new URL('./sw.js', import.meta.url), 'utf8');
      const hash = createHash('sha256').update(source);
      for (const name of Object.keys(bundle).sort()) {
        const asset = bundle[name];
        hash.update(name).update(asset.type === 'chunk' ? asset.code : asset.source);
      }
      this.emitFile({ type: 'asset', fileName: 'sw.js',
        source: source.replace('__DIRMAP_BUILD_ID__', hash.digest('hex').slice(0, 16)) });
    },
  }],
  server: { host: '0.0.0.0', port: 5173, proxy: {
    '/api': { target: apiOrigin, changeOrigin: false },
    '/assets': { target: apiOrigin, changeOrigin: false },
  } },
  build: { outDir: 'dist', emptyOutDir: true, target: 'es2022' },
});
