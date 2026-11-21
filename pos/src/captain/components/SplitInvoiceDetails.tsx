import { useEffect, useState } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@ury/ui';
import { getPOSInvoiceItems, type POSInvoiceItem, type SplitGroupInvoice } from '../../lib/invoice-api';
import { t } from '../../i18n';

/** Read the sibling without replacing the editable table order or its unsent changes. */
export default function SplitInvoiceDetails({ invoice, onClose }: { invoice: SplitGroupInvoice; onClose: () => void }) {
  const [items, setItems] = useState<POSInvoiceItem[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    getPOSInvoiceItems(invoice.name).then(result => {
      if (active) setItems(result.items.filter(item => !item.is_disposable && item.qty > 0));
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [invoice.name]);

  return <Dialog open onOpenChange={open => { if (!open) onClose(); }}>
    <DialogContent onClose={onClose}>
      <DialogHeader>
        <DialogTitle>{invoice.name}</DialogTitle>
        <DialogDescription>{[invoice.custom_guest_label, invoice.customer_name || invoice.customer].filter(Boolean).join(' · ')}</DialogDescription>
      </DialogHeader>
      {failed ? <p role="alert">{t('orders.failed_to_load_details')}</p> : !items ? <p role="status">{t('common.loading')}</p> :
        <ul className="max-h-72 overflow-y-auto">{items.map(item => <li key={item.name}>{item.qty} × {item.item_name}</li>)}</ul>}
    </DialogContent>
  </Dialog>;
}
