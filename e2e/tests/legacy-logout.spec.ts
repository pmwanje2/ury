import { expect, test } from '@playwright/test';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

for (const role of ['URY Captain', 'URY Cashier']) {
  test(`${role} preserves foreign storage after logout refusal and retry`, async ({ page }) => {
    const root = new URL('../../ury/public/urypos/', import.meta.url);
    const html = (await readFile(new URL('index.html', root), 'utf8'))
      .replace(/{{ brand_json [^}]+}}/, 'null').replace('{{ csrf_token }}', 'fixture-token');
    let attempts = 0;
    await page.route('https://till.example.test/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path.startsWith('/assets/ury/urypos/')) {
        return route.fulfill({ path: fileURLToPath(new URL(path.slice('/assets/ury/urypos/'.length), root)) });
      }
      if (route.request().isNavigationRequest()) return route.fulfill({ contentType: 'text/html', body: html });
      if (path.endsWith('/logout')) {
        expect(route.request().method()).toBe('POST');
        expect(route.request().headers()['x-frappe-csrf-token']).toBe('fixture-token');
        return route.fulfill({ status: ++attempts === 1 ? 403 : 200, json: {} });
      }
      if (path.endsWith('get_logged_user')) return route.fulfill({ json: { message: attempts < 2 ? 'staff@example.test' : null } });
      if (path.includes('/resource/User/')) return route.fulfill({ json: { data: { roles: [{ role }] } } });
      if (path.endsWith('getPosProfile')) return route.fulfill({ json: { message: { pos_profile: 'Till', multiple_cashier: 1 } } });
      if (path.endsWith('frappe.client.get')) return route.fulfill({ json: { message: { role_allowed_for_billing: [{ role: 'URY Cashier' }], transfer_role_permissions: [], role_restricted_for_table_order: [] } } });
      if (path.endsWith('pos_opening_check')) return route.fulfill({ json: { message: { opening_exists: true } } });
      return route.fulfill({ json: { message: [], data: [] } });
    });
    await page.addInitScript(() => {
      if (location.pathname !== '/urypos/Table') return;
      localStorage.setItem('userAuth', 'true');
      localStorage.setItem('otherapp_panel_strike', 'keep');
    });
    await page.goto('https://till.example.test/urypos/Table');
    await page.locator('#user-menu-button').click();
    const logout = page.getByRole('button', { name: 'Log out' });
    await logout.click();
    await expect(page.getByText('Failed to logout. Please try again.')).toBeVisible();
    const keys = () => page.evaluate(() => [localStorage.getItem('userAuth'), localStorage.getItem('otherapp_panel_strike')]);
    expect(await keys()).toEqual(['true', 'keep']);
    await page.getByRole('button', { name: 'OK', exact: true }).click();
    await logout.click();
    await expect(page).toHaveURL(/\/urypos\/login$/);
    expect(attempts).toBe(2);
    expect(await keys()).toEqual([null, 'keep']);
  });
}
