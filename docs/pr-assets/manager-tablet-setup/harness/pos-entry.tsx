import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { ToastProvider } from '@ury/ui';
import Header from '/src/components/Header';
import { initI18n } from '/src/i18n';
import { usePOSStore } from '/src/store/pos-store';
import { useRootStore } from '/src/store/root-store';
import '/src/index.css';
import '../../waiter-split-food-ready/harness/visual.css';

await initI18n('en');

useRootStore.setState({
  user: { name: 'manager@example.test', full_name: 'Grace Manager', roles: ['URY Manager'] },
});
usePOSStore.setState({
  posProfile: {
    name: 'Kampala POS', owner: 'manager@example.test', creation: '', modified: '', modified_by: '',
    docstatus: 0, idx: 0, company: 'URY Foods', customer: 'Walk In', country: 'Uganda', disabled: 0,
    warehouse: 'Main Warehouse', campaign: null, company_address: null, restaurant: 'URY Foods',
    branch: 'Kampala Main', currency: 'UGX', role_allowed_for_billing: [],
    role_restricted_for_table_order: [], transfer_role_permissions: [], waiter: 'manager@example.test',
    cashier: 'manager@example.test', print_format: 'Salama Receipt 80mm', qz_print: 0, qz_host: null,
    printer: null, print_type: 'socket', tableAttention: 0, paid_limit: 0, disable_rounded_total: 0,
    enable_discount: 0, multiple_cashier: 0,
  },
});

createRoot(document.getElementById('root')!).render(
  <>
    <ToastProvider />
    <MemoryRouter initialEntries={['/pos']}>
      <Header />
    </MemoryRouter>
  </>,
);
