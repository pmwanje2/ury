import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import Orders from './Orders';
import { isOpeningGatedRoute } from '../components/POSOpeningProvider';

it('keeps transactional orders gated and exposes the closed register without an opening', () => {
  expect(isOpeningGatedRoute('/orders')).toBe(true);
  expect(isOpeningGatedRoute('/orders/settled')).toBe(false);
});

it('searches posted checks, previews original tenders, and retries only the copy', async () => {
  const get = vi.spyOn(call, 'get').mockResolvedValue({message: [{name:'INV-8', status:'Partly Paid', posting_date:'2026-10-01', currency:'UGX', outstanding_amount:75, grand_total:100, items:[{item_name:'Lunch',qty:1,amount:100}], taxes:[{description:'VAT',tax_amount:10}], payments:[{mode_of_payment:'Visa',amount:25}]}]});
  const post = vi.spyOn(call,'post').mockResolvedValueOnce({message:{status:'FAILED',failure_reason:'offline'}}).mockResolvedValueOnce({message:{status:'SUBMITTED'}});
  render(<MemoryRouter initialEntries={['/orders/settled']}><Orders /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button',{name:'Search'}));
  fireEvent.click(await screen.findByRole('button',{name:/INV-8/}));
  for (const label of ['Lunch','VAT','Visa']) expect(screen.getByText(label)).toBeTruthy();
  expect(screen.getByText(/Amount on account.*75/i)).toBeTruthy();
  expect(screen.queryByRole('button',{name:/edit|settle bill|collect|cancel order/i})).toBeNull();
  fireEvent.click(screen.getByRole('button',{name:'Print copy'}));
  fireEvent.click(await screen.findByRole('button',{name:'Retry copy'}));
  expect(get.mock.calls.some(c=>c[0]==='ury.ury_pos.api.search_settled_bills')).toBe(true);
  expect(post.mock.calls[0]).toEqual(post.mock.calls[1]);
  expect(post.mock.calls[0][0]).toBe('ury.ury_pos.api.print_bill_copy');
});

it('keeps the settled register open when a numbered fallback is cancelled', async () => {
  vi.spyOn(call, 'get').mockResolvedValue({ message: [{ name: 'COPY-4', currency: 'UGX', items: [], payments: [], taxes: [] }] });
  vi.spyOn(call, 'post').mockResolvedValue({ message: { status: 'RESERVED', delivery: 'browser', html: 'COPY #4', print_url: '/printview?copy_number=4' } });
  const open = vi.spyOn(window, 'open').mockReturnValue({ closed: true } as Window);
  vi.spyOn(window, 'confirm').mockReturnValue(false);
  render(<MemoryRouter initialEntries={['/orders/settled']}><Orders /></MemoryRouter>);
  fireEvent.click(await screen.findByRole('button', { name: 'Search' }));
  fireEvent.click(await screen.findByRole('button', { name: /COPY-4/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Print copy' }));
  await vi.waitFor(() => expect(open).toHaveBeenCalledWith('/printview?copy_number=4', '_blank'));
  expect(await screen.findByText(/delivery was not confirmed/i)).toBeTruthy();
  expect(call.post).not.toHaveBeenCalledWith(expect.stringContaining('confirm_bill_copy'), expect.anything());
});
