import React, { useState } from 'react';
import { createRoot } from 'react-dom/client';
import CaptainActionsMenu from '/src/captain/components/CaptainActionsMenu';
import './visual.css';

// Renders the real Captain (waiter) order-screen overflow menu, opened, with
// every action the waiter would be offered. The props of the removed
// guest-phone QR entry are also passed, so the base frame shows it and the
// head frame proves it is gone. The head component ignores unknown props.
const Menu = () => {
  const [open, setOpen] = useState(true);
  const removed = { showGuestPhoneQr: true, onGuestPhoneQr: () => undefined } as Record<string, unknown>;
  return (
    <div style={{ display: 'flex', justifyContent: 'flex-end', padding: 24 }}>
      <CaptainActionsMenu
        isOpen={open}
        onOpenChange={setOpen}
        showReprintKot
        onReprintKot={() => undefined}
        showTransferTable
        onTransferTable={() => undefined}
        showTransferCaptain
        onTransferCaptain={() => undefined}
        showPrintBill
        onPrintBill={() => undefined}
        {...removed}
      />
    </div>
  );
};

createRoot(document.getElementById('root')!).render(<Menu />);
