import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

// M03 on the kitchen display (ported from the fork's source pins in
// test_till_clarity.py, converted to a mounted vitest spec): every ticket
// shows the order's guest label, and the order-delayed banner names it.

const server = vi.hoisted(() => ({ kots: [] }));
const socket = vi.hoisted(() => {
  const handlers = new Map();
  return {
    handlers, connected: true,
    on(event, fn) { handlers.set(event, [...(handlers.get(event) ?? []), fn]); },
    off(event, fn) { handlers.set(event, (handlers.get(event) ?? []).filter(h => h !== fn)); },
    emit() {},
    fire(event, payload) { (handlers.get(event) ?? []).forEach(fn => fn(payload)); },
  };
});

vi.mock("socket.io-client", () => ({ default: () => socket }));
vi.mock("masonry-layout", () => ({ default: class { layout() {} } }));
vi.mock("frappe-js-sdk", () => ({
  FrappeApp: class {
    auth() { return { getLoggedInUser: async () => "cook@example.test" }; }
    call() {
      return {
        post: async () => ({}),
        get: async (method) => {
          if (method === "ury.ury.api.ury_kot_display.kot_list") {
            return { message: {
              Branch: "Branch A", kot_alert_time: 9999, audio_alert: 0, daily_order_number: 0,
              server_time: "2026-11-20 14:58:00", KOT: structuredClone(server.kots),
            } };
          }
          return { message: [] };
        },
      };
    }
  },
}));

const ticket = (overrides = {}) => ({
  name: "KOT-1", invoice: "POS-INV-00042", production: "Kitchen", restaurant_table: "T-4",
  table_takeaway: 0, type: "New Order", time: "14:55:00", order_no: "42", user: "waiter",
  customer_name: "Walk In", is_aggregator: 0, comments: "", custom_guest_label: null,
  kot_items: [{ name: "row-1", item: "Fish", item_name: "Fish", quantity: 1, qty: 1, serve_priority: 1 }],
  ...overrides,
});

let wrapper;
async function board() {
  const { default: Kot } = await import("./kot.vue");
  wrapper = mount(Kot, { props: { production: "Kitchen" }, global: { mocks: { __: (s, ...a) => a.reduce((t, v, i) => t.replace(`{${i}}`, v), s) } } });
  for (let i = 0; i < 6; i++) await flushPromises();
  return wrapper;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(new Date("2026-11-20T14:58:00"));
  vi.stubGlobal("fetch", async () => ({ json: async () => ({ message: { site_name: "test" } }) }));
  vi.stubGlobal("Audio", class { play() { return Promise.resolve(); } });
  server.kots = [];
  socket.handlers.clear();
});
afterEach(() => {
  wrapper?.unmount();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Mosaic guest label", () => {
  it("shows the guest label on a dine-in ticket, not only on aggregator orders", async () => {
    server.kots = [ticket({ custom_guest_label: "Susan" })];
    await board();
    const label = wrapper.find(".kot-guest");
    expect(label.exists(), "no guest label on the ticket").toBe(true);
    expect(label.text()).toBe("Susan");
  });

  it("a ticket without a label is unchanged", async () => {
    server.kots = [ticket()];
    await board();
    expect(wrapper.find(".kot-guest").exists()).toBe(false);
    expect(wrapper.text()).not.toContain("Guest");
  });

  it("the order-delayed banner names the guest", async () => {
    server.kots = [ticket({ custom_guest_label: "Susan" })];
    await board();
    socket.fire("kot_error_Branch A_Kitchen", { kot: "KOT-1", invoice: "POS-INV-00042" });
    await flushPromises();
    expect(wrapper.text()).toContain("Order Delayed");
    expect(wrapper.text()).toContain("Guest: Susan");
  });
});
