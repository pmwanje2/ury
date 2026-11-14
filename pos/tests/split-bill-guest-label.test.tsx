import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import BillSplitDialog from '../src/components/BillSplitDialog';
import { initI18n } from '../src/i18n';

// M03: two people at one table stay identifiable on separately named split
// bills without a Customer master for each.

vi.mock('../src/components/CustomerPicker', () => ({ CustomerPicker: () => null }));

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  await initI18n('en');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

const items = [
  { name: 'line-1', item_name: 'Fish', qty: 1, rate: 100, amount: 100 },
  { name: 'line-2', item_name: 'Tea', qty: 1, rate: 20, amount: 20 },
];

async function setValue(input: HTMLInputElement, value: string) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

it('sends a short guest label for the new bill, keeping the same customer', async () => {
  const onConfirm = vi.fn(async () => {});
  await act(async () => root.render(
    <BillSplitDialog open onOpenChange={vi.fn()} invoiceName="BILL-1" items={items}
      sourceCustomer={{ id: 'Walk-in', name: 'Walk-in', phone: '' }} onConfirm={onConfirm} />,
  ));
  const fish = [...document.querySelectorAll<HTMLElement>('[role="button"]')].find((row) => row.textContent?.includes('Fish'))!;
  await act(async () => fish.click());
  const label = document.querySelector<HTMLInputElement>('input[name="guest_label"]')!;
  expect(label).not.toBeNull();
  expect(label.maxLength).toBe(40);
  await setValue(label, 'David');
  const confirm = [...document.querySelectorAll('button')].filter((b) => b.textContent === 'Split bill').pop()!;
  await act(async () => confirm.click());
  expect(onConfirm).toHaveBeenCalledExactlyOnceWith({
    itemsToMove: [{ name: 'line-1', qty: 1 }], customer: undefined, guestLabel: 'David',
  });
});

it('a blank label sends none', async () => {
  const onConfirm = vi.fn(async () => {});
  await act(async () => root.render(
    <BillSplitDialog open onOpenChange={vi.fn()} invoiceName="BILL-1" items={items}
      sourceCustomer={null} onConfirm={onConfirm} />,
  ));
  const fish = [...document.querySelectorAll<HTMLElement>('[role="button"]')].find((row) => row.textContent?.includes('Fish'))!;
  await act(async () => fish.click());
  const confirm = [...document.querySelectorAll('button')].filter((b) => b.textContent === 'Split bill').pop()!;
  await act(async () => confirm.click());
  expect(onConfirm).toHaveBeenCalledExactlyOnceWith({
    itemsToMove: [{ name: 'line-1', qty: 1 }], customer: undefined, guestLabel: undefined,
  });
});
