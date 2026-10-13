import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick, reactive } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { setImmediate as flushPromises } from 'node:timers/promises';
import Menu from '../src/components/Menu.vue';
import Cart from '../src/components/Cart.vue';
import Table from '../src/components/Table.vue';
import NotificationModal from '../src/components/NotificationModal.vue';
import { useMenuStore } from '../src/stores/Menu.js';
import { useTableStore } from '../src/stores/Table.js';

const LEAVE_PROMPT = 'You have unsent items. Leave without sending them?';

const dependencies = vi.hoisted(() => ({ pushes: [] }));
vi.mock('../src/router', () => ({
  default: {
    push: (path) => {
      dependencies.pushes.push(path);
      return Promise.resolve();
    },
  },
}));
vi.mock('../src/stores/Auth.js', () => ({ useAuthStore: () => dependencies.auth }));
vi.mock('../src/stores/invoiceData.js', () => ({ useInvoiceDataStore: () => dependencies.invoice }));
vi.mock('../src/stores/Customer.js', () => ({ useCustomerStore: () => dependencies.customer }));
vi.mock('../src/stores/recentOrder.js', () => ({ usetoggleRecentOrder: () => dependencies.recent }));
vi.mock('../src/stores/frappeSdk.js', () => ({
  default: { url: 'http://localhost', db: () => dependencies.db, call: () => dependencies.call },
}));

let app;
let modalApp;
let host;
let menu;
let table;
let invoice;
const menuRows = [
  { item: 'Water 500ml', item_name: 'Water 500ml', rate: 1500, special_dish: 0, disabled: 0, item_image: null, course: 'Drinks', course_label: 'Drinks' },
  { item: 'Beef Pilau', item_name: 'Beef Pilau', rate: 18000, special_dish: 0, disabled: 0, item_image: null, course: 'Mains', course_label: 'Mains' },
  { item: 'Chapati', item_name: 'Chapati', rate: 2000, special_dish: 0, disabled: 0, item_image: null, course: 'Sides', course_label: 'Sides' },
];

beforeEach(async () => {
  vi.useFakeTimers();
  vi.spyOn(window, 'scrollTo').mockImplementation(() => {});
  vi.stubGlobal('matchMedia', () => ({ matches: false }));
  dependencies.pushes.length = 0;
  dependencies.auth = reactive({
    cashier: false,
    viewItemImage: true,
    removeTableOrderItem: 1,
    hasAccess: false,
    restrictTableOrder: false,
    sessionUser: 'waiter-test',
  });
  dependencies.invoice = reactive({
    currency: 'UGX',
    posProfile: 'Salama POS',
    warehouse: 'Stores - SAL',
    showUpdateButtton: true,
    invoiceUpdating: false,
    kotPrinting: false,
    enableKotReprint: false,
    isPrinting: false,
    invoiceNumber: '',
    waiter: 'waiter-test',
    cashier: '',
    tableAttention: 15,
  });
  dependencies.customer = reactive({
    numberOfPax: '',
    search: '',
    customerFavouriteItems: '',
    newCustomerMobileNo: '',
    validateInput() {},
  });
  dependencies.recent = reactive({
    editPrintedInvoice: 0,
    restaurantTable: false,
    draftInvoice: '',
    invoicePrinted: 0,
    pastOrderdItem: [],
    modifiedTime: '',
  });
  invoice = {
    items: [],
    name: null,
    invoice_printed: 0,
    waiter: 'waiter-test',
    customer: null,
    no_of_pax: null,
    custom_comments: '',
    modified: '',
    grand_total: 0,
    mobile_number: '',
  };
  dependencies.db = {
    getDocList: () => Promise.resolve([{ name: 'Drinks' }, { name: 'Mains' }, { name: 'Sides' }]),
  };
  dependencies.call = {
    get: async (method) => {
      if (method === 'ury.ury_pos.api.getRestaurantMenu') {
        return { message: { items: structuredClone(menuRows), name: 'Salama Main Menu' } };
      }
      if (method === 'ury.ury.doctype.ury_order.ury_order.get_order_invoice') {
        return { message: structuredClone(invoice) };
      }
      throw new Error('Unexpected endpoint: ' + method);
    },
  };
  setActivePinia(createPinia());
  menu = useMenuStore();
  table = useTableStore();
  table.selectedRoom = 'Main Dining';
  table.rooms = [{ name: 'Main Dining' }];
  await table.getMenu();
  await flushPromises();
  document.body.append(host = document.createElement('div'));
});

afterEach(() => {
  app?.unmount();
  modalApp?.unmount();
  app = undefined;
  modalApp = undefined;
  document.body.replaceChildren();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function show(component) {
  app?.unmount();
  app = createApp(component);
  app.mount(host);
}

function mountPrompt() {
  modalApp?.unmount();
  const modalHost = document.createElement('div');
  document.body.append(modalHost);
  modalApp = createApp(NotificationModal);
  modalApp.mount(modalHost);
}

async function openTable(name) {
  table.addToSelectedTables({ name, occupied: 0, is_take_away: 0 });
  await flushPromises();
  show(Menu);
}

async function add(name) {
  const heading = [...host.querySelectorAll('h2')].find((node) => node.textContent.trim() === name);
  expect(heading).toBeDefined();
  const button = [...heading.parentElement.querySelectorAll('button')].find((node) => node.textContent.trim() === 'ADD +');
  expect(button).toBeDefined();
  button.click();
  await nextTick();
}

function showFloor(rows) {
  table.tables = rows;
  show(Table);
  mountPrompt();
}

function viewOrder(name) {
  const button = host.querySelector(`button[aria-label="View order for ${name}"]`);
  expect(button).not.toBeNull();
  expect(button.disabled).toBe(false);
  button.click();
}

function cartSnapshot() {
  return menu.cart.map((row) => ({ item: row.item, qty: row.qty }));
}

async function loadUnchangedRecentOrder() {
  dependencies.auth.cashier = true;
  const { usetoggleRecentOrder } = await vi.importActual('../src/stores/recentOrder.js');
  const recent = usetoggleRecentOrder();
  table.recentOrders = recent;
  menu.recentOrders = recent;
  dependencies.recent = recent;
  const originalGet = dependencies.call.get;
  dependencies.call.get = async (method) => {
    if (method === 'ury.ury_pos.api.getPosInvoiceItems') return { message: [[], []] };
    if (method === 'frappe.client.get') {
      return {
        message: {
          ...structuredClone(invoice),
          name: 'POS-INV-6',
          restaurant_table: 'T-06',
          order_type: 'Dine In',
          items: [{ item_code: 'Beef Pilau', item_name: 'Beef Pilau', qty: 2, rate: 18000, comment: '' }],
        },
      };
    }
    if (method === 'ury.ury_pos.api.get_select_field_options') return { message: [{ name: 'Dine In' }] };
    return originalGet(method);
  };
  await recent.viewRecentOrder({
    name: 'POS-INV-6',
    restaurant_table: 'T-06',
    order_type: 'Dine In',
    posting_date: '2026-10-13',
    invoice_printed: 0,
  });
  await flushPromises();
  await recent.editOrder();
  await flushPromises();
  return recent;
}

function clickChoice(label) {
  const button = [...document.body.querySelectorAll('button')].find((node) => node.textContent.trim() === label);
  expect(button).toBeDefined();
  button.click();
}

describe('View order asks before leaving unsent items', () => {
  it('asks when View order would leave unsent items, and Cancel keeps the cart intact', async () => {
    await openTable('T-05');
    await add('Water 500ml');
    expect(cartSnapshot()).toEqual([{ item: 'Water 500ml', qty: 1 }]);
    const pushesBefore = [...dependencies.pushes];
    showFloor([{ name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' }]);
    viewOrder('T-05');
    await nextTick();
    expect(document.body.textContent).toContain(LEAVE_PROMPT);
    expect(cartSnapshot()).toEqual([{ item: 'Water 500ml', qty: 1 }]);
    clickChoice('No');
    await nextTick();
    expect(cartSnapshot()).toEqual([{ item: 'Water 500ml', qty: 1 }]);
    expect(table.selectedTable).toBe('T-05');
    expect(dependencies.pushes).toEqual(pushesBefore);
    expect(document.body.textContent).not.toContain(LEAVE_PROMPT);
    show(Cart);
    expect(host.textContent).toContain('Water 500ml');
    expect(host.querySelector('input[name="qty_input_cart"]').value).toBe('1');
    expect(host.textContent).not.toContain('Nothing to show here');
  });

  it('resets and reloads the table when the waiter confirms leaving unsent items', async () => {
    await openTable('T-05');
    await add('Water 500ml');
    invoice.name = 'POS-INV-5';
    invoice.waiter = 'waiter-test';
    invoice.items = [{
      item_code: 'Beef Pilau',
      item_name: 'Beef Pilau',
      qty: 2,
      rate: 18000,
      comment: '',
    }];
    showFloor([{ name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' }]);
    viewOrder('T-05');
    await nextTick();
    expect(document.body.textContent).toContain(LEAVE_PROMPT);
    expect(cartSnapshot()).toEqual([{ item: 'Water 500ml', qty: 1 }]);
    clickChoice('Yes');
    await flushPromises();
    show(Cart);
    expect(cartSnapshot()).toEqual([{ item: 'Beef Pilau', qty: 2 }]);
    expect(host.textContent).toContain('Beef Pilau');
    expect(host.textContent).not.toContain('Water 500ml');
    expect(host.querySelector('input[name="qty_input_cart"]').value).toBe('2');
    expect(table.selectedTable).toBe('T-05');
    expect(dependencies.pushes).toContain('/Cart');
  });

  it('does not ask when the cart has no unsent items', async () => {
    invoice.name = 'POS-INV-5';
    invoice.waiter = 'waiter-test';
    invoice.items = [{
      item_code: 'Water 500ml',
      item_name: 'Water 500ml',
      qty: 2,
      rate: 1500,
      comment: '',
    }];
    await openTable('T-05');
    expect(cartSnapshot()).toEqual([{ item: 'Water 500ml', qty: 2 }]);
    showFloor([{ name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' }]);
    viewOrder('T-05');
    await flushPromises();
    expect(document.body.textContent).not.toContain(LEAVE_PROMPT);
    show(Cart);
    expect(cartSnapshot()).toEqual([{ item: 'Water 500ml', qty: 2 }]);
    expect(host.querySelector('input[name="qty_input_cart"]').value).toBe('2');
    expect(host.textContent).toContain('Water 500ml');
    expect(dependencies.pushes).toContain('/Cart');
  });

  it('does not ask when an unchanged saved order was loaded through Recent Orders', async () => {
    await openTable('T-05');
    const recent = await loadUnchangedRecentOrder();
    expect(cartSnapshot()).toEqual([{ item: 'Beef Pilau', qty: 2 }]);
    expect(table.previousOrderdItem).toEqual([]);
    expect(recent.pastOrderdItem.map((row) => ({ item: row.item_code, qty: row.qty }))).toEqual([
      { item: 'Beef Pilau', qty: 2 },
    ]);
    showFloor([{ name: 'T-06', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' }]);
    viewOrder('T-06');
    await nextTick();
    expect(document.body.textContent).not.toContain(LEAVE_PROMPT);
  });

  it('asks when a line comment changes and the quantity stays the same', async () => {
    invoice.name = 'POS-INV-5';
    invoice.items = [{
      item_code: 'Water 500ml',
      item_name: 'Water 500ml',
      qty: 2,
      rate: 1500,
      comment: '',
    }];
    await openTable('T-05');
    menu.showModal(menu.cart[0]);
    menu.itemComments = 'No ice';
    menu.addToCartAndUpdateQty();
    expect(menu.cart[0].comment).toBe('No ice');
    expect(table.previousOrderdItem[0].comment).toBe('');
    expect(cartSnapshot()).toEqual([{ item: 'Water 500ml', qty: 2 }]);
    showFloor([{ name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' }]);
    viewOrder('T-05');
    await flushPromises();
    expect(document.body.textContent).toContain(LEAVE_PROMPT);
    expect(menu.cart[0].comment).toBe('No ice');
    expect(cartSnapshot()).toEqual([{ item: 'Water 500ml', qty: 2 }]);
  });
});
