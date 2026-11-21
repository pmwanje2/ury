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
  ['urypos/src/components/Header.vue', 'URY POS logo', '/ury-pos-default.jpg'],
  ['urypos/src/components/Login.vue', 'URY POS logo', '/ury-pos-default.jpg'],
  ['mosaic/src/components/Header.vue', 'Logo', '/mosaic-default.jpg'],
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


test('classic POS browser receipts honour the native letter head', async () => {
  let target;
  const { useInvoiceDataStore: store } = loadModule(resolve('urypos/src/stores/invoiceData.js'), {
    window: { open: url => { target = new URL(url, 'https://example.test'); } },
  }, { pinia: { defineStore: (_name, options) => options }, '../router': {},
    './utils/PrintWithQz': {}, ...Object.fromEntries(['Table', 'Menu', 'Customer', 'Notification',
      'recentOrder', 'Alert', 'NotificationModal', 'Auth', 'frappeSdk'].map(name => [`./${name}.js`, {}])),
    './NotificationModal': {},
  });
  await store.actions.printFunction.call({ recentOrders: {}, invoiceNumber: 'INV-1', print_type: 'socket',
    print_format: 'Receipt', call: { post: async () => {} }, notification: { createNotification() {} } });
  assert.equal(target.searchParams.get('format'), 'Receipt');
  assert.equal(target.searchParams.has('letterhead') || target.searchParams.has('no_letterhead'), false);
});
