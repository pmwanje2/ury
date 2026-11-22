import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import test from 'node:test';
import { loadModule } from './brand-harness.mjs';

const require = createRequire(new URL('../../../urypos/package.json', import.meta.url));
const Vue = require('vue');
const { renderToString } = require('vue/server-renderer');
const { compile } = require('@vue/compiler-dom');
const { parse } = require('@vue/compiler-sfc');

const fixtures = [
  ['urypos/src/components/Header.vue', 'Maximus POS', '/assets/ury/Images/maximus-mark.svg'],
  ['urypos/src/components/Login.vue', 'Maximus POS', '/assets/ury/Images/maximus-mark.svg'],
  ['mosaic/src/components/Header.vue', 'Maximus Kitchen', '/assets/ury/Images/maximus-mark.svg'],
];

for (const [file, defaultAlt, defaultLogo] of fixtures) {
  for (const brand of [null, { name: 'Example\\Site "<script>', logo: '/files/tenant.png', favicon: '/files/tenant.ico' }]) {
    test(`${file}: renders ${brand ? 'site identity' : 'existing URY default'} without changing the Vue layout`, async () => {
      const dependencies = {
        '../i18n': { t: text => text },
        '../composables/productionScope': { productionScopeKey: 'scope' },
        './ScopeSelector.vue': {},
        '@/assets/logos/URY_POS.jpg': { __esModule: true, default: '/ury-pos-default.jpg' },
        '@/assets/logos/mosaic.jpg': { __esModule: true, default: '/mosaic-default.jpg' },
        '@/stores/Auth.js': { useAuthStore: () => ({ cashier: true, sessionUser: 'test@example.com', recentWaiters: [], getLoginAvatar: () => 'Test User' }) },
        '@/stores/posOpening.js': { posOpening: () => ({}) },
        '@/stores/posClosing.js': { posClosing: () => ({}) },
        '@/stores/bottomTabs.js': { tabFunctions: () => ({ currentTab: '/Table' }) },
        '@/stores/Table.js': { useTableStore: () => ({}) },
      };
      const component = loadModule(resolve(file), { window: { frappe: { boot: { ury_brand: brand } } } }, dependencies).default;
      const template = parse(readFileSync(file, 'utf8')).descriptor.template.content;
      component.render = new Function('Vue', compile(template, { prefixIdentifiers: true }).code)(Vue);
      const app = Vue.createSSRApp(component);
      app.provide('scope', { companies: [], branches: [] });
      app.config.globalProperties.$route = { name: 'Kitchen' };
      app.config.globalProperties.__ = text => text;
      app.component('router-link', { template: '<a><slot /></a>' });
      const html = await renderToString(app);
      assert.ok(html.includes(`src="${brand?.logo ?? defaultLogo}"`), 'Rendered logo must come from site settings when set');
      assert.ok(html.includes(`alt="${brand ? 'Example\\Site &quot;&lt;script&gt;' : defaultAlt}"`), 'Logo alt must use escaped site name or the original alt');
      if (file.startsWith('mosaic/')) assert.match(html, /<img[^>]+class="[^"]*h-8[^"]*w-auto/);
      assert.ok(!html.includes('<script>'), 'Site name cannot inject markup');
    });
  }
}

function classicStore(window = {}) {
  const { useInvoiceDataStore } = loadModule(resolve('urypos/src/stores/invoiceData.js'), {
    window,
  }, { pinia: { defineStore: (_name, options) => options }, '../router': {},
    './utils/PrintWithQz': { printWithQz: async () => 'printed' }, ...Object.fromEntries(['Table', 'Menu', 'Customer', 'Notification',
      'recentOrder', 'Alert', 'NotificationModal', 'Auth', 'frappeSdk'].map(name => [`./${name}.js`, {}])),
    './NotificationModal': {},
  });
  return useInvoiceDataStore.actions;
}

test('classic POS browser printview explicitly suppresses letterhead', async () => {
  let target;
  const store = classicStore({ open: url => { target = new URL(url, 'https://example.test'); } });
  await store.printFunction.call({ recentOrders: {}, invoiceNumber: 'INV-1', print_type: 'socket',
    print_format: 'Receipt', call: { post: async () => {} }, notification: { createNotification() {} } });
  assert.equal(target.searchParams.get('format'), 'Receipt');
  assert.equal(target.searchParams.get('no_letterhead'), '1');
  assert.equal(target.searchParams.get('letterhead'), 'No Letterhead');
  assert.equal(target.searchParams.get('settings'), '{}');
});

test('classic POS QZ leaves letterhead selection to native printview', async () => {
  let request;
  const store = classicStore();
  await store.printFunction.call({ recentOrders: {}, invoiceNumber: 'INV-1', print_type: 'qz',
    print_format: 'Receipt', updatePrintTable: async () => true,
    call: { get: async (method, args) => {
      request = { method, ...args };
      return { message: { html: '<p>Receipt</p>' } };
    } },
  });
  assert.deepEqual(request, { method: 'frappe.www.printview.get_html_and_style', doc: 'POS Invoice',
    name: 'INV-1', print_format: 'Receipt', _lang: 'en' });
});
