// @vitest-environment node
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { beforeAll, expect, test } from 'vitest';
import { installWorkerGlobals, ORIGIN, TestExtendableEvent } from './helpers/sw-env';

installWorkerGlobals();

let router: import('workbox-routing').Router;

beforeAll(async () => {
  const { Router } = await import('workbox-routing');
  const { createOfflineWorker } = await import('../src/pwa/offline-sw-core');
  router = new Router();
  for (const route of createOfflineWorker({ origin: ORIGIN }).routes) router.registerRoute(route);
});

function cacheFor(href: string, method = 'GET'): string | undefined {
  const url = new URL(href);
  const request = new Request(href, { method });
  const { route } = router.findMatchingRoute({
    url,
    request,
    sameOrigin: url.origin === ORIGIN,
    event: new TestExtendableEvent('fetch') as unknown as ExtendableEvent,
  });
  return (route?.handler as { cacheName?: string } | undefined)?.cacheName;
}

const READS = [
  '/api/method/ury.ury_pos.api.getRestaurantMenu?pos_profile=Bar&order_type=Take+Away',
  '/api/method/ury.ury_pos.api.getPosProfile',
  '/api/method/ury.ury_pos.api.getModeOfPayment',
  '/api/resource/URY%20Table?fields=%5B%22name%22%5D',
  '/api/resource/URY%20Table/T-1',
  '/api/resource/POS%20Profile/Salama%20POS',
  '/api/method/frappe.client.get_list?doctype=URY+Table&fields=%5B%22name%22%5D',
];
const SHELL = ['/pos', '/pos/pos', '/pos/tables'];

const DENIED: Array<[string, string]> = [
  [ORIGIN + '/api/method/ury.ury_pos.api.getRestaurantMenu', 'POST'],
  [ORIGIN + '/api/method/ury.ury_pos.api.getPosProfile', 'POST'],
  [ORIGIN + '/api/method/ury.ury_pos.api.getModeOfPayment', 'PUT'],
  [ORIGIN + '/api/resource/URY%20Table', 'POST'],
  [ORIGIN + '/api/resource/URY%20Table/T-1', 'PUT'],
  [ORIGIN + '/api/resource/POS%20Profile/Salama%20POS', 'DELETE'],
  [ORIGIN + '/pos', 'POST'],
  [ORIGIN + '/api/method/ury.ury_pos.api.sync_order', 'GET'],
  [ORIGIN + '/api/method/ury.ury.doctype.ury_order.ury_order.get_order_invoice?table=T-1', 'GET'],
  [ORIGIN + '/api/method/logout', 'GET'],
  [ORIGIN + '/api/method/ury.ury_pos.api.getAggregatorItem?aggregator=X', 'GET'],
  [ORIGIN + '/api/method/ury.ury_pos.api.getMenuCourses', 'GET'],
  [ORIGIN + '/api/method/ury.ury_pos.api.getRestaurantMenuForReal', 'GET'],
  [ORIGIN + '/api/method/ury.ury_pos.api.getRestaurantMenu.save', 'GET'],
  [ORIGIN + '/api/resource/POS%20Invoice', 'GET'],
  [ORIGIN + '/api/resource/Customer', 'GET'],
  [ORIGIN + '/api/resource/URY%20Room', 'GET'],
  [ORIGIN + '/api/resource/URY%20Table/T-1/extra', 'GET'],
  [ORIGIN + '/api/resource/Currency/UGX', 'GET'],
  [ORIGIN + '/api/method/frappe.client.get_list?doctype=POS+Invoice', 'GET'],
  [ORIGIN + '/api/method/frappe.client.get_list?doctype=URY+Room', 'GET'],
  [ORIGIN + '/api/method/frappe.client.get_list?doctype=URY+Table+Extra', 'GET'],
  [ORIGIN + '/api/method/frappe.client.get_list', 'GET'],
  [ORIGIN + '/api/method/frappe.client.get_count?doctype=URY+Table', 'GET'],
  [ORIGIN + '/api/method/frappe.client.get?doctype=URY+Table&name=T-1', 'GET'],
  [ORIGIN + '/assets/ury/pos/assets/index-abc.js', 'GET'],
  [ORIGIN + '/login', 'GET'],
  [ORIGIN + '/posx', 'GET'],
  [ORIGIN + '/app/pos-profile', 'GET'],
  [ORIGIN + '/api/method/ury.ury_pos.api.make_invoice', 'GET'],
  // Frappe form_dict keeps the first value: this executes a User list.
  [ORIGIN + '/api/method/frappe.client.get_list?doctype=User&doctype=URY+Table', 'GET'],
  [ORIGIN + '/api/method/frappe.client.get_list?doctype=URY+Table&doctype=User', 'GET'],
  [ORIGIN + '/api/method/frappe.client.get_list?doctype=URY+Table&cmd=frappe.client.get_list', 'GET'],
  [ORIGIN + '/api/method/ury.ury_pos.api.getPosProfile?doctype=User', 'GET'],
  [ORIGIN + '/api/resource/URY%20Table?doctype=User', 'GET'],
  [ORIGIN + '/api/method/ury.ury_pos.api.getRestaurantMenu?pos_profile=A&pos_profile=B', 'GET'],
  // Other origins never match, including another tenant on the same paths.
  ['https://other-tenant.invalid/api/resource/POS%20Profile/Till', 'GET'],
  ['https://other-tenant.invalid/api/method/ury.ury_pos.api.getPosProfile', 'GET'],
  ['https://other-tenant.invalid/pos', 'GET'],
  ['http://ury.localhost/api/method/ury.ury_pos.api.getPosProfile', 'GET'],
];

test('allowlisted same-origin GET reads route to the read cache', () => {
  for (const path of READS) expect(cacheFor(ORIGIN + path), path).toBe('ury-pos-reads');
  for (const path of SHELL) expect(cacheFor(ORIGIN + path), path).toBe('ury-pos-shell');
});

test('other methods, routes, origins and ambiguous parameters are never routed', () => {
  for (const [href, method] of DENIED) expect(cacheFor(href, method), method + ' ' + href).toBeUndefined();
});

test('the worker source names its precache under the URY prefix', () => {
  const source = readFileSync(resolve('pos/src/pwa/sw.ts'), 'utf8');
  expect(source).toContain("setCacheNameDetails({ prefix: 'ury-pos'");
  expect(source).toContain('precacheAndRoute(self.__WB_MANIFEST)');
});

