import type { User } from '../store/slices/auth-slice';
import { hasManagementRole } from './roles';

export function homePath(user?: Pick<User, 'name' | 'roles'> | null): '/orders' | '/order' {
  const roles = user?.roles ?? [];
  const isManagerOrDesk = user?.name === 'Administrator' || hasManagementRole(roles)
    || ['Desk User', 'Floor Lead', 'Restaurant Manager'].some((role) => roles.includes(role));
  return roles.includes('URY Captain') && !roles.includes('URY Cashier') && !isManagerOrDesk
    ? '/order'
    : '/orders';
}
