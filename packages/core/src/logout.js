import { clearAppStorage, purgeOfflineSession } from './storage';

export async function logout() {
  const response = await fetch('/api/method/logout', {
    method: 'POST',
    headers: { 'X-Frappe-CSRF-Token': window.csrf_token || '' },
  });
  if (!response.ok) throw new Error('Logout refused');
  clearAppStorage();
  await purgeOfflineSession();
}
