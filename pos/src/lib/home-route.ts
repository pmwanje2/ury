import type { User } from '../store/slices/auth-slice';

export function homePath(user?: Pick<User, 'name' | 'roles'> | null): '/orders' | '/dashboard' | '/order' {
  const isCashier = user?.roles.includes('URY Cashier') === true;
  const isCaptain = user?.roles.includes('URY Captain') === true;
  if (isCaptain && !isCashier) return '/order';
  return isCashier ? '/orders' : '/dashboard';
}
