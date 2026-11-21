import { useState, useEffect } from 'react';
import { usePOSStore } from '../store/pos-store';
import { offlineCaptureEnabled } from '../pwa/runtime-caching';
import ScreenSizeDialog from './ScreenSizeDialog';

interface ScreenSizeProviderProps {
  children: React.ReactNode;
}

const isCaptainRoute = () => window.location.pathname.includes('/order');

const ScreenSizeProvider = ({ children }: ScreenSizeProviderProps) => {
  const phoneTill = usePOSStore((state) => offlineCaptureEnabled(state.posProfile));
  const [isScreenTooSmall, setIsScreenTooSmall] = useState(false);

  const checkScreenSize = () => {
    const isSmall = window.innerWidth < (phoneTill ? 320 : 1024) && !isCaptainRoute();
    setIsScreenTooSmall(isSmall);
  };

  useEffect(() => {
    // Check on mount
    checkScreenSize();

    // Add resize listener
    const handleResize = () => {
      checkScreenSize();
    };

    window.addEventListener('resize', handleResize);

    // Cleanup
    return () => {
      window.removeEventListener('resize', handleResize);
    };
  }, [phoneTill]);

  // Show dialog if screen is too small
  if (isScreenTooSmall) {
    return <ScreenSizeDialog />;
  }

  // Render children if screen size is acceptable
  return <>{children}</>;
};

export default ScreenSizeProvider; 