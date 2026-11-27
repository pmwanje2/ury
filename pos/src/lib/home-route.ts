import type { User } from '../store/slices/auth-slice';
import type { PosProfileCombined } from './pos-profile-api';
import { hasManagementRole } from './roles';

const TILL_ROLES = ['URY Cashier', 'Cashier', 'Floor Lead', 'URY Floor Lead', 'Restaurant Manager', 'Sales User', 'Sales Manager'];

export function hasTillRole(
  user?: Pick<User, 'name' | 'roles'> | null,
  profile?: Pick<PosProfileCombined, 'role_allowed_for_billing' | 'assigned_billing_user'> | null,
): boolean {
  const roles = user?.roles ?? [];
  return user?.name === 'Administrator' || hasManagementRole(roles)
    || (!!user?.name && profile?.assigned_billing_user === user.name)
    || TILL_ROLES.some(role => roles.includes(role))
    || !!profile?.role_allowed_for_billing?.some(({ role }) => roles.includes(role));
}

export function homePath(
  user?: Pick<User, 'name' | 'roles'> | null,
  profile?: Pick<PosProfileCombined, 'role_allowed_for_billing' | 'assigned_billing_user'> | null,
): '/orders' | '/order' {
  return user?.roles?.includes('URY Captain') && !hasTillRole(user, profile)
    ? '/order'
    : '/orders';
}
