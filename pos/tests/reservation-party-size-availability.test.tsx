import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import TableReservationEditDialog from '../src/components/TableReservationEditDialog';
import { initI18n } from '../src/i18n';

// Only tables the party fits are offered: the POS sends the party size to the
// available-tables call, and the server filters by seats and minimum seating
// (it also refuses a party that does not fit when the booking is saved).
const boundary = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('@ury/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/core')>();
  Object.assign(actual.call, { get: boundary.get, post: boundary.post });
  return { ...actual, call: actual.call };
});
vi.mock('../src/store/pos-store', () => ({
  usePOSStore: (select?: (state: unknown) => unknown) => {
    const state = { posProfile: { branch: 'Branch A' } };
    return select ? select(state) : state;
  },
}));
vi.mock('../src/store/root-store', () => ({
  useRootStore: (select: (state: unknown) => unknown) => select({ user: { name: 'host@fixture.invalid', roles: [] } }),
}));

const AVAILABLE = 'ury.ury.api.table_reservation.get_available_tables_for_reservation';
const table = (name: string, seats: number) => ({
  name, occupied: 0, latest_invoice_time: null, is_take_away: 0, restaurant_room: 'Dining',
  table_shape: 'Square', no_of_seats: seats,
});
const reservation = {
  name: 'TR-1', branch: 'Branch A', reserved_table: 'Table 4', customer: 'Grace', customer_name: 'Grace',
  customer_phone: '+256772123456', no_of_pax: 4, reserved_at: '2099-11-23 19:00:00', status: 'Confirmed',
};

const availabilityCalls = () => boundary.get.mock.calls.filter(([method]) => method === AVAILABLE).map(([, args]) => args);

beforeEach(async () => {
  vi.resetAllMocks();
  await initI18n('en');
  boundary.get.mockImplementation(async (method: string, args: { no_of_pax?: number }) => {
    if (method !== AVAILABLE) throw new Error(`Unexpected method ${method}`);
    return { message: Number(args.no_of_pax) > 4 ? [table('Table 8', 8)] : [table('Table 4', 4), table('Table 8', 8)] };
  });
});

it('sends the party size to the available-tables call and re-asks when it changes', async () => {
  render(<TableReservationEditDialog open reservation={reservation as never} onOpenChange={() => {}}
    onConfirm={async () => {}} />);
  await waitFor(() => expect(availabilityCalls()).toHaveLength(1));
  expect(availabilityCalls()[0]).toEqual(expect.objectContaining({
    branch: 'Branch A', reserved_at: '2099-11-23 19:00:00', exclude_reservation: 'TR-1', no_of_pax: 4,
  }));

  await act(async () => {
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '6' } });
  });
  await waitFor(() => expect(availabilityCalls().at(-1)).toEqual(expect.objectContaining({ no_of_pax: 6 })));
});

const saveButton = () => screen.getByRole('button', { name: /save changes/i });

it('keeps Save disabled while the tables for the party are loading', async () => {
  let release: (tables: unknown[]) => void = () => {};
  boundary.get.mockImplementation(() => new Promise(resolve => { release = tables => resolve({ message: tables }); }));
  render(<TableReservationEditDialog open reservation={reservation as never} onOpenChange={() => {}}
    onConfirm={async () => {}} />);
  await waitFor(() => expect(availabilityCalls()).toHaveLength(1));
  expect(saveButton()).toBeDisabled();
  await act(async () => { release([table('Table 4', 4)]); });
  await waitFor(() => expect(saveButton()).toBeEnabled());
});

it('clearing the party size to retype it neither asks for one guest nor drops the table', async () => {
  render(<TableReservationEditDialog open reservation={reservation as never} onOpenChange={() => {}}
    onConfirm={async () => {}} />);
  await waitFor(() => expect(availabilityCalls()).toHaveLength(1));
  const pax = screen.getByRole('spinbutton') as HTMLInputElement;
  await act(async () => { fireEvent.change(pax, { target: { value: '' } }); });
  expect(pax.value).toBe('');
  expect(availabilityCalls().map(args => args.no_of_pax)).not.toContain(1);
  expect(screen.getByRole('heading', { name: 'Table 4' })).toBeInTheDocument();
  await act(async () => { fireEvent.change(pax, { target: { value: '6' } }); });
  await waitFor(() => expect(availabilityCalls().at(-1)).toEqual(expect.objectContaining({ no_of_pax: 6 })));
  expect(availabilityCalls().map(args => args.no_of_pax)).not.toContain(1);
});
