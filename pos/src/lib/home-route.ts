import type { User } from '../store/slices/auth-slice';
import type { PosProfileCombined } from './pos-profile-api';
import { hasManagementRole } from './roles';

const TILL_ROLES = ['URY Cashier', 'Cashier', 'Floor Lead', 'URY Floor Lead', 'Restaurant Manager', 'Sales User', 'Sales Manager'];

export function homePath(
  user?: Pick<User, 'name' | 'roles'> | null,
  profile?: Pick<PosProfileCombined, 'role_allowed_for_billing'> | null,
): '/orders' | '/order' {
  const roles = user?.roles ?? [];
  const hasTillRole = user?.name === 'Administrator' || hasManagementRole(roles)
    || TILL_ROLES.some(role => roles.includes(role))
    || profile?.role_allowed_for_billing?.some(({ role }) => roles.includes(role));
  return roles.includes('URY Captain') && !hasTillRole
    ? '/order'
    : '/orders';
}
