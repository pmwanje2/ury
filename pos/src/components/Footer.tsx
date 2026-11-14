import { useEffect, useState } from 'react';
import { NavLink } from 'react-router-dom';
import {
  LayoutDashboard,
  LayoutGrid,
  ClipboardList,
  Table,
  BookLock,
  Settings,
} from 'lucide-react';
import { cn } from '@ury/ui';
import { t } from '../i18n';
import { useRootStore } from '../store/root-store';
import { homePath } from '../lib/home-route';
import { usePOSStore } from '../store/pos-store';
import { getBranchReservationSettings } from '../lib/table-api';

const Footer = () => {
  const user = useRootStore((state) => state.user);
  const home = homePath(user);
  const { posProfile } = usePOSStore();
  const branch = posProfile?.branch ?? null;
  const [reservationEnabled, setReservationEnabled] = useState(false);

  useEffect(() => {
    setReservationEnabled(false);
    if (!branch) {
      return;
    }

    let isMounted = true;
    getBranchReservationSettings(branch)
      .then((settings) => {
        if (isMounted) {
          setReservationEnabled(Number(settings?.enable_reservation) === 1);
        }
      })
      .catch(() => {
        if (isMounted) {
          setReservationEnabled(false);
        }
      });

    return () => {
      isMounted = false;
    };
  }, [branch]);

  const navItems = [
    { icon: LayoutDashboard, label: t('footer.dashboard'), path: home },
    { icon: LayoutGrid, label: t('footer.pos'), path: '/pos' },
    { icon: Table, label: t('footer.tables'), path: '/tables' },
    ...(reservationEnabled
      ? [{ icon: BookLock, label: t('footer.reservations') || 'Reservations', path: '/reservations' }]
      : []),
    { icon: ClipboardList, label: t(home === '/orders' ? 'footer.analytics' : 'footer.orders'), path: home === '/orders' ? '/dashboard' : '/orders' },
    { icon: Settings, label: t('footer.settings'), path: '/settings', hidden: true },
  ].filter((item) => !item.hidden);

  return (
    <div className="bg-white border-t border-gray-200 py-2 relative">
      <nav className="max-w-screen-xl mx-auto px-4">
        <div className="flex justify-center items-center gap-4">
          {navItems.map((item) => (
            <NavLink
              key={item.path}
              to={item.path}
              className={({ isActive }) =>
                cn(
                  'flex flex-col items-center p-2 rounded-lg text-gray-700 hover:bg-gray-100 transition-colors',
                  isActive && 'text-blue-600'
                )
              }
            >
              <item.icon className="w-5 h-5" />
              <span className="text-xs mt-1">{item.label}</span>
            </NavLink>
          ))}
        </div>
      </nav>
    </div>
  );
};

export default Footer;
