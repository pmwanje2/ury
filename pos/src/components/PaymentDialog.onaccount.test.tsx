import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import { initI18n } from '../i18n';
import { useRootStore } from '../store/root-store';
import { usePOSStore } from '../store/pos-store';
import PaymentDialog from './PaymentDialog';

const root = useRootStore.getState(), pos = usePOSStore.getState();
beforeEach(async () => { await initI18n('en'); });
afterEach(() => { cleanup(); vi.restoreAllMocks(); useRootStore.setState(root,true); usePOSStore.setState(pos,true); });
function show(role: string, customer='Walk-in') {
  useRootStore.setState({user:{name:'user', roles:[role]}});
  usePOSStore.setState({paymentModes:['Cash'], fetchPaymentModes:async()=>{},posProfile:{name:'Till',customer:'Walk-in',allow_partial_payment:1,order_type_customer_map:{},default_customer:null} as any});
  render(<PaymentDialog customer={customer} grandTotal={100} roundedTotal={100} order={{invoice_printed:1}} invoice="B1" posProfile="Till" table={null} cashier="user" owner="user" onClose={vi.fn()} fetchOrders={async()=>{}} clearSelectedOrder={vi.fn()} />);
}
it.each([0,40])('selects the unique approved ID and sends only actual tender %s',async amount=>{
  const get=vi.spyOn(call,'get').mockResolvedValue({message:['AC-1','AC-2'].map(name=>({name,customer_name:'Acme',balance:25,limit:225,available:200,currency:'UGX'}))});
  const post=vi.spyOn(call,'post').mockResolvedValue({message:{status:'Submitted'}});
  show('URY Cashier'); fireEvent.change(screen.getByRole('searchbox',{name:/charge to account/i}),{target:{value:'Acme'}});
  const result=await screen.findByRole('button',{name:/AC-2/}); expect(result.textContent).toMatch(/Acme/); for (const n of ['25','225','200']) expect(result.textContent).toContain(n);
  fireEvent.click(result); if(amount) fireEvent.change(screen.getByRole('spinbutton'),{target:{value:String(amount)}});
  expect(screen.getByRole('status')).toHaveTextContent(new RegExp('On account.*'+(100-amount)));
  fireEvent.click(screen.getByRole('button',{name:/^Pay /}));
  await waitFor(()=>expect(post).toHaveBeenCalledWith(expect.stringContaining('make_invoice'),expect.objectContaining({customer:'AC-2',payments:[{mode_of_payment:'Cash',amount}]})));
  expect(get).toHaveBeenCalledWith('ury.ury_pos.api.credit_accounts',{pos_profile:'Till',query:'Acme'});
});
it('captains cannot see balances or settle a short named bill',()=>{show('URY Captain','Named'); fireEvent.change(screen.getByRole('spinbutton'),{target:{value:'40'}}); expect(screen.queryByRole('searchbox')).toBeNull();expect(screen.getByRole('button',{name:/^Pay /})).toBeDisabled();});
