import { render, screen, within } from '@testing-library/react';
import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { formatCurrency } from '@ury/core';
import { initI18n } from '../src/i18n';
import MenuCard from '../src/components/MenuCard';
import OrderTypeSelect from '../src/components/OrderTypeSelect';
import { ORDER_TYPES, orderTypeLabel } from '../src/data/order-types';
import type { ItemAvailability } from '../src/lib/availability-api';
import { installTailwindUtilities, TRUNCATES, truncationStyle } from './helpers/tailwind-css';

// U-4: the register's 384px cart panel must show every order type without
// clipping one past its edge, and a menu card must not squeeze its category
// against the price. Assertions read real compiled Tailwind styles.

vi.mock('../src/store/pos-store', () => ({
  usePOSStore: () => ({
    selectedOrderType: 'Take Away', setSelectedOrderType: vi.fn(), selectedTable: null,
    tableOrder: null, posProfile: {}, isUpdatingOrder: false, activeOrders: [],
  }),
}));
vi.mock('../src/store/root-store', () => ({
  useRootStore: () => ({ user: { name: 'cashier@example.com', roles: ['URY Cashier'] } }),
}));
vi.mock('../src/components/TableSelectionDialog', () => ({ default: () => null }));

beforeAll(async () => {
  await installTailwindUtilities([
    'src/components/OrderTypeSelect.tsx',
    'src/components/MenuCard.tsx',
    '../packages/ui/src/components/button.tsx',
    '../packages/ui/src/components/badge.tsx',
    '../packages/ui/src/components/menu-item-card.tsx',
  ]);
});

beforeEach(async () => {
  await initI18n('en');
});

function isFlexRow(element: Element) {
  const style = getComputedStyle(element);
  return style.display.endsWith('flex') && (style.flexDirection === '' || style.flexDirection.startsWith('row'));
}

function commonAncestor(a: Element, b: Element) {
  let node: Element | null = a;
  while (node && !node.contains(b)) node = node.parentElement;
  return node!;
}

describe('U-4 register order-type selector', () => {
  it('lays every order type out in a two-column grid instead of a scrolling row', () => {
    render(<OrderTypeSelect />);
    const buttons = ORDER_TYPES.map(({ value }) => screen.getByRole('button', { name: orderTypeLabel(value) }));
    expect(buttons.length).toBeGreaterThanOrEqual(4);
    const group = buttons[0].parentElement!;
    for (const button of buttons) expect(button.parentElement).toBe(group);
    const style = getComputedStyle(group);
    expect(style.display).toBe('grid');
    expect(style.gridTemplateColumns).toBe('repeat(2, minmax(0, 1fr))');
    expect(style.overflowX).not.toBe('auto');
  });

  it('fits each order type to its cell and clips a long label instead of the panel', () => {
    render(<OrderTypeSelect />);
    for (const { value } of ORDER_TYPES) {
      const label = orderTypeLabel(value);
      const button = screen.getByRole('button', { name: label });
      expect({ width: getComputedStyle(button).width, minWidth: getComputedStyle(button).minWidth })
        .toEqual({ width: '100%', minWidth: '0px' });
      expect(button.getAttribute('title')).toBe(label);
      expect(truncationStyle(within(button).getByText(label))).toEqual(TRUNCATES);
    }
  });
});

const entry = (fields: Partial<ItemAvailability>): ItemAvailability => ({
  item_code: 'ITEM-TEA', sellable: true, available_qty: null, reason_code: 'AVAILABLE',
  stock_policy_mode: 'NATIVE', source: 'CATALOG', production_policy: null, company: 'C1', branch: 'B1',
  department: null, production_unit: null, warehouse: null, plan_qty: null, plan_remaining: null,
  fg_available: null, max_producible: null, blocking_component: null, as_of: '', ...fields,
});

describe('U-4 menu item card category', () => {
  it.each(['Beverages', 'Desserts', 'Main Course', 'Starters'])(
    'gives the %s category its own row, away from the price and the stock chip',
    (course) => {
      render(<MenuCard id="1" item="ITEM-TEA" name="House Special" price={12500} item_image={null}
        course={course} availability={entry({ available_qty: 3 })} />);
      const category = screen.getByText(course);
      const price = screen.getByText(formatCurrency(12500));
      const chip = screen.getByText('3 left');
      expect(isFlexRow(commonAncestor(category, price))).toBe(false);
      expect(isFlexRow(commonAncestor(category, chip))).toBe(false);
      expect(getComputedStyle(category).maxWidth).toBe('100%');
      expect(truncationStyle(category)).toMatchObject({ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' });
      expect(category.getAttribute('title')).toBe(course);
      expect(getComputedStyle(price).flexShrink).toBe('0');
    },
  );

  it('keeps the category row in place when an item has no category, so prices line up', () => {
    const { container } = render(<MenuCard id="1" item="ITEM-TEA" name="House Special" price={12500}
      item_image={null} availability={null} />);
    const price = screen.getByText(formatCurrency(12500));
    const heading = container.querySelector('h3')!;
    const between = Array.from(heading.parentElement!.children);
    expect(between.indexOf(price.parentElement!) - between.indexOf(heading)).toBe(2);
  });
});
