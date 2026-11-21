import { useSyncExternalStore } from 'react';

function subscribeOnline(refresh: () => void) {
  window.addEventListener('online', refresh);
  window.addEventListener('offline', refresh);
  return () => { window.removeEventListener('online', refresh); window.removeEventListener('offline', refresh); };
}

export function useOnline() {
  return useSyncExternalStore(subscribeOnline, () => navigator.onLine);
}
