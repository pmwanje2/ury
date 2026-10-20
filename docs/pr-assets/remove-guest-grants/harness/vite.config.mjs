import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// Vite dev server over the real pos/ source of the checkout in HARNESS_REPO
// (default: this checkout). The captured component makes no API call.
const here = dirname(fileURLToPath(import.meta.url));
const repo = process.env.HARNESS_REPO || resolve(here, '../../../..');

export default defineConfig({
  root: resolve(repo, 'pos'),
  plugins: [react(), {
    name: 'captain-menu-harness',
    resolveId(id) {
      return id === '/@harness/menu-entry.tsx' ? resolve(here, 'menu-entry.tsx') : null;
    },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        if (!req.url.startsWith('/harness')) return next();
        res.setHeader('Content-Type', 'text/html');
        return res.end(await server.transformIndexHtml(req.url, readFileSync(resolve(here, 'menu.html'), 'utf8')));
      });
    },
  }],
  resolve: { dedupe: ['react', 'react-dom'] },
  server: { host: '127.0.0.1', strictPort: true, fs: { allow: [repo, here] } },
});
