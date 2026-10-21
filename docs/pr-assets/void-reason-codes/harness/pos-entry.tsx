import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import '/src/index.css';
import Orders from '/src/pages/Orders';
import { initI18n } from '/src/i18n';
import { usePOSStore } from '/src/store/pos-store';

await initI18n('en');
document.documentElement.lang = 'en';
document.documentElement.dir = 'ltr';

// The Orders page reads the POS Profile the till already loaded via
// getCombinedPosProfile (limited fields plus the full document). This harness
// lets that real fetch run against the disclosed middleware, then mounts the
// real Orders page. No component is mocked.
await usePOSStore.getState().fetchPosProfile();

createRoot(document.getElementById('root')!).render(
  <MemoryRouter>
    <Orders />
  </MemoryRouter>,
);
