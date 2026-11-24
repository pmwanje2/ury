import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ProductDialog from '../src/components/ProductDialog';
import OrderPanel from '../src/components/OrderPanel';
import POS from '../src/pages/POS';
import { usePOSStore, type MenuItem } from '../src/store/pos-store';
import { useRootStore } from '../src/store/root-store';
import en from '../src/i18n/locales/en.json';
import fr from '../src/i18n/locales/fr.json';
import es from '../src/i18n/locales/es.json';
import ar from '../src/i18n/locales/ar.json';

const mockSyncOrder = vi.fn();

vi.mock('../src/lib/order-api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../src/lib/order-api')>();
  return {
    ...actual,
    syncOrder: (...args: unknown[]) => mockSyncOrder(...args),
  };
});

vi.mock('../src/lib/kot-status-api', () => ({
  getOrderKotStatus: vi.fn().mockResolvedValue({ items: {} }),
}));

vi.mock('../src/lib/use-menu-availability', () => ({
  useMenuAvailability: () => ({
    availabilityMap: {},
    availabilityStale: false,
    refetchAll: vi.fn(),
  }),
  sortMenuItemsByAvailability: <T,>(items: T[]) => items,
}));

vi.mock('../src/offline/use-outbox', () => ({
  useOutbox: () => ({ queue: false, offline: false, replaying: false, rows: [] }),
}));

vi.mock('../src/offline/cashier', () => ({
  assertOfflineActionAllowed: vi.fn().mockResolvedValue(undefined),
  submitOrderOfflineAware: async ({ onlineSubmit }: { onlineSubmit: (ids?: unknown) => Promise<unknown> }) => ({
    offline: false,
    response: await onlineSubmit(undefined),
  }),
}));

vi.mock('@ury/core', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@ury/core')>();
  return {
    ...actual,
    db: {
      ...actual.db,
      getDoc: vi.fn().mockResolvedValue({
        name: 'RIBEYE',
        item_name: 'Ribeye Steak',
        image: null,
        custom_pos_item_variants: [],
        custom_pos_add_on_items: [],
      }),
    },
  };
});

const STEAK_ITEM: MenuItem = {
  id: 'RIBEYE',
  item: 'RIBEYE',
  name: 'Ribeye Steak',
  item_name: 'Ribeye Steak',
  image: null,
  price: 65000,
  course: 'Mains',
  course_label: 'Mains',
  special_dish: 0,
  has_modifiers: true,
  has_required_modifiers: true,
  prompt_on_add: true,
  is_combo_meal: false,
  modifier_groups: [
    {
      name: 'Meat Temperature',
      display_title: 'Meat Temperature',
      group_type: 'Condiment',
      required: true,
      prompt_on_add: true,
      min_selections: 1,
      max_selections: 1,
      allow_prefixes: false,
      allowed_prefixes: [],
      inherit_parent_routing: true,
      hide_unpriced_on_receipt: true,
      options: [
        {
          option_code: 'MED-RARE',
          item: null,
          label: 'Medium Rare',
          price_mode: 'Unpriced Prep',
          upcharge_amount: 0,
          unit_price: 0,
          available: true,
        },
        {
          option_code: 'WELL-DONE',
          item: null,
          label: 'Well Done',
          price_mode: 'Unpriced Prep',
          upcharge_amount: 0,
          unit_price: 0,
          available: true,
        },
      ],
    },
    {
      name: 'Steak Extras',
      display_title: 'Steak Extras',
      group_type: 'Condiment',
      required: false,
      prompt_on_add: false,
      min_selections: 0,
      max_selections: 2,
      allow_prefixes: true,
      allowed_prefixes: ['No', 'Extra', 'Side', 'Light', 'On Side'],
      inherit_parent_routing: true,
      hide_unpriced_on_receipt: true,
      options: [
        {
          option_code: 'ONION',
          item: null,
          label: 'Onion',
          price_mode: 'Unpriced Prep',
          upcharge_amount: 0,
          unit_price: 0,
          available: true,
        },
        {
          option_code: 'EXTRA-CHEESE',
          item: 'EXTRA-CHEESE',
          label: 'Aged Cheddar',
          price_mode: 'Menu Item Price',
          upcharge_amount: 0,
          unit_price: 3000,
          available: true,
        },
      ],
    },
  ],
};

const COMBO_ITEM: MenuItem = {
  id: 'BURGER-COMBO',
  item: 'BURGER-COMBO',
  name: 'Burger Combo',
  item_name: 'Burger Combo',
  image: null,
  price: 25000,
  course: 'Combos',
  course_label: 'Combos',
  special_dish: 0,
  has_modifiers: true,
  has_required_modifiers: true,
  prompt_on_add: true,
  is_combo_meal: true,
  product_bundle: 'BURGER-COMBO',
  bundle_defaults: [
    { item_code: 'BURGER', item_name: 'Classic Burger', qty: 1 },
    { item_code: 'FRIES', item_name: 'French Fries', qty: 1 },
    { item_code: 'COLA', item_name: 'Cola', qty: 1 },
  ],
  modifier_groups: [
    {
      name: 'Choose Side',
      display_title: 'Choose Side',
      group_type: 'Combo Choice',
      required: true,
      prompt_on_add: true,
      min_selections: 1,
      max_selections: 1,
      allow_prefixes: false,
      allowed_prefixes: [],
      inherit_parent_routing: true,
      hide_unpriced_on_receipt: false,
      options: [
        {
          option_code: 'FRIES',
          item: 'FRIES',
          label: 'French Fries',
          price_mode: 'Combo Upcharge',
          upcharge_amount: 0,
          replaces_bundle_item: 'FRIES',
          is_default: true,
          unit_price: 0,
          available: true,
        },
        {
          option_code: 'ONION-RINGS',
          item: 'ONION-RINGS',
          label: 'Onion Rings',
          price_mode: 'Combo Upcharge',
          upcharge_amount: 3000,
          upcharge_item: 'COMBO-UPGRADE-FEE',
          replaces_bundle_item: 'FRIES',
          is_default: false,
          unit_price: 3000,
          available: true,
        },
      ],
    },
  ],
};

type Tree = { [key: string]: string | Tree };
function lookup(tree: Tree, key: string): string | undefined {
  let node: string | Tree | undefined = tree;
  for (const part of key.split('.')) node = typeof node === 'object' ? node[part] : undefined;
  return typeof node === 'string' ? node : undefined;
}
const placeholders = (value: string) => (value.match(/\{\{\w+\}\}/g) ?? []).sort().join(',');

describe('POS-PR-8 G3 Modifiers & G4 Combo Meals UI', () => {
  beforeEach(() => {
    mockSyncOrder.mockReset().mockResolvedValue({ message: { name: 'POS-INV-1' } });
    useRootStore.setState({
      user: { name: 'cashier@maximus.test', roles: ['URY Cashier'], full_name: 'Cashier' } as any,
    });
    usePOSStore.setState({
      isInitializing: false,
      loading: false,
      menuLoading: false,
      orderLoading: false,
      profileLoading: false,
      error: null,
      currency: 'UGX',
      posProfile: {
        name: 'Main POS',
        restaurant: 'Grand Terrace',
        branch: 'Main',
        company: 'Maximus Hospitality',
        currency: 'UGX',
        cashier: 'cashier@maximus.test',
        owner: 'owner@maximus.test',
      } as any,
      menuItems: [STEAK_ITEM, COMBO_ITEM],
      categories: [
        { name: 'Mains', label: 'Mains' },
        { name: 'Combos', label: 'Combos' },
      ],
      activeOrders: [],
      selectedItem: null,
      selectedOrderType: 'Take Away',
      selectedCustomer: { id: 'Walk-in', name: 'Walk-in', phone: '' },
      paymentModes: ['Cash'],
      orderId: null,
      isUpdatingOrder: false,
      fetchMenuItems: vi.fn().mockResolvedValue(undefined),
    });
  });

  it('single tap on item with required modifiers immediately opens selector without adding unconfigured item', async () => {
    const user = userEvent.setup();
    render(<POS />);

    await user.click(screen.getByText('Ribeye Steak'));

    // Dialog opens immediately with Meat Temperature required group, cart still empty
    await waitFor(() => {
      expect(screen.getByText('Meat Temperature')).toBeInTheDocument();
    });
    expect(usePOSStore.getState().activeOrders).toHaveLength(0);
    // Submit disabled until required Meat Temperature is picked
    expect(screen.getByRole('button', { name: /add to order/i })).toBeDisabled();
  });

  it('configures unpriced prep + priced modifier, links child to parent, and indents in OrderPanel', async () => {
    const user = userEvent.setup();
    usePOSStore.setState({ selectedItem: STEAK_ITEM });
    const onClose = vi.fn();

    const { unmount } = render(<ProductDialog onClose={onClose} />);

    // Required Meat Temp not chosen yet -> disabled
    const submitBtn = screen.getByRole('button', { name: /add to order/i });
    expect(submitBtn).toBeDisabled();

    // Pick Medium Rare (required)
    await user.click(screen.getByRole('button', { name: /Medium Rare/i }));
    expect(submitBtn).toBeEnabled();

    // Pick "No" prefix then "Onion" (unpriced prep -> NO extra cart row!)
    await user.click(screen.getByRole('button', { name: 'No' }));
    await user.click(screen.getByRole('button', { name: /Onion/i }));

    // Pick "Extra" prefix then "Aged Cheddar" (+UGX 3,000 -> linked child row!)
    await user.click(screen.getByRole('button', { name: 'Extra' }));
    await user.click(screen.getByRole('button', { name: /Aged Cheddar/i }));

    await user.click(submitBtn);
    expect(onClose).toHaveBeenCalled();

    const orders = usePOSStore.getState().activeOrders;
    // Parent + 1 priced modifier row (unpriced Onion & Medium Rare do NOT create extra rows!)
    expect(orders).toHaveLength(2);
    const parent = orders[0];
    const child = orders[1];
    expect(parent.item).toBe('RIBEYE');
    expect(parent.selectedModifiers?.map((m) => `${m.prefix ? `${m.prefix} ` : ''}${m.label}`)).toEqual(
      expect.arrayContaining(['Medium Rare', 'No Onion'])
    );
    expect(child.item).toBe('EXTRA-CHEESE');
    expect(child.isModifier).toBe(true);
    expect(child.parentLineKey).toBe(parent.reservationLineKey);
    expect(child.modifierPrefix).toBe('Extra');

    unmount();

    // Render OrderPanel: child modifier renders indented under parent, parent qty bump scales child, remove removes both
    render(<OrderPanel />);
    expect(screen.getByTestId(`modifier-child-${child.uniqueId}`)).toBeInTheDocument();
    expect(screen.getByText(/Medium Rare/i)).toBeInTheDocument();
    expect(screen.getByText(/No Onion/i)).toBeInTheDocument();

    // Increment parent qty 1 -> 2 scales child modifier qty to 2
    await usePOSStore.getState().updateQuantity(parent.uniqueId!, 2);
    expect(usePOSStore.getState().activeOrders.find((o) => o.uniqueId === child.uniqueId)?.quantity).toBe(2);

    // Sync sends parent-child linkage fields cleanly
    await user.click(screen.getByRole('button', { name: /add new order/i }));
    await waitFor(() => expect(mockSyncOrder).toHaveBeenCalledTimes(1));
    const sentItems = mockSyncOrder.mock.calls[0][0].items;
    expect(sentItems).toHaveLength(2);
    expect(sentItems[0].custom_modifiers_json).toContain('MED-RARE');
    expect(sentItems[1].custom_parent_line_key).toBe(sentItems[0].reservation_line_key);
    expect(sentItems[1].custom_is_modifier).toBe(1);
    expect(sentItems[1].custom_modifier_prefix).toBe('Extra');
  });

  it('pre-selects Product Bundle combo defaults, swaps side with upcharge, and sends bundle overrides', async () => {
    const user = userEvent.setup();
    usePOSStore.setState({ selectedItem: COMBO_ITEM });
    const onClose = vi.fn();

    render(<ProductDialog onClose={onClose} />);

    // Default French Fries is pre-selected -> submit enabled immediately!
    const submitBtn = screen.getByRole('button', { name: /add to order/i });
    expect(submitBtn).toBeEnabled();

    // Switch side from French Fries -> Onion Rings (+3,000)
    await user.click(screen.getByRole('button', { name: /Onion Rings/i }));
    await user.click(submitBtn);

    const orders = usePOSStore.getState().activeOrders;
    expect(orders).toHaveLength(2);
    expect(orders[0].item).toBe('BURGER-COMBO');
    expect(orders[0].lineRole).toBe('Combo Header');
    expect(orders[0].bundleOverrides).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ slot_item: 'FRIES', chosen_item: 'ONION-RINGS' }),
      ])
    );
    expect(orders[1].item).toBe('COMBO-UPGRADE-FEE');
    expect(orders[1].lineRole).toBe('Combo Upcharge');
    expect(orders[1].parentLineKey).toBe(orders[0].reservationLineKey);
    expect(orders[1].price).toBe(3000);

    // Removing parent atomically removes its combo upcharge child row
    await usePOSStore.getState().removeFromOrder(orders[0].uniqueId!);
    expect(usePOSStore.getState().activeOrders).toHaveLength(0);
  });

  it('maintains 4-locale parity (en/fr/es/ar) with zero vendor terms for all modifier & combo keys', () => {
    const REQUIRED_KEYS: Record<string, string> = {
      'product_dialog.required_badge': '',
      'product_dialog.optional_badge': '',
      'product_dialog.included_badge': '',
      'product_dialog.sold_out_badge': '',
      'product_dialog.regular_prefix': '',
      'product_dialog.prefix_strip_label': '',
      'product_dialog.select_required_prompt': '',
      'product_dialog.combo_choices': '',
      'product_dialog.modifiers': '',
      'product_dialog.selection_count_required': '{{min}},{{selected}}',
      'product_dialog.selection_count_range': '{{max}},{{selected}}',
      'cart.modify': '',
    };
    const FORBIDDEN = /\b(?:erpnext|frappe|ury|kamra|huf|heykoala|tridz|buildsuite)\b/i;

    for (const [lang, locale] of [
      ['en', en],
      ['fr', fr],
      ['es', es],
      ['ar', ar],
    ] as const) {
      for (const [key, expectedPlaceholders] of Object.entries(REQUIRED_KEYS)) {
        const value = lookup(locale as Tree, key);
        expect(value?.trim(), `${lang}:${key}`).toBeTruthy();
        expect(placeholders(value!), `${lang}:${key}`).toBe(expectedPlaceholders);
        expect(value!, `${lang}:${key} white-label`).not.toMatch(FORBIDDEN);
      }
    }
  });
});
