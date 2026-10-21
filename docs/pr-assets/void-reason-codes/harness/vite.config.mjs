import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '../../../..');
const mock = JSON.parse(readFileSync(resolve(here, 'mock-data.json'), 'utf8'));

function fullProfile() {
  return {
    name: mock.profile_name,
    owner: 'manager@example.test',
    docstatus: 0,
    company: 'Kampala Demo',
    customer: 'Walk-in Customer',
    country: 'Uganda',
    disabled: 0,
    warehouse: 'Stores',
    restaurant: 'Dining',
    branch: 'Kampala Main',
    currency: 'UGX',
    role_allowed_for_billing: [],
    paid_limit: 30,
    custom_void_reasons: mock.void_reasons,
  };
}

function methodOf(url) {
  return decodeURIComponent(url.pathname.slice('/api/method/'.length));
}

function apiResponse(url) {
  const method = methodOf(url);
  if (method === 'ury.ury_pos.api.getPosProfile') return { message: mock.limited_profile };
  if (method === 'ury.ury_pos.api.getPosInvoice') {
    return { message: { data: [mock.invoice], next: false } };
  }
  if (method === 'ury.ury_pos.api.getPosInvoiceItems') {
    return { message: [mock.items, []] };
  }
  if (method === 'ury.ury_pos.api.get_split_group') {
    return { message: { invoices: [], current: mock.invoice.name, group: null } };
  }
  if (method === 'frappe.client.has_permission') return { message: { has_permission: true } };
  if (method === 'ury.ury_pos.api.getBranch') return { message: 'Kampala Main' };
  if (method === 'frappe.client.get_list') return { message: [] };
  if (method === 'ury.ury.api.ury_server_time.get_server_time') return { message: '2026-10-21 13:20:00' };
  return { message: null };
}

function harness() {
  return {
    name: 'void-reason-codes-visual-harness',
    resolveId(id) {
      if (id === '/@harness/pos-entry.tsx') return resolve(here, 'pos-entry.tsx');
      return null;
    },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const url = new URL(req.url, 'http://harness');
        if (url.pathname.startsWith('/api/method/')) {
          res.setHeader('Content-Type', 'application/json');
          return res.end(JSON.stringify(apiResponse(url)));
        }
        if (url.pathname.startsWith('/api/resource/')) {
          const resource = decodeURIComponent(url.pathname.slice('/api/resource/'.length));
          const body = resource.startsWith('POS Profile/')
            ? { data: fullProfile() }
            : resource.startsWith('Currency/')
              ? { data: { name: 'UGX', symbol: 'USh', fraction: 'Cent', fraction_units: 100, smallest_currency_fraction_value: 1, number_format: '#,###.##' } }
              : { data: {} };
          res.setHeader('Content-Type', 'application/json');
          return res.end(JSON.stringify(body));
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
  resolve: {
    alias: { '@': resolve(repo, 'pos/src') },
    dedupe: ['react', 'react-dom'],
  },
  server: { host: '127.0.0.1', strictPort: true, fs: { allow: [repo] } },
});
