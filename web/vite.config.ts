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
    // The production server answers /elevation with elevation.html; so does the dev server.
    name: 'elevation-page-route', apply: 'serve',
    configureServer(server) {
      server.middlewares.use((request, _response, next) => {
        const [path, query] = (request.url ?? '').split(/\?(.*)/s);
        if (path.replace(/\/$/, '') === '/elevation') request.url = '/elevation.html' + (query ? '?' + query : '');
        next();
      });
    },
  }, {
    name: 'versioned-offline-shell', apply: 'build', enforce: 'post',
    generateBundle(_options, bundle) {
      const source = readFileSync(new URL('./sw.js', import.meta.url), 'utf8');
      const hash = createHash('sha256').update(source);
      for (const name of Object.keys(bundle).sort()) {
        const asset = bundle[name];
        hash.update(name).update(asset.type === 'chunk' ? asset.code : asset.source);
      }
      this.emitFile({ type: 'asset', fileName: 'sw.js',
        source: source.replace('__DIRMAP_BUILD_ID__', hash.digest('hex').slice(0, 16))
          .replace("'__DIRMAP_ASSETS__'", JSON.stringify(Object.keys(bundle).filter(name => /\.(js|css)$/.test(name)).map(name => '/'+name))) });
    },
  }],
  server: { host: '0.0.0.0', port: 5173, proxy: {
    '/api': { target: apiOrigin, changeOrigin: false },
    '/assets': { target: apiOrigin, changeOrigin: false },
  } },
  build: { outDir: 'dist', emptyOutDir: true, target: 'es2022', rollupOptions: { input: {
    // Separate pages: the course app loads exactly as before, without a
    // dynamic-import CSS preload that a blocked web font could fail.
    index: fileURLToPath(new URL('./index.html', import.meta.url)),
    elevation: fileURLToPath(new URL('./elevation.html', import.meta.url)),
  } } },
});
