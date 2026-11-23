import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { initI18n } from '../src/i18n';
import MenuList from '../src/components/MenuList';
import { useMenuAvailability } from '../src/lib/use-menu-availability';
import type { ItemAvailability } from '../src/lib/availability-api';

// Simphony shows a menu item's remaining count as "[N]?" while it cannot
// confirm it (CAPS offline). The register keeps the last count it read when a
// refresh fails, so it must say that count is unconfirmed.

const { getMenuAvailability } = vi.hoisted(() => ({ getMenuAvailability: vi.fn() }));
vi.mock('../src/lib/availability-api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../src/lib/availability-api')>(),
  getMenuAvailability,
}));
vi.mock('../src/lib/realtime', () => ({ getRealtimeSocket: async () => ({ on: vi.fn(), off: vi.fn() }) }));
vi.mock('../src/store/pos-store', () => ({
  usePOSStore: () => ({
    menuItems: [{ id: 'ITEM-1', item: 'ITEM-1', name: 'House Special', price: 12500, image: null, course: 'Beverages', special_dish: 0 }],
    menuLoading: false, error: null, selectedCategory: '', searchQuery: '', quickFilter: 'all',
    fetchMenuItems: vi.fn(), isMenuInteractionDisabled: () => false, isOrderInteractionDisabled: () => false,
  }),
}));

const count = (available_qty: number): ItemAvailability => ({
  item_code: 'ITEM-1', sellable: true, available_qty, reason_code: 'AVAILABLE', production_policy: null,
  company: 'C1', branch: 'B1', department: null, production_unit: null, warehouse: null, plan_qty: null,
  plan_remaining: null, fg_available: null, max_producible: null, blocking_component: null, as_of: '',
});

beforeEach(async () => {
  await initI18n('en');
  getMenuAvailability.mockReset();
});

afterEach(() => vi.restoreAllMocks());

const items = [{ item: 'ITEM-1' }];

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

describe('useMenuAvailability staleness', () => {
  it('flags the counts as unconfirmed after a refresh fails, keeps them, and clears the flag on the next success', async () => {
    getMenuAvailability.mockResolvedValueOnce({ 'ITEM-1': count(3) });
    const { result } = renderHook(() => useMenuAvailability(items, 'B1', 'C1'));
    await waitFor(() => expect(result.current.availabilityMap['ITEM-1']?.available_qty).toBe(3));
    expect(result.current.availabilityStale).toBe(false);

    getMenuAvailability.mockRejectedValueOnce(new Error('Network Error'));
    await act(async () => { await result.current.refetchAll(); });
    expect(result.current.availabilityStale).toBe(true);
    expect(result.current.availabilityMap['ITEM-1']?.available_qty).toBe(3);

    getMenuAvailability.mockResolvedValueOnce({ 'ITEM-1': count(2) });
    await act(async () => { await result.current.refetchAll(); });
    expect(result.current.availabilityStale).toBe(false);
    expect(result.current.availabilityMap['ITEM-1']?.available_qty).toBe(2);
  });

  it('flags the counts as unconfirmed while the device is offline, and until a re-read after reconnecting succeeds', async () => {
    getMenuAvailability.mockResolvedValueOnce({ 'ITEM-1': count(3) });
    const { result } = renderHook(() => useMenuAvailability(items, 'B1', 'C1'));
    await waitFor(() => expect(result.current.availabilityMap['ITEM-1']).toBeTruthy());
    expect(getMenuAvailability).toHaveBeenCalledTimes(1);
    const onLine = vi.spyOn(window.navigator, 'onLine', 'get').mockReturnValue(false);
    act(() => { window.dispatchEvent(new Event('offline')); });
    expect(result.current.availabilityStale).toBe(true);

    // Another till may have sold the last ones while this one was offline.
    const reread = deferred<Record<string, ItemAvailability>>();
    getMenuAvailability.mockReturnValueOnce(reread.promise);
    onLine.mockReturnValue(true);
    act(() => { window.dispatchEvent(new Event('online')); });
    expect(getMenuAvailability).toHaveBeenCalledTimes(2);
    expect(result.current.availabilityStale).toBe(true);
    expect(result.current.availabilityMap['ITEM-1']?.available_qty).toBe(3);

    await act(async () => { reread.resolve({ 'ITEM-1': count(0) }); await reread.promise; });
    expect(result.current.availabilityStale).toBe(false);
    expect(result.current.availabilityMap['ITEM-1']?.available_qty).toBe(0);
  });

  it('re-reads on reconnect when a refresh failed while offline', async () => {
    getMenuAvailability.mockResolvedValueOnce({ 'ITEM-1': count(3) });
    const { result } = renderHook(() => useMenuAvailability(items, 'B1', 'C1'));
    await waitFor(() => expect(result.current.availabilityMap['ITEM-1']).toBeTruthy());
    const onLine = vi.spyOn(window.navigator, 'onLine', 'get').mockReturnValue(false);
    act(() => { window.dispatchEvent(new Event('offline')); });
    getMenuAvailability.mockRejectedValueOnce(new Error('Network Error'));
    await act(async () => { await result.current.refetchAll(); });
    expect(result.current.availabilityStale).toBe(true);

    getMenuAvailability.mockResolvedValueOnce({ 'ITEM-1': count(1) });
    onLine.mockReturnValue(true);
    await act(async () => { window.dispatchEvent(new Event('online')); });
    await waitFor(() => expect(result.current.availabilityStale).toBe(false));
    expect(result.current.availabilityMap['ITEM-1']?.available_qty).toBe(1);
    expect(getMenuAvailability).toHaveBeenCalledTimes(3);
  });

  it('does not confirm the counts with a read sent before the device went offline', async () => {
    getMenuAvailability.mockResolvedValueOnce({ 'ITEM-1': count(3) });
    const { result } = renderHook(() => useMenuAvailability(items, 'B1', 'C1'));
    await waitFor(() => expect(result.current.availabilityMap['ITEM-1']).toBeTruthy());
    const beforeOutage = deferred<Record<string, ItemAvailability>>();
    const afterReconnect = deferred<Record<string, ItemAvailability>>();
    getMenuAvailability.mockReturnValueOnce(beforeOutage.promise).mockReturnValueOnce(afterReconnect.promise);
    let pending: Promise<void> | undefined;
    act(() => { pending = result.current.refetchAll(); });

    const onLine = vi.spyOn(window.navigator, 'onLine', 'get').mockReturnValue(false);
    act(() => { window.dispatchEvent(new Event('offline')); });
    onLine.mockReturnValue(true);
    act(() => { window.dispatchEvent(new Event('online')); });

    await act(async () => { beforeOutage.resolve({ 'ITEM-1': count(2) }); await pending; });
    expect(result.current.availabilityMap['ITEM-1']?.available_qty).toBe(2);
    expect(result.current.availabilityStale).toBe(true);

    await waitFor(() => expect(getMenuAvailability).toHaveBeenCalledTimes(3));
    await act(async () => { afterReconnect.resolve({ 'ITEM-1': count(1) }); await afterReconnect.promise; });
    expect(result.current.availabilityStale).toBe(false);
    expect(result.current.availabilityMap['ITEM-1']?.available_qty).toBe(1);
  });
});

describe('MenuList', () => {
  it('passes the unconfirmed state to every menu card', () => {
    render(<MenuList onItemClick={vi.fn()} availabilityMap={{ 'ITEM-1': count(3) }} availabilityStale />);
    expect(screen.getByText('3 left?')).toBeInTheDocument();
  });
});
