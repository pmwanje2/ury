import { useState, useEffect } from 'react';
import ScreenSizeDialog from './ScreenSizeDialog';

interface ScreenSizeProviderProps {
  children: React.ReactNode;
}

// The gate sits above the router. Captain routes stay exempt. The till itself
// must open at a 412px phone width, so only viewports under 320px are blocked.
const MIN_TILL_WIDTH = 320;
const isCaptainRoute = () => window.location.pathname.includes('/order');

const ScreenSizeProvider = ({ children }: ScreenSizeProviderProps) => {
  const [isScreenTooSmall, setIsScreenTooSmall] = useState(false);

  const checkScreenSize = () => {
    const isSmall = window.innerWidth < MIN_TILL_WIDTH && !isCaptainRoute();
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
  }, []);

  // Show dialog if screen is too small
  if (isScreenTooSmall) {
    return <ScreenSizeDialog />;
  }

  // Render children if screen size is acceptable
  return <>{children}</>;
};

export default ScreenSizeProvider; 