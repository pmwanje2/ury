import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { ToastProvider } from '@ury/ui';
import CaptainOrder from '/src/captain/pages/CaptainOrder';
import CaptainTables from '/src/captain/pages/CaptainTables';
import { initI18n } from '/src/i18n';
import { usePOSStore } from '/src/store/pos-store';
import { useRootStore } from '/src/store/root-store';
import '/src/index.css';
import './visual.css';

const params = new URLSearchParams(window.location.search);
const stage = params.get('stage') || 'menu';
const language = params.get('lang') || 'en';
document.body.dataset.screen = stage === 'kitchen' ? 'captain' : 'order';
document.cookie = 'user_id=waiter%40example.test; path=/';

await initI18n(language);
document.documentElement.lang = language;
document.documentElement.dir = language === 'ar' ? 'rtl' : 'ltr';

useRootStore.setState({
  user: { name: 'waiter@example.test', full_name: 'Amina Captain', roles: ['URY Captain'] },
});
usePOSStore.setState({
  paymentModes: ['Cash'],
  categories: [{ name: 'Main', label: 'Main' }, { name: 'Drinks', label: 'Drinks' }],
  posProfile: {
    name: 'Kampala POS', owner: 'cashier@example.test', creation: '', modified: '', modified_by: '',
    docstatus: 0, idx: 0, company: 'URY Foods', customer: 'Walk In', country: 'Uganda', disabled: 0,
    warehouse: 'Main Warehouse', campaign: null, company_address: null, restaurant: 'URY Foods',
    branch: 'Kampala Main', currency: 'UGX', role_allowed_for_billing: [],
    role_restricted_for_table_order: [], transfer_role_permissions: [], waiter: 'waiter@example.test',
    cashier: 'cashier@example.test', print_format: 'Salama Receipt 80mm', qz_print: 0, qz_host: null,
    printer: null, print_type: 'socket', tableAttention: 0, paid_limit: 0, disable_rounded_total: 0,
    enable_discount: 0, multiple_cashier: 0,
  },
});

const root = createRoot(document.getElementById('root')!);
root.render(
  <>
    <ToastProvider />
    <MemoryRouter initialEntries={[stage === 'kitchen' ? '/order' : '/order/table/T-05']}>
      <Routes>
        <Route path="/order" element={<CaptainTables />} />
        <Route path="/order/table/:table" element={<CaptainOrder />} />
      </Routes>
    </MemoryRouter>
  </>,
);
