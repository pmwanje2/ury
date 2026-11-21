import { useSyncExternalStore } from 'react';
import { getSessionUser } from '@ury/core';

function subscribe(notify: () => void) {
  const cookies = (window as Window & { cookieStore?: EventTarget }).cookieStore;
  cookies?.addEventListener('change', notify);
  window.addEventListener('focus', notify);
  const timer = cookies ? undefined : window.setInterval(notify, 1000);
  return () => {
    cookies?.removeEventListener('change', notify);
    window.removeEventListener('focus', notify);
    window.clearInterval(timer);
  };
}

export function useSessionOwner() {
  return useSyncExternalStore(subscribe, getSessionUser);
}
