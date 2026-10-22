import type { User } from '../store/slices/auth-slice';

export function homePath(user?: Pick<User, 'name' | 'roles'> | null): '/orders' | '/dashboard' | '/order' {
  const isCashier = user?.roles.includes('URY Cashier') === true;
  const isCaptain = user?.roles.includes('URY Captain') === true;
  if (isCaptain && !isCashier) return '/order';
  return isCashier ? '/orders' : '/dashboard';
}

export function isFloorOrderPath(pathname: string, user?: Pick<User, 'name' | 'roles'> | null): boolean {
  const path = pathname.replace(/^\/pos(?=\/|$)/, '').replace(/\/+$/, '') || '/';
  return path === '/order' || path.startsWith('/order/') || (path === '/' && homePath(user) === '/order');
}
