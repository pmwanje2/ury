import { useState, useEffect } from 'react';
import { usePOSStore } from '../store/pos-store';
import { offlineCaptureEnabled } from '../pwa/runtime-caching';
import ScreenSizeDialog from './ScreenSizeDialog';

interface ScreenSizeProviderProps {
  children: React.ReactNode;
  pathname?: string;
}

const ScreenSizeProvider = ({ children, pathname = window.location.pathname }: ScreenSizeProviderProps) => {
  const phoneTill = usePOSStore((state) => offlineCaptureEnabled(state.posProfile));
  const [width, setWidth] = useState(() => window.innerWidth);
  useEffect(() => {
    const resize = () => setWidth(window.innerWidth);
    window.addEventListener('resize', resize);
    return () => window.removeEventListener('resize', resize);
  }, []);

  const route = pathname.replace(/^\/pos(?=\/|$)/, '').replace(/\/$/, '');
  const handheldRoute = ['', '/register', '/tables', '/table'].includes(route);
  if (!pathname.includes('/order') && width < (handheldRoute || phoneTill ? 320 : 1024)) {
    return <ScreenSizeDialog />;
  }
  return <>{children}</>;
};

export default ScreenSizeProvider;
