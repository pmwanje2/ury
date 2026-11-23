import { useEffect, useState } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from '@ury/ui';
import { getPOSInvoiceItems, type POSInvoiceItem, type SplitGroupInvoice } from '../../lib/invoice-api';
import { t } from '../../i18n';
import { formatGuestSubtitle } from '../../lib/order-display';

/** Read the sibling without replacing the editable table order or its unsent changes. */
export default function SplitInvoiceDetails({ invoice, loadedItems, onClose }: { invoice: Pick<SplitGroupInvoice, 'name'> & Partial<Pick<SplitGroupInvoice, 'custom_guest_label' | 'customer_name' | 'customer'>>; loadedItems?: POSInvoiceItem[]; onClose: () => void }) {
  const [items, setItems] = useState<POSInvoiceItem[] | null>(loadedItems ?? null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (loadedItems) return;
    let active = true;
    getPOSInvoiceItems(invoice.name).then(result => {
      if (active) setItems(result.items);
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [invoice.name, loadedItems]);

  return <Dialog open onOpenChange={open => { if (!open) onClose(); }}>
    <DialogContent onClose={onClose}>
      <DialogHeader>
        <DialogTitle>{invoice.name}</DialogTitle>
        <DialogDescription>{formatGuestSubtitle(invoice.custom_guest_label, invoice.customer_name || invoice.customer)}</DialogDescription>
      </DialogHeader>
      {failed ? <p role="alert">{t('orders.failed_to_load_details')}</p> : !items ? <p role="status">{t('common.loading')}</p> :
        <ul className="max-h-72 overflow-y-auto">{items.filter(item => !item.is_disposable && item.qty > 0).map(item => <li key={item.name}>{item.qty} × {item.item_name}</li>)}</ul>}
    </DialogContent>
  </Dialog>;
}
