// Dev-server harness for the PR screenshots. It serves the real app source
// (HARNESS_APP=frontend|pos) and stubs only the Frappe HTTP API with
// mock-data.json. Run from the app directory; see run.sh.
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '../../../..');
const app = process.env.HARNESS_APP;
const mock = JSON.parse(readFileSync(resolve(here, 'mock-data.json'), 'utf8'));

function readBody(req) {
  return new Promise((done) => {
    let raw = '';
    req.on('data', (chunk) => { raw += chunk; });
    req.on('end', () => { try { done(JSON.parse(raw || '{}')); } catch { done({}); } });
  });
}

function harness() {
  return {
    name: 'maximus-rebrand-harness',
    resolveId(id) {
      if (id === '/@harness/pos-entry.tsx') return resolve(here, 'pos-entry.tsx');
      return null;
    },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const url = new URL(req.url, 'http://harness');
        if (mock.files[url.pathname]) {
          res.setHeader('Content-Type', 'image/svg+xml');
          return res.end(mock.files[url.pathname]);
        }
        if (url.pathname.startsWith('/api/method/')) {
          const method = url.pathname.slice('/api/method/'.length);
          const body = req.method === 'POST' ? await readBody(req) : {};
          const key = body.doctype ? method + ':' + body.doctype : method;
          res.setHeader('Content-Type', 'application/json');
          return res.end(JSON.stringify(mock.api[key] ?? mock.api[method] ?? mock.fallback));
        }
        const page = app === 'frontend' && url.pathname.startsWith('/ury') ? 'frontend.html'
          : app === 'pos' && url.pathname === '/harness' ? 'pos.html' : null;
        if (!page) return next();
        const html = readFileSync(resolve(here, page), 'utf8')
          .replace('__BOOT__', JSON.stringify(mock.boot).replace(/</g, '\\u003c'));
        res.setHeader('Content-Type', 'text/html');
        return res.end(await server.transformIndexHtml(req.url, html));
      });
    },
  };
}

export default defineConfig({
  root: resolve(repo, app),
  plugins: [react(), harness()],
  resolve: { dedupe: ['react', 'react-dom'] },
  server: { host: '127.0.0.1', strictPort: true, fs: { allow: [repo] } },
});
