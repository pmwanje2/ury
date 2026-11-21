import { useCallback, useEffect, useRef, useState } from 'react';
import { useSessionOwner } from '../lib/use-session-owner';
import { call, getSessionUser } from '@ury/core';
import { Button } from '@ury/ui';
import { getSplitGroup, mapSplitGroupInvoiceToPOSInvoice, type POSInvoice } from '../lib/invoice-api';
import { t } from '../i18n';

interface BillRequest {
  name: string;
  table: string | null;
  invoice: string | null;
  status: string;
  requested_at: string;
}

const invoiceFields = ['name', 'restaurant_table', 'status', 'docstatus', 'invoice_printed',
  'grand_total', 'rounded_total', 'net_total', 'total_taxes_and_charges',
  'customer', 'mobile_number', 'cashier', 'waiter', 'order_type', 'posting_date', 'posting_time'];

function isOpen(invoice: POSInvoice & { docstatus?: number }) {
  return (invoice.status === 'Draft' || invoice.status === 'Unbilled') &&
    (invoice.docstatus === undefined || invoice.docstatus === 0);
}

interface Props {
  orders: POSInvoice[];
  selectOrder: (invoice: POSInvoice) => Promise<boolean>;
}

export default function BillRequestStrip(props: Props) {
  const owner = useSessionOwner();
  if (!owner || owner === 'Guest') return null;
  return <OwnerRequests key={owner} {...props} owner={owner} />;
}

function OwnerRequests({ owner, orders, selectOrder }: Props & { owner: string }) {
  const [requests, setRequests] = useState<BillRequest[]>([]);
  const [loading, setLoading] = useState(true);
  const [unavailable, setUnavailable] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [refreshFailed, setRefreshFailed] = useState(false);
  const [lookupLoading, setLookupLoading] = useState(false);
  const [message, setMessage] = useState('');
  const [choices, setChoices] = useState<POSInvoice[]>([]);
  const active = useRef(false);
  const refreshId = useRef(0);
  const hasLoaded = useRef(false);
  const branch = useRef('');

  const current = useCallback(() => active.current && getSessionUser() === owner, [owner]);
  const clearRows = useCallback(() => {
    hasLoaded.current = false;
    branch.current = '';
    setRequests([]); setChoices([]); setMessage(''); setLookupLoading(false); setRefreshFailed(false);
  }, []);
  const refresh = useCallback(async () => {
    const id = ++refreshId.current;
    if (!current()) { clearRows(); return; }
    let confirmedBranch = '';
    setLoading(!hasLoaded.current);
    setRefreshing(true);
    try {
      // Use the same session branch as getPosInvoice, never a caller-supplied branch.
      const till = await call.get<{ message: string }>('ury.ury_pos.api.getBranch');
      if (!current() || id !== refreshId.current) return;
      confirmedBranch = till.message;
      if (branch.current !== confirmedBranch) clearRows();
      if (!till.message) throw Error('Till branch unavailable');
      const tables = await call.get<{ message: Array<{ name: string }> }>('frappe.client.get_list', {
        doctype: 'URY Table', filters: { branch: till.message }, fields: ['name'], limit_page_length: 0,
      });
      if (!current() || id !== refreshId.current) return;
      const tableNames = tables.message.map(table => table.name);
      const response = tableNames.length ? await call.get<{ message: BillRequest[] }>('frappe.client.get_list', {
        doctype: 'URY Service Request',
        filters: [['request_type', '=', 'Bill'], ['status', '!=', 'Resolved'], ['table', 'in', tableNames]],
        fields: ['name', 'table', 'invoice', 'status', 'requested_at'],
        limit_page_length: 0,
      }) : { message: [] };
      if (current() && id === refreshId.current) {
        branch.current = till.message;
        hasLoaded.current = true;
        setRequests(response.message);
        setUnavailable(false);
        setRefreshFailed(false);
      }
    } catch {
      if (current() && id === refreshId.current) {
        if (!confirmedBranch) clearRows();
        if (hasLoaded.current) setRefreshFailed(true);
        else setUnavailable(true);
      }
    } finally {
      if (active.current && !current()) clearRows();
      if (current() && id === refreshId.current) {
        setLoading(false);
        setRefreshing(false);
      }
    }
  }, [current, clearRows]);

  useEffect(() => {
    active.current = true;
    void refresh();
    const interval = setInterval(() => void refresh(), 30000);
    return () => { active.current = false; clearInterval(interval); };
  }, [refresh]);

  async function open(invoice: POSInvoice) {
    if (!current()) return;
    const opened = await selectOrder(invoice);
    if (!opened && current()) setMessage(t('bill_requests.lookup_unavailable'));
  }

  async function pickUp(request: BillRequest) {
    const scope = branch.current;
    const valid = () => current() && !!scope && scope === branch.current;
    if (!valid()) { clearRows(); return; }
    setLookupLoading(true);
    setMessage('');
    setChoices([]);
    try {
      if (request.invoice) {
        const loaded = orders.find(inv => inv.name === request.invoice && isOpen(inv));
        if (loaded) { await open(loaded); return; }
        const result = await call.get<{ message: POSInvoice[] }>('frappe.client.get_list', {
          doctype: 'POS Invoice', filters: { name: request.invoice, branch: scope, status: 'Draft', docstatus: 0 },
          fields: invoiceFields, limit_page_length: 1,
        });
        if (!valid()) return;
        const exact = result.message.find(inv => inv.name === request.invoice && isOpen(inv));
        if (exact) { await open(exact); return; }
        const group = await getSplitGroup(request.invoice).catch((error: { httpStatus?: number }) => {
          if (error.httpStatus === 404) return null;
          throw error;
        });
        if (!valid()) return;
        if (!group) { setMessage(t('bill_requests.not_found', { invoice: request.invoice })); return; }
        const unpaid = group.invoices.filter(isOpen).map(mapSplitGroupInvoiceToPOSInvoice);
        if (unpaid.length) setChoices(unpaid);
        else setMessage(t('bill_requests.not_open', { invoice: request.invoice }));
      } else if (request.table) {
        // The visible queue is paginated; native permission-filtered reads find all table checks.
        const response = await call.get<{ message: POSInvoice[] }>('frappe.client.get_list', {
          doctype: 'POS Invoice',
          filters: { restaurant_table: request.table, branch: branch.current, status: 'Draft', docstatus: 0 },
          fields: invoiceFields,
          limit_page_length: 0,
        });
        if (!valid()) return;
        const checks = response.message.filter(inv => inv.restaurant_table === request.table && isOpen(inv));
        if (checks.length === 1) await open(checks[0]);
        else if (checks.length) setChoices(checks);
        else setMessage(t('bill_requests.no_open_checks', { table: request.table }));
      } else {
        setMessage(t('bill_requests.no_link'));
      }
    } catch {
      if (valid()) setMessage(t('bill_requests.lookup_unavailable'));
    } finally {
      if (valid()) setLookupLoading(false);
      else if (active.current && !current()) clearRows();
    }
  }

  return (
    <section aria-label={t('bill_requests.title')} className="mb-4 rounded-lg border bg-white p-3">
      <div className="flex items-center justify-between gap-2">
        <h2 className="font-semibold" aria-live="polite">
          {loading ? t('bill_requests.loading') : unavailable ? t('bill_requests.unavailable')
            : t('bill_requests.waiting', { count: requests.length })}
        </h2>
        <Button variant="outline" className="min-h-11" disabled={refreshing} onClick={() => void refresh()}>
          {t('bill_requests.refresh')}
        </Button>
      </div>
      {refreshFailed && <p role="status" className="mt-2 text-sm text-gray-500">{t('bill_requests.refresh_failed')}</p>}
      {!loading && !unavailable && (requests.length === 0
        ? <p className="mt-2 text-sm text-gray-500">{t('bill_requests.empty')}</p>
        : <ul className="mt-2 space-y-2">
          {requests.map(request => (
            <li key={request.name} className="flex flex-wrap items-center gap-2">
              <Button variant="outline" className="min-h-11 h-auto whitespace-normal" disabled={lookupLoading}
                onClick={() => void pickUp(request)}>
                {request.table || request.name} {request.invoice && `· ${request.invoice}`} · {request.requested_at} · {request.status}
              </Button>
              <a className="inline-flex min-h-11 items-center text-sm text-blue-600 underline"
                href={`/app/ury-service-request/${encodeURIComponent(request.name)}`}>
                {t('bill_requests.open_record')}
              </a>
            </li>
          ))}
        </ul>)}
      <div aria-live="polite">
        {lookupLoading && <p>{t('bill_requests.lookup_loading')}</p>}
        {message && <p className="mt-2 text-sm">{message}</p>}
        {choices.length > 0 && <div className="mt-2 flex flex-wrap gap-2">
          <p className="w-full text-sm">{t('bill_requests.choose_check')}</p>
          {choices.map(invoice => <Button key={invoice.name} variant="outline" className="min-h-11"
            onClick={async () => {
              setChoices([]);
              try { await open(invoice); }
              catch { if (current()) setMessage(t('bill_requests.lookup_unavailable')); }
            }}>{invoice.name}</Button>)}
        </div>}
      </div>
    </section>
  );
}
