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
    req.on('end', () => {
      try { done(JSON.parse(raw || '{}')); } catch { done({}); }
    });
  });
}

function apiResponse(url) {
  const method = url.pathname.slice('/api/method/'.length);
  if (method === 'frappe.client.get_list') {
    let doctype = url.searchParams.get('doctype') || '';
    try { doctype = JSON.parse(doctype); } catch { /* plain query value */ }
    return mock.api[`${method}:${doctype}`] ?? mock.fallback;
  }
  return mock.api[method] ?? mock.fallback;
}

function harness() {
  return {
    name: 'one-staff-app-visual-harness',
    resolveId(id) {
      if (id === '/@harness/pos-entry.tsx') return resolve(here, 'pos-entry.tsx');
      return null;
    },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const url = new URL(req.url, 'http://harness');
        if (url.pathname.startsWith('/api/method/')) {
          if (req.method === 'POST') await readBody(req);
          if (url.pathname.endsWith('/ury.ury_pos.floor_pin.sign_in')) res.statusCode = 417;
          res.setHeader('Content-Type', 'application/json');
          return res.end(JSON.stringify(apiResponse(url)));
        }
        if (url.pathname.startsWith('/api/resource/')) {
          const doctype = decodeURIComponent(url.pathname.slice('/api/resource/'.length));
          const response = mock.api[`frappe.client.get_list:${doctype}`] ?? mock.fallback;
          res.setHeader('Content-Type', 'application/json');
          return res.end(JSON.stringify({ data: response.message ?? [] }));
        }
        if (url.pathname === '/harness') {
          const html = readFileSync(resolve(here, 'pos.html'), 'utf8')
            .replace('__BOOT__', JSON.stringify({ ...mock.boot, lang: url.searchParams.get('lang') || 'en' }).replace(/</g, '\\u003c'));
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
