import { Outlet } from 'react-router-dom';
import Header from './Header';
import Footer from './Footer';
import OfflineBanner from './OfflineBanner';

const AppLayout = () => {
  return (
    <div className="flex flex-col h-screen bg-gray-100 font-inter">
      <Header />
      <OfflineBanner />
      <div className="relative flex min-h-0 flex-1 overflow-hidden">
        <Outlet />
      </div>
      <Footer />
    </div>
  );
};

export default AppLayout;
