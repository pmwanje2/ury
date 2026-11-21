import { __ } from '../i18n';

const labels: Record<string, string> = {
  'URY Admin': 'Restaurant POS Admin',
  'URY Manager': 'Restaurant POS Manager',
  'URY Cashier': 'Restaurant POS Cashier',
  'URY Captain': 'Restaurant POS Captain',
  'URY Waiter': 'POS Waiter',
  'URY Kitchen User': 'Kitchen Display User',
};

export const roleLabel = (role: string) => __(labels[role] ?? role);
export const roleOption = (value: string) => ({ value, label: roleLabel(value) });
