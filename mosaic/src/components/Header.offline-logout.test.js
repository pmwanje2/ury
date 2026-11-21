import { afterEach, expect, it, vi } from 'vitest';
import Header from './Header.vue';

afterEach(() => vi.unstubAllGlobals());
it('ends cached POS reads before finishing kitchen logout', async () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}')));
  const remove = vi.fn().mockResolvedValue(true);
  vi.stubGlobal('caches', { keys: async () => ['ury-pos-reads', 'other'], delete: remove });
  await Header.methods.logout.call({ showUserMenu: true });
  expect(remove.mock.calls).toEqual([['ury-pos-reads']]);
});

it('posts the current CSRF token and preserves shared till state on refusal', async () => {
  const request = vi.fn().mockResolvedValue(new Response('{}', {status: 403}));
  vi.stubGlobal('fetch', request);
  vi.stubGlobal('csrf_token', 'fixture-token');
  sessionStorage.setItem('posProfile:fixture', '{}');
  const menu = { showUserMenu: true };
  await Header.methods.logout.call(menu);
  expect(new Headers(request.mock.calls[0][1].headers).get('X-Frappe-CSRF-Token')).toBe('fixture-token');
  expect(menu.logoutError).toBeTruthy();
  expect(menu.loggingOut).toBe(false);
  expect(sessionStorage.getItem('posProfile:fixture')).toBe('{}');
});
