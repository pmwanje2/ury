import { useRef, useState } from 'react';
import { Button, showToast } from '@ury/ui';
import { clearCashierStorage } from '../offline/cashier';
import { t } from '../i18n';

export function useTillLogout() {
  const [loggingOut, setLoggingOut] = useState(false);
  const logoutPending = useRef(false);

  const handleLogout = async () => {
    if (logoutPending.current) return;
    logoutPending.current = true;
    setLoggingOut(true);
    try {
      await clearCashierStorage(true);
      window.location.href = '/login?redirect-to=%2Fpos';
    } catch (error) {
      showToast.error(error instanceof Error && /outbox/i.test(error.message) ? error.message : t('errors.failed_logout'));
      logoutPending.current = false;
      setLoggingOut(false);
    }
  };

  return { loggingOut, handleLogout };
}

export default function TillLogout() {
  const { loggingOut, handleLogout } = useTillLogout();
  return <Button variant="outline" disabled={loggingOut} onClick={handleLogout}>{t('header.logout')}</Button>;
}
