import { expect, it, vi } from 'vitest';
import { menuAvailabilityService } from './menuAvailability';

const { get, post } = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('@ury/core', () => ({ call: { get, post } }));

it('checks the dashboard catalog with a single branch batch instead of a request per dish', async () => {
  const fish = { item_code: 'FISH', sellable: false, reason_code: 'SOLD_OUT_MANUAL', available_qty: 0 };
  const salad = { item_code: 'SALAD', sellable: true, reason_code: 'AVAILABLE', available_qty: null };
  get.mockImplementation(async (_method, { item_code }) => ({ message: item_code === 'FISH' ? fish : salad }));
  post.mockResolvedValue({ message: { items: { FISH: fish, SALAD: salad } } });

  const result = await menuAvailabilityService.checkAvailability([{ item_code: 'FISH' }, { item_code: 'SALAD' }], 'B1', 'C1');
  expect(result).toEqual({ checked: [fish, salad], failed: [] });
  expect(post).toHaveBeenCalledExactlyOnceWith('ury.ury.api.ury_availability.get_menu_availability',
    { branch: 'B1', company: 'C1', item_codes: ['FISH', 'SALAD'] });
  expect(get).not.toHaveBeenCalled();
});
