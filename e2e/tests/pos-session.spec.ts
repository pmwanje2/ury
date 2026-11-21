import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

// Run after the POS production build. Only Frappe boot data, the offline worker
// acknowledgement and RPC replies are fixtures; auth, storage and idle are real.
test('paired offline staff sign-in survives the authenticated idle timeout', async ({ page }) => {
  const root = new URL('../../ury/public/pos/', import.meta.url);
  const index = await readFile(new URL('index.html', root), 'utf8');
  const entry = index.match(/<script type="module"[^>]+src="([^"]+)"/)[1];
  const origin = 'https://floor.example.test';
  let logouts = 0;
  await page.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.origin !== origin) return route.abort();
    if (url.pathname.startsWith('/assets/ury/pos/')) {
      return route.fulfill({ path: fileURLToPath(new URL(url.pathname.slice('/assets/ury/pos/'.length), root)) });
    }
    if (route.request().isNavigationRequest()) {
      return route.fulfill({ contentType: 'text/html', body: `<div id="root"></div><script type="module" src="${entry}"></script>` });
    }
    if (url.pathname.endsWith('/logout')) logouts++;
    return route.fulfill({ status: 503, json: {} });
  });
  await page.addInitScript(() => {
    const user = 'staff@example.test';
    document.cookie = `user_id=${user}; path=/`;
    window.frappe = { boot: { user: { roles: ['URY Cashier'], full_name: 'Test cashier' } } };
    window.floor_pad = { paired: true, till_open: true };
    localStorage.setItem('userAuth', 'true');
    sessionStorage.setItem(`posProfile:${user}`, JSON.stringify({
      name: 'Till', country: 'Uganda', enable_payment_without_printing: 1,
      ury_offline_capture: 1, floor_idle_signout_seconds: 1,
    }));
    Object.defineProperty(navigator.serviceWorker, 'controller', { value: {
      scriptURL: `${location.origin}/ury-pos-sw.js`,
      postMessage: (_message, [port]) => port.postMessage({ ok: true }),
    } });
  });
  await page.goto(`${origin}/pos/register`);
  await expect(page.getByRole('status').filter({ hasText: 'Offline' })).toBeVisible();
  await page.getByRole('button', { name: /username/i }).click();
  const input = page.locator('input[name="user_id"]');
  await input.fill('next@example.test');
  await new Promise(resolve => setTimeout(resolve, 1500));
  expect(logouts).toBe(0);
  await expect(page).toHaveURL(`${origin}/pos/register`);
  await expect(input).toHaveValue('next@example.test');
  expect(await page.evaluate(() => sessionStorage.getItem('ury_floor_logout_failed'))).toBeNull();
});
