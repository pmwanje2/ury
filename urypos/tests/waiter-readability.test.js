import { afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest';
import tableSource from '../src/components/Table.vue?raw';
import menuSource from '../src/components/Menu.vue?raw';
import postcss from 'postcss';
import tailwindcss from 'tailwindcss';
import { createApp, nextTick, reactive } from 'vue';
import Table from '../src/components/Table.vue';
import Menu from '../src/components/Menu.vue';

const state = vi.hoisted(() => ({}));
vi.mock('../src/stores/Table.js', () => ({ useTableStore: () => state.table }));
vi.mock('../src/stores/Auth.js', () => ({ useAuthStore: () => state.auth }));
vi.mock('../src/stores/Menu.js', () => ({ useMenuStore: () => state.menu }));
vi.mock('../src/stores/invoiceData.js', () => ({ useInvoiceDataStore: () => state.invoiceData }));
vi.mock('../src/stores/recentOrder.js', () => ({ usetoggleRecentOrder: () => state.recentOrders }));
vi.mock('../src/stores/Customer.js', () => ({ useCustomerStore: () => ({}) }));
vi.mock('../src/stores/frappeSdk.js', () => ({ default: {} }));

let app;
let host;
beforeAll(async () => {
  // Real native utility CSS: assert behaviour, not any particular class string.
  const content = [tableSource, menuSource].map(raw => ({ raw, extension: 'vue' }));
  const { css } = await postcss([tailwindcss({ content })]).process('@tailwind base; @tailwind utilities;', { from: undefined });
  document.head.append(Object.assign(document.createElement('style'), { textContent: css }));
});
beforeEach(() => {
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
  const tables = [
    { name: 'Table 01', occupied: 0, state: 'Free' },
    { name: 'Garden Terrace 02', occupied: 1, state: 'Occupied', age: '0:08' },
    { name: 'Table 03', occupied: 1, state: 'Attention', age: '0:27' },
    { name: 'Table 04', occupied: 1, state: 'Active', age: '0:18' },
  ];
  state.auth = reactive({ cashier: false, viewItemImage: true, restrictTableOrder: false, removeTableOrderItem: 1 });
  state.table = reactive({ tables, filteredTables: tables, rooms: [{ name: 'Dining' }], selectedRoom: 'Dining',
    serviceRequests: [], serviceRequestsLoading: false, serviceRequestsUnavailable: false, fetchServiceRequests: vi.fn(),
    getBadgeText: row => row.state, getBadgeType: row => ({ Free: 'green', Occupied: 'yellow', Attention: 'red', Active: 'default' })[row.state],
    getTimeDifference: row => row.age, addToSelectedTables: vi.fn(), routeToMenu: vi.fn(), routeToCart: vi.fn(),
    handleRoomChange: vi.fn(), toggleTableTypeSwitch: vi.fn(), toggleDropdown: vi.fn(),
  });
  state.invoiceData = reactive({ currency: 'UGX', billing: vi.fn() });
  state.recentOrders = reactive({ editPrintedInvoice: 0, restaurantTable: false });
  const items = [
    { item: 'CHICKEN', item_name: 'Grilled chicken with garden vegetables and pepper sauce', rate: '125,000', item_image: '/safe-synthetic.svg' },
    { item: 'SOUP', item_name: 'Cream of mushroom soup', rate: '18,000', qty: 2 },
  ];
  state.menu = reactive({ items, paginatedItems: items, selectedOrderType: 'Dine In', course: [], pageNumbers: [1], currentPage: 1, totalPages: 1,
    getFullImagePath: path => path, itemNameExtract: name => name.slice(0, 2),
    addToCart: vi.fn(), incrementItemQuantity: vi.fn(), decrementItemQuantity: vi.fn(), showModal: vi.fn(),
  });
});
afterEach(() => { app?.unmount(); host?.remove(); vi.restoreAllMocks(); });
function mount(component) {
  document.body.append(host = document.createElement('div'));
  (app = createApp(component)).mount(host);
}
function buttonNamed(text) { return [...host.querySelectorAll('button')].find(button => button.textContent.includes(text)); }
function pixels(value) { return (parseFloat(value) || 0) * (value.endsWith('rem') ? 16 : 1); }

it('shows table identities, occupied ages and states with touchable native actions', () => {
  mount(Table);
  for (const row of state.table.tables) {
    expect(host.textContent).toContain(row.name);
    expect(host.textContent).toContain(row.state);
    if (row.occupied) expect(host.textContent).toContain(row.age);
  }
  for (const name of host.querySelectorAll('h5')) expect.soft(pixels(getComputedStyle(name).fontSize)).toBeGreaterThanOrEqual(24);
  const age = [...host.querySelectorAll('span')].find(span => span.textContent.trim() === '0:08');
  expect.soft(pixels(getComputedStyle(age).fontSize)).toBeGreaterThanOrEqual(16);
  for (const button of host.querySelectorAll('button')) expect.soft(pixels(getComputedStyle(button).minHeight)).toBeGreaterThanOrEqual(44);
  expect(host.querySelector('button[aria-label="Open menu for Garden Terrace 02"]')).not.toBeNull();
  host.querySelector('button[aria-label="Open menu for Garden Terrace 02"]').click();
  expect(state.table.routeToMenu).toHaveBeenCalledWith(state.table.tables[1]);
  host.querySelector('button[aria-label="View order for Garden Terrace 02"]').click();
  expect(state.table.routeToCart).toHaveBeenCalledWith(state.table.tables[1]);
  buttonNamed('Open Table').click();
  expect(state.table.addToSelectedTables).toHaveBeenCalledWith(state.table.tables[0]);
  buttonNamed('Bill').click();
  expect(state.invoiceData.billing).toHaveBeenCalledWith(state.table.tables[1]);
});
it('prevents restricted table controls from invoking native navigation', async () => {
  state.auth.restrictTableOrder = true;
  mount(Table);
  buttonNamed('Open Table').click();
  for (const button of host.querySelectorAll('button[aria-label^="Open menu"], button[aria-label^="View order"]')) button.click();
  await nextTick();
  expect(state.table.addToSelectedTables).not.toHaveBeenCalled();
  expect(state.table.routeToMenu).not.toHaveBeenCalled();
  expect(state.table.routeToCart).not.toHaveBeenCalled();
});
it('wraps full dish names and replaces missing images with local initials', () => {
  mount(Menu);
  const name = [...host.querySelectorAll('h2')].find(node => node.textContent.trim() === state.menu.items[0].item_name);
  expect(name).toBeDefined();
  expect.soft(getComputedStyle(name).whiteSpace).not.toBe('nowrap');
  expect.soft(getComputedStyle(name).overflow).not.toBe('hidden');
  const images = [...host.querySelectorAll('img')];
  expect.soft(images).toHaveLength(1);
  expect.soft(images[0].alt).toBe(state.menu.items[0].item_name);
  expect(images[0].getAttribute('src')).toBe('/safe-synthetic.svg');
  expect.soft(images.some(image => image.src.includes('dummyimage.com'))).toBe(false);
  expect.soft(host.textContent).toContain('Cr');
  expect(host.textContent).toContain('UGX 125,000');
});
it('keeps menu touch targets at least 44px, native actions and the printed-order guard', async () => {
  mount(Menu);
  const quantityButtons = [...host.querySelectorAll('button')].filter(button => ['-', '+'].includes(button.textContent.trim()));
  expect(quantityButtons).toHaveLength(2);
  for (const control of [buttonNamed('ADD +'), ...quantityButtons, host.querySelector('input[name="qty_input"]')]) expect.soft(pixels(getComputedStyle(control).minHeight)).toBeGreaterThanOrEqual(44);
  for (const button of quantityButtons) expect.soft(pixels(getComputedStyle(button).minWidth)).toBeGreaterThanOrEqual(44);
  buttonNamed('ADD +').click();
  expect(state.menu.addToCart).toHaveBeenCalledWith(state.menu.items[0]);
  expect(state.menu.items[0].showInput).toBe(true);
  [...host.querySelectorAll('button')].find(button => button.textContent.trim() === '+').click();
  [...host.querySelectorAll('button')].find(button => button.textContent.trim() === '-').click();
  host.querySelector('input[name="qty_input"]').click();
  expect(state.menu.incrementItemQuantity).toHaveBeenCalledWith(state.menu.items[1]);
  expect(state.menu.decrementItemQuantity).toHaveBeenCalledWith(state.menu.items[1]);
  expect(state.menu.showModal).toHaveBeenCalledWith(state.menu.items[1]);
  state.menu.decrementItemQuantity.mockClear();
  state.recentOrders.editPrintedInvoice = 1;
  state.auth.removeTableOrderItem = 0;
  await nextTick();
  [...host.querySelectorAll('button')].find(button => button.textContent.trim() === '-').click();
  expect(state.menu.decrementItemQuantity).not.toHaveBeenCalled();
});
