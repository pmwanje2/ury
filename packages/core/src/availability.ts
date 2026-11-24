const MANAGERS = new Set(['URY Manager', 'URY Admin', 'System Manager']);
const FLOOR = new Set(['URY Cashier', 'URY Captain', 'URY Waiter', 'Kitchen User', 'Kitchen Cook']);

// Presentation only; the write endpoint repeats the role, privilege-rule, and branch checks.
export function availabilityPermissions(roles: readonly string[] = []) {
  const canRestore = roles.some(role => MANAGERS.has(role));
  const canMark = canRestore || roles.some(role => FLOOR.has(role));
  return {
    canRestore,
    canMark,
    /** Floor operators can trigger restore/count controls and escalate with Manager PIN in place. */
    canRequestRestoreWithPin: canMark,
  };
}

export interface AvailabilityChange {
  status?: 'Available' | 'Sold Out';
  count?: number;
  reset_policy?: 'Never' | 'Start of business day';
}
