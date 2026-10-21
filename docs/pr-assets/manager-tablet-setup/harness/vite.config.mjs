import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '../../../..');
const mock = JSON.parse(readFileSync(resolve(here, 'mock-data.json'), 'utf8'));

function readBody(req) {
  return new Promise((done) => {
    let raw = '';
    req.on('data', (chunk) => { raw += chunk; });
    req.on('end', () => done(raw));
  });
}

function harness() {
  return {
    name: 'manager-tablet-setup-visual-harness',
    resolveId(id) {
      if (id === '/@harness/pos-entry.tsx') return resolve(here, 'pos-entry.tsx');
      return null;
    },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const url = new URL(req.url, 'http://harness');
        if (url.pathname.startsWith('/api/method/')) {
          if (req.method === 'POST') await readBody(req);
          const method = url.pathname.slice('/api/method/'.length);
          res.setHeader('Content-Type', 'application/json');
          return res.end(JSON.stringify(mock.api[method] ?? mock.fallback));
        }
        if (url.pathname.startsWith('/api/resource/')) {
          res.setHeader('Content-Type', 'application/json');
          return res.end(JSON.stringify({ data: [] }));
        }
        if (url.pathname === '/harness') {
          const html = readFileSync(resolve(here, 'pos.html'), 'utf8')
            .replace('__BOOT__', JSON.stringify(mock.boot).replace(/</g, '\\u003c'));
          res.setHeader('Content-Type', 'text/html');
          return res.end(await server.transformIndexHtml(req.url, html));
        }
        return next();
      });
    },
  };
}

export default defineConfig({
  root: resolve(repo, 'pos'),
  plugins: [react(), harness()],
  resolve: { dedupe: ['react', 'react-dom'] },
  server: { host: '127.0.0.1', strictPort: true, fs: { allow: [repo] } },
});
