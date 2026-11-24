import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";
import { canRecallKot, recentServedQuery } from "./kitchen-actions.js";
import { createStationPrinter } from "./station-print.js";
import enLocale from "../locales/en.json";
import arLocale from "../locales/ar.json";

const server = vi.hoisted(() => ({
  kots: [],
  isExpo: false,
  recallWindowMinutes: 15,
  expoOrders: [],
  allDaySummary: [],
  stationDeviceConfig: null,
  posts: [],
  kotListCalls: 0,
}));

const socket = vi.hoisted(() => {
  const handlers = new Map();
  return {
    handlers,
    connected: true,
    on(event, fn) { handlers.set(event, [...(handlers.get(event) ?? []), fn]); },
    off(event, fn) { handlers.set(event, (handlers.get(event) ?? []).filter(h => h !== fn)); },
    emit() {},
  };
});

vi.mock("socket.io-client", () => ({ default: () => socket }));
vi.mock("masonry-layout", () => ({ default: class { layout() {} } }));
vi.mock("frappe-js-sdk", () => ({
  FrappeApp: class {
    auth() { return { getLoggedInUser: async () => "chef@example.test" }; }
    call() {
      return {
        post: async (method, args) => {
          server.posts.push({ method, args });
          return { message: { status: "Success" } };
        },
        get: async (method) => {
          if (method === "ury.ury.api.ury_kot_display.kot_list") {
            server.kotListCalls += 1;
            return {
              message: {
                Branch: "Branch A",
                kot_alert_time: 9999,
                audio_alert: 0,
                daily_order_number: 1,
                server_time: "2026-11-24 14:00:00",
                recall_window_minutes: server.recallWindowMinutes,
                is_expo: server.isExpo,
                expo_orders: structuredClone(server.expoOrders),
                all_day_summary: structuredClone(server.allDaySummary),
                station_device_config: server.stationDeviceConfig ? structuredClone(server.stationDeviceConfig) : null,
                KOT: structuredClone(server.kots),
              },
            };
          }
          if (method === "ury.ury.doctype.ury_order.ury_order.get_captain_context") {
            return { message: { roles: ["URY Manager"] } };
          }
          return { message: [] };
        },
      };
    }
  },
}));

const makeKot = (overrides = {}) => ({
  name: "KOT-1",
  invoice: "POS-INV-100",
  production: "Kitchen",
  restaurant_table: "T-4",
  table_takeaway: 0,
  type: "New Order",
  order_status: "Ready For Prepare",
  time: "13:55:00",
  order_no: "100",
  user: "Waiter",
  customer_name: "Walk In",
  is_aggregator: 0,
  comments: "",
  custom_is_rush: 0,
  custom_is_vip: 0,
  kot_items: [
    { name: "r2", item: "CAKE", item_name: "Chocolate Cake", quantity: 1, qty: 1, course: "Dessert", serve_priority: 3, state: "QUEUED" },
    { name: "r1", item: "SOUP", item_name: "Tomato Soup", quantity: 2, qty: 2, course: "Starter", serve_priority: 1, state: "IN_PREPARATION" },
  ],
  ...overrides,
});

let wrapper;
async function mountBoard(production = "Kitchen") {
  const { default: Kot } = await import("./kot.vue");
  wrapper = mount(Kot, {
    props: { production },
    global: {
      mocks: {
        __: (s, ...args) => args.reduce((acc, v, i) => acc.split(`{${i}}`).join(String(v)), s),
      },
    },
  });
  for (let i = 0; i < 6; i++) await flushPromises();
  return wrapper;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(new Date("2026-11-24T14:00:00"));
  vi.stubGlobal("fetch", async () => ({ json: async () => ({ message: { site_name: "test" } }) }));
  vi.stubGlobal("Audio", class { play() { return Promise.resolve(); } });
  server.kots = [];
  server.isExpo = false;
  server.recallWindowMinutes = 15;
  server.expoOrders = [];
  server.allDaySummary = [];
  server.stationDeviceConfig = null;
  server.posts.length = 0;
  server.kotListCalls = 0;
  socket.handlers.clear();
});

afterEach(() => {
  wrapper?.unmount();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("G26: KDS Expo Mode, All-Day Summary, Rush/VIP, Course Grouping & Configurable Recall", () => {
  it("supports configurable recall window minutes in helpers and UI", async () => {
    const served25MinAgo = {
      order_status: "Served",
      creation: "2026-11-24 13:20:00",
      production_time: "15",
    };
    const now = new Date("2026-11-24T14:00:00");
    expect(canRecallKot(served25MinAgo, now, 15)).toBe(false);
    expect(canRecallKot(served25MinAgo, now, 30)).toBe(true);

    const q30 = recentServedQuery("Branch A", "Kitchen", now, 30);
    expect(q30.filters.modified).toEqual([">=", "2026-11-24 13:30:00"]);

    server.recallWindowMinutes = 30;
    server.kots = [makeKot()];
    await mountBoard("Kitchen");
    await wrapper.find("button").trigger("click"); // Ensure reactivity; open recall panel
    wrapper.vm.showRecentServed = true;
    await flushPromises();
    expect(wrapper.text()).toContain("Recall is available for 30 minutes after serving.");
    expect(wrapper.text()).not.toContain("Recall is available for 15 minutes after serving.");
  });

  it("renders live All-Day summary bar, Rush/VIP priority pinning, and course headers", async () => {
    server.kots = [
      makeKot({ name: "KOT-NORMAL", invoice: "POS-INV-101", order_no: "101", custom_is_rush: 0, custom_is_vip: 0 }),
      makeKot({
        name: "KOT-RUSH",
        invoice: "POS-INV-102",
        order_no: "102",
        restaurant_table: "T-9",
        custom_is_rush: 1,
        custom_is_vip: 1,
        kot_items: [
          { name: "r3", item: "SOUP", item_name: "Tomato Soup", quantity: 3, qty: 3, course: "Starter", serve_priority: 1, state: "QUEUED" },
        ],
      }),
    ];
    await mountBoard("Kitchen");

    // All-Day summary aggregates Tomato Soup (2 + 3 = 5) and Chocolate Cake (1)
    const allDay = wrapper.find('[data-testid="all-day-summary"]');
    expect(allDay.exists(), "All-Day prep summary bar missing").toBe(true);
    expect(allDay.text()).toContain("Tomato Soup");
    expect(allDay.text()).toContain("5");

    // Rush/VIP ticket pinned first with visible badges
    const cards = wrapper.findAll(".kot-ticket");
    expect(cards.length).toBe(2);
    expect(cards[0].text()).toContain("T-9");
    expect(cards[0].find('[data-testid="kot-rush-badge"]').exists()).toBe(true);
    expect(cards[0].find('[data-testid="kot-vip-badge"]').exists()).toBe(true);

    // Course headers rendered in serve_priority order (Starter before Dessert)
    const headers = cards[1].findAll('[data-testid="kot-course-header"]').map(el => el.text().trim());
    expect(headers).toEqual(["Starter", "Dessert"]);

    // 100% Maximus white-label
    expect(wrapper.text()).not.toMatch(/\b(?:URY|HUF|Kamra|Tridz)\b/i);
  });

  it("renders Expo mode showing multi-station readiness per check and final Expo Bump", async () => {
    server.isExpo = true;
    server.kots = [
      makeKot({ name: "KOT-HOT", invoice: "POS-INV-200", production: "Hot Kitchen", kot_items: [{ name: "i1", item_name: "Steak", quantity: 1, qty: 1, course: "Main", serve_priority: 2, state: "READY" }] }),
      makeKot({ name: "KOT-BAR", invoice: "POS-INV-200", production: "Bar", kot_items: [{ name: "i2", item_name: "Juice", quantity: 2, qty: 2, course: "Drinks", serve_priority: 1, state: "IN_PREPARATION" }] }),
    ];
    await mountBoard("Expo");

    const expoBoard = wrapper.find('[data-testid="expo-board"]');
    expect(expoBoard.exists(), "Expo mode board missing").toBe(true);
    const expoCards = wrapper.findAll('[data-testid="expo-order-card"]');
    expect(expoCards.length).toBe(1);
    const chips = expoCards[0].findAll('[data-testid="expo-station-chip"]').map(c => c.text());
    expect(chips.some(c => c.includes("Hot Kitchen") && c.includes("Ready"))).toBe(true);
    expect(chips.some(c => c.includes("Bar") && c.includes("Preparing"))).toBe(true);

    const bumpBtn = expoCards[0].find('[data-testid="expo-bump-btn"]');
    expect(bumpBtn.exists()).toBe(true);
    await bumpBtn.trigger("click");
    await flushPromises();
    expect(server.posts.some(p => p.method === "ury.ury.api.ury_kot_display.expo_bump_order" && p.args.invoice === "POS-INV-200")).toBe(true);
  });

  it("G26/G27: starts live station heartbeat interval on mount, cleans up on unmount, and subscribes to kot_priority realtime events", async () => {
    server.stationDeviceConfig = {
      production_unit: "Kitchen",
      auto_failover_enabled: true,
      heartbeat_timeout_seconds: 120,
    };
    server.kots = [makeKot({ name: "KOT-1", production: "Kitchen", custom_is_rush: 0 })];
    await mountBoard("Kitchen");

    const heartbeats = () => server.posts.filter(p => p.method === "ury.ury.api.ury_kot_display.record_station_heartbeat");
    // Immediate heartbeat on mount
    expect(heartbeats().length).toBe(1);
    expect(heartbeats()[0].args).toEqual({ production_unit: "Kitchen" });

    // Periodic 30s heartbeat
    vi.advanceTimersByTime(30000);
    await flushPromises();
    expect(heartbeats().length).toBe(2);

    // Realtime priority subscription for station board
    const stationPrioHandlers = socket.handlers.get("kot_priority_Branch A_Kitchen") ?? [];
    expect(stationPrioHandlers.length).toBeGreaterThan(0);

    const callsBefore = server.kotListCalls;
    server.kots = [makeKot({ name: "KOT-1", production: "Kitchen", custom_is_rush: 1, custom_is_vip: 1 })];
    stationPrioHandlers[0]({ invoice: "POS-INV-100", kot: "KOT-1", custom_is_rush: 1, custom_is_vip: 1 });
    for (let i = 0; i < 4; i++) await flushPromises();
    expect(server.kotListCalls).toBeGreaterThan(callsBefore);
    expect(wrapper.find('[data-testid="kot-rush-badge"]').exists()).toBe(true);

    // Cleanup on unmount stops heartbeats
    const countBeforeUnmount = heartbeats().length;
    wrapper.unmount();
    vi.advanceTimersByTime(60000);
    await flushPromises();
    expect(heartbeats().length).toBe(countBeforeUnmount);

    // Expo board also subscribes to branch-wide kot_priority_{branch}
    socket.handlers.clear();
    server.isExpo = true;
    server.kots = [makeKot({ name: "KOT-HOT", production: "Hot Kitchen" })];
    await mountBoard("Expo");
    expect((socket.handlers.get("kot_priority_Branch A") ?? []).length).toBeGreaterThan(0);
  });

  it("G28: displays editable backup network host/port hydrated from server and saves a backup queue name without conflict", async () => {
    localStorage.clear();
    server.stationDeviceConfig = {
      production_unit: "Kitchen",
      enabled: true,
      printer: "",
      host: "192.0.2.10",
      port: 9100,
      backupPrinter: "",
      backupHost: "192.0.2.20",
      backupPort: 9101,
      printFormat: "Restaurant Kitchen Ticket 80mm",
      pageWidth: 576,
      commandMode: "standard",
    };
    server.kots = [makeKot()];
    await mountBoard("Kitchen");

    await wrapper.vm.openPrintSettings();
    await flushPromises();

    // Backup host & port inputs MUST be visible and populated from server config
    const backupHostInput = wrapper.find('[data-testid="backup-host-input"]');
    const backupPortInput = wrapper.find('[data-testid="backup-port-input"]');
    expect(backupHostInput.exists(), "Backup printer host input missing").toBe(true);
    expect(backupPortInput.exists(), "Backup printer port input missing").toBe(true);
    expect(backupHostInput.element.value).toBe("192.0.2.20");
    expect(backupPortInput.element.value).toBe("9101");

    // Switching to a backup printer queue name must clear backupHost and save cleanly without conflict
    const backupPrinterInput = wrapper.find('[data-testid="backup-printer-input"]');
    expect(backupPrinterInput.exists()).toBe(true);
    await backupPrinterInput.setValue("KITCHEN-BAK");
    await wrapper.find("form").trigger("submit");
    await flushPromises();

    expect(wrapper.vm.stationSettingsError).toBe("");
    expect(wrapper.vm.stationSettings.backupPrinter).toBe("KITCHEN-BAK");
    expect(wrapper.vm.stationSettings.backupHost).toBe("");
  });
});

describe("G28: Station backup printer failover & server-synced device config", () => {
  it("falls back to backup printer when primary fails and logs [KOT-FAILOVER-PRINTED]", async () => {
    const printedTargets = [];
    const comments = [];
    const call = {
      get: async (method) => {
        if (method === "frappe.www.printview.get_html_and_style") return { message: { html: "<div>Ticket</div>" } };
        if (method === "ury.ury.api.ury_print.qz_certificate") return { message: "-----BEGIN CERTIFICATE-----\nTEST" };
        return { message: {} };
      },
      post: async (method, args) => {
        if (method === "ury.ury.api.ury_print.signature_promise") return { message: "sig" };
        if (method === "frappe.desk.form.utils.add_comment") comments.push(args.content);
        return { message: {} };
      },
    };
    const qz = {
      security: { setCertificatePromise() {}, setSignatureAlgorithm() {}, setSignaturePromise() {} },
      websocket: { isActive: () => true, connect: async () => {} },
      configs: { create: target => target },
      printers: { find: async () => ["KITCHEN-PRI", "KITCHEN-BAK"] },
      print: async (target) => {
        printedTargets.push(target);
        if (target === "KITCHEN-PRI") throw new Error('Cannot find printer with name "KITCHEN-PRI"');
      },
    };
    const store = new Map();
    const printer = createStationPrinter({
      production: "Kitchen",
      user: "chef@example.test",
      call,
      qz,
      storage: {
        getItem: k => store.get(k) ?? null,
        setItem: (k, v) => store.set(k, String(v)),
      },
      serverSettings: {
        enabled: true,
        enabledAt: "2026-11-24T13:00:00.000Z",
        printer: "KITCHEN-PRI",
        backupPrinter: "KITCHEN-BAK",
        printFormat: "Restaurant Kitchen Ticket 80mm",
      },
    });

    const outcome = await printer.process({
      name: "KOT-FAILOVER-1",
      production: "Kitchen",
      creation: "2026-11-24T13:55:00.000Z",
    });

    expect(outcome.printed).toBe(true);
    expect(outcome.failoverUsed).toBe(true);
    expect(printedTargets).toEqual(["KITCHEN-PRI", "KITCHEN-BAK"]);
    expect(comments).toContain("[KOT-FAILOVER-PRINTED] KITCHEN-BAK");
  });
});

describe("4-locale i18n parity for Mosaic (en/fr/es/ar)", () => {
  it("ships complete fr and es locale catalogs with matching placeholders alongside ar and en", () => {
    const localesDir = resolve(dirname(fileURLToPath(import.meta.url)), "../locales");
    const frPath = resolve(localesDir, "fr.json");
    const esPath = resolve(localesDir, "es.json");
    expect(existsSync(frPath), "mosaic/src/locales/fr.json missing").toBe(true);
    expect(existsSync(esPath), "mosaic/src/locales/es.json missing").toBe(true);
    const frLocale = JSON.parse(readFileSync(frPath, "utf8"));
    const esLocale = JSON.parse(readFileSync(esPath, "utf8"));
    expect(enLocale).toEqual({});

    const newKeys = [
      "All-Day",
      "Expo Station",
      "Expo Bump",
      "RUSH",
      "VIP",
      "Backup printer",
      "Recall is available for {0} minutes after serving.",
    ];
    for (const [name, catalog] of [["ar", arLocale], ["fr", frLocale], ["es", esLocale]]) {
      for (const key of newKeys) {
        expect(catalog[key], `${name} missing key "${key}"`).toBeTruthy();
        const expectedPh = (key.match(/\{\d+\}/g) || []).sort().join(",");
        const actualPh = (String(catalog[key]).match(/\{\d+\}/g) || []).sort().join(",");
        expect(actualPh, `${name} placeholder mismatch on "${key}"`).toBe(expectedPh);
        expect(String(catalog[key])).not.toMatch(/\b(?:URY|HUF|Kamra|Tridz)\b/i);
      }
      for (const key of Object.keys(arLocale)) {
        expect(catalog[key], `${name} missing existing key "${key}"`).toBeTruthy();
      }
    }
  });
});
