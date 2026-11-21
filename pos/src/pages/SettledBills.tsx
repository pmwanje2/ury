import { useState } from 'react';
import { Link } from 'react-router-dom';
import { call, parseFrappeError } from '@ury/core';
import { Button } from '@ury/ui';
import { tData } from '../i18n';

type Bill = {
  name: string; customer: string; status: string; posting_date: string; posting_time: string; currency: string;
  restaurant_table: string; pos_profile: string; consolidated_invoice: string | null;
  grand_total: number; rounded_total: number; outstanding_amount: number; total_taxes_and_charges: number;
  items: { item_name: string; qty: number; amount: number }[];
  taxes: { description: string; tax_amount: number }[];
  payments: { mode_of_payment: string; amount: number }[];
};
const filters = {bill_no: 'Bill number', date_from: 'From date', date_to: 'To date', table: 'Table', pos_profile: 'Outlet', branch: 'Branch'};
const PAGE = 20;
const method = 'ury.ury_pos.api.';

export default function SettledBills() {
  const [rows, setRows] = useState<Bill[] | null>(null);
  const [bill, select] = useState<Bill | null>(null);
  const [filtersUsed, setFilters] = useState<Record<string, string>>({});
  const [start, setStart] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [keys] = useState(() => new Map<string, string>());
  const [outcome, setOutcome] = useState('');

  async function search(params: Record<string,string>, offset: number) {
    setFilters(params); setStart(offset); setBusy(true); select(null); setRows(null); setMessage('');
    try {
      const response = await call.get<{message: Bill[]}>(method + 'search_settled_bills', {...params, start: offset, page_length: PAGE});
      setRows(response.message);
    } catch (error) { setMessage(parseFrappeError(error)); }
    finally { setBusy(false); }
  }

  async function print() {
    if (!bill || busy) return;
    const key = keys.get(bill.name) || crypto.randomUUID();
    keys.set(bill.name, key); setBusy(true); setMessage('');
    try {
      const {message: result} = await call.post<{message: {status: string; failure_reason?: string}}>(
        method + 'print_bill_copy', {invoice: bill.name, idempotency_key: key});
      setOutcome(result.status);
      setMessage([tData('Copy'), result.status, result.failure_reason].filter(Boolean).join(' · '));
      if (result.status === 'COMPLETED') keys.delete(bill.name);
    } catch (error) { setOutcome('ERROR'); setMessage(parseFrappeError(error)); }
    finally { setBusy(false); }
  }

  return <main className="flex-1 overflow-auto p-4 space-y-4">
    <Link className="text-blue-700 underline" to="/orders">{tData('Orders')}</Link>
    <h1 className="text-xl font-semibold">{tData('Settled / Closed')}</h1>
    <form className="flex flex-wrap items-end gap-3" onSubmit={e => {
      e.preventDefault(); const params: Record<string,string> = {};
      new FormData(e.currentTarget).forEach((value,k) => { if (String(value).trim()) params[k] = String(value).trim(); });
      void search(params, 0);
    }}>
      {Object.entries(filters).map(([name,label]) => <label className="grid gap-1 text-sm" key={name}>{tData(label)}
        <input className="rounded border p-2" name={name} type={name.startsWith('date_') ? 'date' : 'text'} /></label>)}
      <Button type="submit" disabled={busy}>{tData('Search')}</Button>
    </form>
    {busy && <p role="status">{tData('Working…')}</p>}
    {message && <p role="status">{message}</p>}
    {rows && <section className="space-y-2" aria-label={tData('Search results')}>
      {!rows.length && <p>{tData('No closed checks found.')}</p>}
      {rows.map(row => <button type="button" disabled={busy} className="block w-full border rounded p-3 text-start" key={row.name}
        onClick={() => { select(row); setMessage(''); setOutcome(keys.has(row.name) ? 'SUBMITTED' : ''); }}>
        <strong>{row.name}</strong> · {row.posting_date} · {row.pos_profile} · {row.status} · {row.currency} {row.grand_total}
      </button>)}
      {start > 0 && <Button disabled={busy} onClick={() => search(filtersUsed, start-PAGE)}>{tData('Previous')}</Button>}
      {rows.length === PAGE && <Button disabled={busy} onClick={() => search(filtersUsed, start+PAGE)}>{tData('Next')}</Button>}
    </section>}
    {bill && <article className="space-y-3 rounded border bg-white p-4">
      <h2 className="font-semibold">{tData('Original invoice')}: {bill.name}</h2>
      <p>{[bill.posting_date, bill.posting_time, bill.restaurant_table, bill.pos_profile, bill.status, bill.consolidated_invoice].filter(Boolean).join(' · ')}</p>
      <table className="w-full text-start"><thead><tr>{['Item','Qty','Amount'].map(x => <th className="text-start" key={x}>{tData(x)}</th>)}</tr></thead>
        <tbody>{bill.items.map((r,i) => <tr key={i}><td>{r.item_name}</td><td>{r.qty}</td><td>{bill.currency} {r.amount}</td></tr>)}</tbody></table>
      <h3>{tData('Taxes')}</h3>{bill.taxes.map((r,i) => <p key={i}><span>{r.description}</span>: {bill.currency} {r.tax_amount}</p>)}
      <p>{tData('Grand total')}: {bill.currency} {bill.grand_total}</p><p>{tData('Rounded total')}: {bill.currency} {bill.rounded_total}</p>
      <h3>{tData('Original tenders')}</h3>{bill.payments.filter(p => p.amount !== 0).map((r,i) => <p key={i}><span>{r.mode_of_payment}</span>: {bill.currency} {r.amount}</p>)}
      <p>{tData('Amount on account / Balance due')}: {bill.currency} {bill.outstanding_amount}{bill.outstanding_amount > 0 && ` · ${bill.customer}`}</p>
      <Button disabled={busy} onClick={print}>{tData(['FAILED','CANCELED','ERROR'].includes(outcome) ? 'Retry copy' : keys.has(bill.name) ? 'Check / retry copy' : 'Print copy')}</Button>
    </article>}
  </main>;
}
