import { __ } from '../i18n';

const labels: Record<string, string> = {
  'URY Admin': 'POS Admin',
  'URY Manager': 'POS Manager',
  'URY Cashier': 'POS Cashier',
  'URY Captain': 'POS Captain',
  'URY Waiter': 'POS Waiter',
  'URY Kitchen User': 'Kitchen Display User',
};

export const roleLabel = (role: string) => __(labels[role] ?? role);
export const roleOption = (value: string) => ({ value, label: roleLabel(value) });
