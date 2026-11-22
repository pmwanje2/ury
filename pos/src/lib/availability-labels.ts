import { t } from '../i18n';

export const availabilityActionLabels = () => ({
  manage: t('menu.manage_availability'),
  soldOut: t('menu.mark_sold_out'),
  restore: t('menu.mark_available'),
  dailyCount: t('menu.daily_count'),
  setCount: t('menu.set_count'),
  reset: t('menu.reset_on_open'),
  error: t('menu.availability_update_failed'),
});
