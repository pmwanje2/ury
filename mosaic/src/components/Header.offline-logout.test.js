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
