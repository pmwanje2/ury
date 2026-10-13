import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createApp, nextTick, reactive } from 'vue';
import { createPinia, setActivePinia } from 'pinia';
import { setImmediate as flushPromises } from 'node:timers/promises';
import Menu from '../src/components/Menu.vue';
import Cart from '../src/components/Cart.vue';
import Table from '../src/components/Table.vue';
import { useMenuStore } from '../src/stores/Menu.js';
import { useTableStore } from '../src/stores/Table.js';

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
  app = undefined;
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
}

function viewOrder(name) {
  const button = host.querySelector(`button[aria-label="View order for ${name}"]`);
  expect(button).not.toBeNull();
  expect(button.disabled).toBe(false);
  button.click();
}

describe('View order keeps the unsent cart for the same table', () => {
  it.each(['Water 500ml', 'Beef Pilau'])('retains unsent %s when the same table View order control opens Cart', async (name) => {
    await openTable('T-05');
    await add(name);
    expect(menu.cart.map((row) => row.item)).toEqual([name]);
    showFloor([{ name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' }]);
    viewOrder('T-05');
    await flushPromises();
    show(Cart);
    expect(host.textContent).not.toContain('Nothing to show here');
    expect(host.textContent).toContain(name);
    expect(host.querySelector('input[name="qty_input_cart"]').value).toBe('1');
    expect(menu.cart.map((row) => row.item)).toEqual([name]);
    expect(dependencies.pushes).toContain('/Cart');
  });

  it('clears the unsent cart when View order opens a different table', async () => {
    await openTable('T-05');
    await add('Water 500ml');
    showFloor([
      { name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' },
      { name: 'T-06', occupied: 1, is_take_away: 0, latest_invoice_time: '12:05:00' },
    ]);
    viewOrder('T-06');
    await flushPromises();
    show(Cart);
    expect(menu.cart).toEqual([]);
    expect(host.textContent).toContain('Nothing to show here');
    expect(table.selectedTable).toBe('T-06');
    expect(dependencies.pushes).toContain('/Cart');
  });

  it('still warns when View order opens a different table assigned to another waiter', async () => {
    await openTable('T-05');
    await add('Beef Pilau');
    const alerts = [];
    table.alert.createAlert = (title, message, button) => {
      alerts.push({ title, message, button });
      return new Promise(() => {});
    };
    invoice.name = 'POS-INV-9';
    invoice.waiter = 'other-waiter';
    showFloor([
      { name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' },
      { name: 'T-06', occupied: 1, is_take_away: 0, latest_invoice_time: '12:05:00' },
    ]);
    viewOrder('T-06');
    await flushPromises();
    expect(menu.cart).toEqual([]);
    expect(table.selectedTable).toBe('T-06');
    expect(alerts).toEqual([{
      title: 'Message',
      message: 'Table is assigned to other-waiter',
      button: 'OK',
    }]);
  });

  it('still loads the saved order when View order reopens the same table with an empty cart', async () => {
    await openTable('T-05');
    expect(menu.cart).toEqual([]);
    invoice.items = [{
      item_code: 'Water 500ml',
      item_name: 'Water 500ml',
      qty: 2,
      rate: 1500,
      comment: '',
    }];
    invoice.name = 'POS-INV-5';
    invoice.waiter = 'waiter-test';
    showFloor([{ name: 'T-05', occupied: 1, is_take_away: 0, latest_invoice_time: '12:00:00' }]);
    viewOrder('T-05');
    await flushPromises();
    show(Cart);
    expect(menu.cart.map((row) => row.item)).toEqual(['Water 500ml']);
    expect(host.querySelector('input[name="qty_input_cart"]').value).toBe('2');
    expect(host.textContent).toContain('Water 500ml');
    expect(host.textContent).not.toContain('Nothing to show here');
  });
});
