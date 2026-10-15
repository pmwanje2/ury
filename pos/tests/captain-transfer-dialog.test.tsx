import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import CaptainTransferDialog from '../src/components/CaptainTransferDialog';
import { initI18n } from '../src/i18n';

const boundary = vi.hoisted(() => ({ get: vi.fn(), getDocList: vi.fn() }));
vi.mock('@ury/core', async (importOriginal) => ({
  ...await importOriginal<typeof import('@ury/core')>(),
  call: { get: boundary.get }, db: { getDocList: boundary.getDocList },
}));

const method = 'ury.ury.doctype.ury_order.ury_order.get_captain_transfer_candidates';
const eligible = [
  { name: 'amina@fixture.invalid', full_name: 'Amina Nakato' },
  { name: 'waiter3@fixture.invalid', full_name: 'Test Waiter Three' },
];
let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  vi.resetAllMocks();
  await initI18n('en');
  boundary.get.mockResolvedValue({ message: eligible });
  // The old global read exposes an unrelated site user; it must not be used.
  boundary.getDocList.mockResolvedValue([{ name: 'other-branch@fixture.invalid', full_name: 'Other Branch' }]);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.unstubAllGlobals();
});

async function render(open = true, invoiceName = 'POS-INV-1') {
  await act(async () => root.render(<CaptainTransferDialog open={open}
    onOpenChange={() => {}} currentCaptain="current@fixture.invalid"
    {...{ invoiceName }} onConfirm={async () => {}} />));
}

function candidateLabels() {
  return [...document.querySelectorAll('button p.font-medium')].map(el => el.textContent);
}

it('lists only eligible captains returned for the order', async () => {
  await render();
  expect(candidateLabels()).toEqual(['Amina Nakato', 'Test Waiter Three']);
  expect(boundary.get).toHaveBeenCalledExactlyOnceWith(method, { invoice: 'POS-INV-1' });
  expect(boundary.getDocList).not.toHaveBeenCalled();
  expect(document.body.textContent).not.toContain('Other Branch');
});

it('shows a newly added waiter beyond twenty users without searching', async () => {
  const roster = Array.from({ length: 25 }, (_, i) => ({
    name: `existing-waiter${i}@fixture.invalid`, full_name: `Waiter ${String(i).padStart(2, '0')}`,
  }));
  roster.push(eligible[1]);
  boundary.get.mockResolvedValue({ message: roster });
  boundary.getDocList.mockImplementation(async (_doctype, options) => roster.slice(0, options.limit));
  await render();
  expect(candidateLabels()).toHaveLength(26);
  expect(candidateLabels()).toContain('Test Waiter Three');
  expect(document.querySelector<HTMLInputElement>('input[type="search"]')!.value).toBe('');
});

it('filters eligible captains locally by name or full name', async () => {
  await render();
  const input = document.querySelector<HTMLInputElement>('input[type="search"]')!;
  for (const query of ['NAKATO', 'amina@']) {
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, query);
      input.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(candidateLabels()).toEqual(['Amina Nakato']);
  }
  expect(boundary.get).toHaveBeenCalledTimes(1);
  expect(boundary.getDocList).not.toHaveBeenCalled();
});

it('shows the server refusal when candidates cannot be loaded', async () => {
  boundary.get.mockRejectedValue({
    _server_messages: JSON.stringify([JSON.stringify({ message: 'You are not permitted to transfer captains.' })]),
    exc_type: 'PermissionError',
  });
  await render();
  expect(document.body.textContent).toContain('You are not permitted to transfer captains.');
  expect(candidateLabels()).toEqual([]);
});

it('reloads candidates for each opening and active invoice', async () => {
  await render();
  await render(false);
  boundary.get.mockResolvedValue({ message: [eligible[1]] });
  await render(true, 'POS-INV-2');
  expect(candidateLabels()).toEqual(['Test Waiter Three']);
  expect(boundary.get).toHaveBeenLastCalledWith(method, { invoice: 'POS-INV-2' });
  expect(boundary.get).toHaveBeenCalledTimes(2);
});
