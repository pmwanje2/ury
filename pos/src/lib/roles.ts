const PRIVILEGED_ROLES = ['URY Manager', 'URY Admin', 'URY Owner', 'System Manager'];

export const hasManagementRole = (roles?: string[] | null): boolean =>
  PRIVILEGED_ROLES.some((role) => roles?.includes(role));

/** True when URY Cashier is held without any management/admin role. */
export const isCashierOnly = (roles?: string[] | null): boolean =>
  Boolean(roles?.includes('URY Cashier')) && !hasManagementRole(roles);
