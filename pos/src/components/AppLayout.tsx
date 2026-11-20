import { Outlet } from 'react-router-dom';
import Header from './Header';
import Footer from './Footer';
import ServiceRequestAlert from './ServiceRequestAlert';

const AppLayout = () => {
  return (
    <div className="flex flex-col h-screen bg-gray-100 font-inter">
      <Header />
      <ServiceRequestAlert />
      <div className="flex-1 overflow-hidden flex flex-col min-h-0">
        <Outlet />
      </div>
      <Footer />
    </div>
  );
};

export default AppLayout;
