import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import MobileQRLayout from '../../self-order/src/layouts/MobileQRLayout';
import { initI18n } from '../../self-order/src/i18n';
import type { OrderingContext } from '../../self-order/src/lib/api';

const context: OrderingContext = {
  session: 'synthetic-session', restaurant: 'Restaurant A', table: 'T-07', source: 'QR Table', layout: 'Mobile',
  capabilities: { product_detail_enabled: true, show_item_images: false, show_item_descriptions: false,
    item_notes_enabled: false, request_bill_enabled: true, customer_payment_enabled: false,
    payment_link_enabled: false, pay_at_counter_enabled: true, add_to_running_table_enabled: true },
};
const order = { invoice: 'POS-1', items: [{ item_code: 'Soup', item_name: 'Soup', qty: 1, rate: 12000, amount: 12000 }],
  grand_total: 12000, billed: false };
const menu = {
  items: [{ item: 'Soup', item_name: 'Soup', rate: 12000, disabled: 0, special_dish: 0, item_image: null, course: null, course_label: null }],
  modified_time: null, name: 'Menu A',
};
let root: Root, container: HTMLDivElement;
// Bill-bearing guest reads travel as POST bodies; guest actions use `writes`.
let writes: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  sessionStorage.clear();
  writes = vi.fn().mockResolvedValue({ message: { status: 'Requested', request: 'REQ-1' } });
  vi.spyOn(call, 'get').mockImplementation(async (path: string) => { throw Error(`Unexpected GET ${path}`); });
  vi.spyOn(call, 'post').mockImplementation(async (path: string, args?: Record<string, unknown>) => {
    if (path.endsWith('get_customer_menu')) return { message: menu };
    if (/\.get_[a-z_]+$/.test(path)) return { message: order };
    return writes(path, args);
  });
  container = document.createElement('div'); document.body.appendChild(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount()); container.remove();
  vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals();
});
async function render(ctx = context) { await act(async () => root.render(<MobileQRLayout initialContext={ctx} />)); }
function button(label: string) { return [...container.querySelectorAll('button')].find(b => b.textContent?.trim() === label); }
async function click(label: string) {
  expect(button(label), `Missing guest action: ${label}`).toBeDefined();
  await act(async () => button(label)!.click());
}
async function addSoup() {
  const item = [...container.querySelectorAll('button')].find(b => b.textContent?.startsWith('Soup'));
  expect(item).toBeDefined();
  await act(async () => item!.click());
}

describe('guest service requests', () => {
  it('sends only the verified session and confirms staff notification with a cooldown', async () => {
    await render(); vi.useFakeTimers(); await click('Call waiter');
    expect(call.post).toHaveBeenCalledWith('ury.ury.api.self_ordering.request_assistance', { session: 'synthetic-session' });
    expect(container.textContent).toContain('Waiter requested — staff notified');
    expect(button('Call waiter')?.disabled).toBe(true);
    await act(async () => vi.advanceTimersByTime(60000));
    expect(button('Call waiter')?.disabled).toBe(false);
  });
  it('blocks repeat taps while the request is in flight', async () => {
    writes.mockImplementation(() => new Promise(() => {}));
    await render(); await click('Call waiter'); await click('Calling waiter…');
    expect(writes).toHaveBeenCalledTimes(1);
  });
  it('keeps the cooldown when Start Over starts a new ordering session', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    await render(); await click('Call waiter'); await click('Start Over');
    expect(button('Call waiter')?.disabled).toBe(true);
  });
  it('does not confirm failed requests and allows a retry', async () => {
    writes.mockRejectedValue({ _server_messages: JSON.stringify([JSON.stringify({ message: 'Session expired. Rescan.' })]) });
    await render(); await click('Call waiter');
    expect(container.textContent).toContain('Session expired. Rescan.');
    expect(container.textContent).not.toContain('staff notified');
    expect(button('Call waiter')?.disabled).toBe(false);
  });
  it.each([ { ...context, source: 'QR Pickup', table: null },
    { ...context, capabilities: { ...context.capabilities, request_bill_enabled: false } } ])('hides assistance when unavailable', async ctx => {
    await render(ctx); expect(button('Call waiter')).toBeUndefined();
  });
});

const serverMessages = (code: string, text: string) => ({ _server_messages: JSON.stringify([
  JSON.stringify({ message: `[${code}] ${text}` }),
  JSON.stringify({ message: `Error while placing order: [${code}] ${text}` })]) });
const exceptionOnly = (code: string, text: string) => ({
  exception: `frappe.exceptions.ValidationError: Error while placing order: [${code}] ${text}` });
const SERVER_TEXT = {
  'SAL-QR-WAITER-OPENS-TABLE': 'Please ask a waiter to open your table first.',
  'SAL-QR-BILL-PRINTED': 'Your bill is printed. Please ask a waiter to add anything else.',
  'SAL-QR-QTY': 'Please choose whole quantities between 1 and 20.',
} as const;
const PLACE = { en: 'Place Order', sw: 'Weka oda', lg: 'Teeka oda' } as const;
const NEXT_STEPS = {
  en: {
    'SAL-QR-WAITER-OPENS-TABLE': 'Your table is not open for orders yet. Please ask a member of staff to open it, then place your order again. Your basket is kept.',
    'SAL-QR-BILL-PRINTED': 'Your bill has already been printed, so new items cannot be added here. Please ask a waiter to add them for you. Your basket is kept.',
    'SAL-QR-QTY': 'Please choose whole quantities between 1 and 20.',
  },
  sw: {
    'SAL-QR-WAITER-OPENS-TABLE': 'Meza yako bado haijafunguliwa kwa oda. Tafadhali mwombe mhudumu aifungue, kisha weka oda yako tena. Kikapu chako kimehifadhiwa.',
    'SAL-QR-BILL-PRINTED': 'Bili yako tayari imechapishwa, kwa hivyo bidhaa mpya haziwezi kuongezwa hapa. Tafadhali mwombe mhudumu akuongezee. Kikapu chako kimehifadhiwa.',
    'SAL-QR-QTY': 'Tafadhali chagua idadi kamili kati ya 1 na 20.',
  },
  lg: {
    'SAL-QR-WAITER-OPENS-TABLE': 'Emmeeza yo tennaggulwawo kuteekako oda. Nsaba osabe omu ku baweereza agiggulewo, oluvannyuma oddemu oteeke oda yo. Ebiri mu kikapu kyo bikyaliwo.',
    'SAL-QR-BILL-PRINTED': "Bili yo yamaze okukubibwa, n'olwekyo ebintu ebipya tebiyinza kwongerwako wano. Nsaba osabe omuweereza abikwongerereko. Ebiri mu kikapu kyo bikyaliwo.",
    'SAL-QR-QTY': 'Nsaba olonde obungi obujjuvu wakati wa 1 ne 20.',
  },
} as const;
const cartBar = () => container.querySelector('.fixed')?.textContent ?? '';

describe('ERP refusal text', () => {
  afterEach(async () => { await initI18n('en', { setCookie() {} }); });
  const cases = (['en', 'sw', 'lg'] as const).flatMap(language =>
    (Object.keys(SERVER_TEXT) as (keyof typeof SERVER_TEXT)[]).flatMap(code => [
      { language, code, shape: '_server_messages', refusal: serverMessages(code, SERVER_TEXT[code]) },
      { language, code, shape: 'exception', refusal: exceptionOnly(code, SERVER_TEXT[code]) },
    ]));
  it.each(cases)('shows the $language next step for $code ($shape), preserving the cart', async ({ language, code, refusal }) => {
    await initI18n(language, { setCookie() {} });
    writes.mockRejectedValue(refusal);
    await render(); await addSoup();
    await click(PLACE[language]);
    const shown = NEXT_STEPS[language][code];
    expect(container.textContent).toContain(shown);
    expect(container.textContent!.split(shown)).toHaveLength(2);
    expect(container.textContent).not.toContain(`[${code}]`);
    expect(container.textContent).not.toContain('Error while placing order');
    if (language !== 'en' || code !== 'SAL-QR-QTY') expect(container.textContent).not.toContain(SERVER_TEXT[code]);
    expect(container.textContent).not.toContain('Could not place the order');
    expect(button(PLACE[language])).toBeDefined();
    expect(cartBar()).toContain('Soup');
  });
  const CALL_WAITER = { en: 'Call waiter', sw: 'Ita mhudumu', lg: 'Yita omuweereza' } as const;
  it.each(['en', 'sw', 'lg'] as const)('only names on-screen actions when an unopened %s table is refused', async language => {
    // SAL-QR-WAITER-OPENS-TABLE means no staff-opened order exists, so the guest's bill is empty.
    vi.mocked(call.post).mockImplementation(async (path: string, args?: Record<string, unknown>) => {
      if (path.endsWith('get_customer_menu')) return { message: menu };
      if (/\.get_[a-z_]+$/.test(path)) return { message: { invoice: null, items: [], grand_total: 0, billed: false } };
      return writes(path, args);
    });
    await initI18n(language, { setCookie() {} });
    writes.mockRejectedValue(serverMessages('SAL-QR-WAITER-OPENS-TABLE', SERVER_TEXT['SAL-QR-WAITER-OPENS-TABLE']));
    await render(); await addSoup();
    await click(PLACE[language]);
    expect(container.textContent).toContain(NEXT_STEPS[language]['SAL-QR-WAITER-OPENS-TABLE']);
    expect(button(CALL_WAITER[language])).toBeUndefined();
    expect(container.textContent).not.toContain(CALL_WAITER[language]);
    expect(cartBar()).toContain('Soup');
  });
  it.each([
    ['en', serverMessages('SAL-QR-UNLISTED', 'Please ask staff about this table.')],
    ['sw', serverMessages('SAL-QR-UNLISTED', 'Please ask staff about this table.')],
    ['lg', exceptionOnly('SAL-QR-UNLISTED', 'Please ask staff about this table.')],
  ] as const)('keeps the server text without its code for an unknown %s refusal id', async (language, refusal) => {
    await initI18n(language, { setCookie() {} });
    writes.mockRejectedValue(refusal);
    await render(); await addSoup();
    await click(PLACE[language]);
    expect(container.textContent!.split('Please ask staff about this table.')).toHaveLength(2);
    expect(container.textContent).not.toContain('[SAL-QR-UNLISTED]');
    expect(container.textContent).not.toContain('Error while placing order');
    expect(button(PLACE[language])).toBeDefined();
    expect(cartBar()).toContain('Soup');
  });
  it('renders server HTML as harmless text only', async () => {
    writes.mockRejectedValue({ _server_messages: JSON.stringify([JSON.stringify({ message: '[DENIED] <b>Please ask staff.</b><img src=x onerror=evil()>' })]) });
    await render(); await addSoup(); await click('Place Order');
    expect(container.textContent).toContain('Please ask staff.');
    expect(container.querySelector('b, [onerror], script')).toBeNull();
  });
  it('keeps the friendly fallback for transport failures without ERP text', async () => {
    writes.mockRejectedValue({ httpStatus: 503 });
    await render(); await addSoup(); await click('Place Order');
    expect(container.textContent).toContain('Could not place the order. Please try again.');
  });
});

describe('guest read transport', () => {
  it('loads the menu and running bill as POST bodies, never as GET query strings', async () => {
    await render();
    expect(container.textContent).toContain('Soup');
    expect(call.post).toHaveBeenCalledWith('ury.ury.api.self_ordering.get_customer_menu', { session: 'synthetic-session' });
    expect(call.post).toHaveBeenCalledWith('ury.ury.api.self_ordering.get_customer_order', { session: 'synthetic-session' });
    expect(call.get).not.toHaveBeenCalled();
  });
});

it('renders the guest boot brand name and logo without interpreting HTML', async () => {
  const browser = window as Window & { frappe?: { boot: object } };
  browser.frappe = { boot: { ury_brand: { name: 'Salama <script>evil()</script>', logo: '/files/salama.png' } } };
  try {
    await render();
    expect(container.querySelector('header')?.textContent).toContain('Salama <script>evil()</script>');
    expect(container.querySelector('header img')?.getAttribute('src')).toBe('/files/salama.png');
    expect(container.querySelector('script')).toBeNull();
  } finally { delete browser.frappe; }
});

it('renders an optional logo even when no display name is configured', async () => {
  const browser = window as Window & { frappe?: { boot: object } };
  browser.frappe = { boot: { ury_brand: { name: null, logo: '/files/logo-only.png' } } };
  try {
    await render();
    expect(container.querySelector('header img')?.getAttribute('src')).toBe('/files/logo-only.png');
  } finally { delete browser.frappe; }
});
