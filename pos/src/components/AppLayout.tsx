import { Outlet } from 'react-router-dom';
import OfflineBanner from './OfflineBanner';
import Header from './Header';
import Footer from './Footer';
import ServiceRequestAlert from './ServiceRequestAlert';

const AppLayout = () => {
  return (
    <div className="flex flex-col h-screen bg-gray-100 font-inter">
      <Header />
      <ServiceRequestAlert />
      <OfflineBanner />
      <div className="relative flex-1 overflow-hidden flex flex-col min-h-0">
        <Outlet />
      </div>
      <Footer />
    </div>
  );
};

export default AppLayout;
