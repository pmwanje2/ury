// Display labels for the real URY role names. They match the SiteOps
// maximus_branding Desk Translations. Values sent to Frappe stay unchanged.
export const ROLE_LABELS: Record<string, string> = {
  'URY Manager': 'Restaurant POS Manager',
  'URY Captain': 'Restaurant POS Captain',
  'URY Cashier': 'Restaurant POS Cashier',
  'URY Admin': 'Restaurant POS Admin',
};

export const roleLabel = (role: string): string => ROLE_LABELS[role] || role;

export const ROLE_OPTIONS = Object.entries(ROLE_LABELS).map(([value, label]) => ({ value, label }));
