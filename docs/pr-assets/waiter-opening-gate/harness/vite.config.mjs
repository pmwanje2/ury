import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '../../../..');
const mock = JSON.parse(readFileSync(resolve(here, 'mock-data.json'), 'utf8'));
const state = { stage: 'waiter-open' };

function readBody(req) {
  return new Promise((done) => {
    let raw = '';
    req.on('data', (chunk) => { raw += chunk; });
    req.on('end', () => {
      try { done(JSON.parse(raw || '{}')); } catch { done({}); }
    });
  });
}

function openingContext() {
  return {
    company: 'URY Foods',
    allowed_profiles: [{ name: 'Kampala POS' }],
    selected_profile: 'Kampala POS',
    payment_modes: [{ mode_of_payment: 'Cash', opening_amount: 0 }],
    user: 'cashier@example.test',
    user_full_name: 'Sam Cashier',
    branch: 'Kampala Main',
    restaurant: 'URY Foods',
    daily_close_pending: false,
    multi_cashier: { enabled: false, main_cashier_open: false, main_cashier_configured: false },
    permissions: { create: true, submit: true },
    occupied_entry: {
      name: 'OPEN-1',
      user_full_name: 'Pat Cashier',
      period_start_date: '2026-10-22 08:00:00',
      joinable: false,
    },
  };
}

function methodResponse(url) {
  const method = url.pathname.slice('/api/method/'.length);
  if (method === 'erpnext.selling.page.point_of_sale.point_of_sale.check_opening_entry') {
    return { message: 1 };
  }
  if (method === 'ury.ury_pos.api.get_pos_opening_screen_data') {
    return { message: openingContext() };
  }
  if (method === 'ury.ury_pos.api.validate_pos_close') {
    return { message: 'Success' };
  }
  if (method === 'ury.ury.doctype.ury_order.ury_order.get_captain_context') {
    const body = structuredClone(mock.api[method]);
    if (state.stage === 'till-closed') body.message.opening_state = { pos_open: false };
    return body;
  }
  if (method === 'frappe.client.get_list') {
    let doctype = url.searchParams.get('doctype') || '';
    try { doctype = JSON.parse(doctype); } catch { /* plain query value */ }
    if (doctype === 'Notification Log') {
      return { message: mock.notifications.initial };
    }
    return mock.api[`frappe.client.get_list:${doctype}`] ?? mock.fallback;
  }
  return mock.api[method] ?? mock.fallback;
}

function harness() {
  return {
    name: 'waiter-opening-gate-visual-harness',
    resolveId(id) {
      if (id === '/@harness/pos-entry.tsx') return resolve(here, 'pos-entry.tsx');
      return null;
    },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const url = new URL(req.url, 'http://harness');
        const floor = url.pathname === '/pos'
          || url.pathname === '/pos/'
          || url.pathname === '/pos/order'
          || url.pathname.startsWith('/pos/order/')
          || url.pathname === '/pos/pos'
          || url.pathname === '/harness';
        if (url.pathname.startsWith('/api/method/')) {
          if (req.method === 'POST') await readBody(req);
          res.setHeader('Content-Type', 'application/json');
          return res.end(JSON.stringify(methodResponse(url)));
        }
        if (url.pathname.startsWith('/api/resource/')) {
          const resource = decodeURIComponent(url.pathname.slice('/api/resource/'.length));
          const value = mock.resource[resource] ?? [];
          res.setHeader('Content-Type', 'application/json');
          return res.end(JSON.stringify({ data: value }));
        }
        if (floor) {
          state.stage = url.searchParams.get('stage') || 'waiter-open';
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
