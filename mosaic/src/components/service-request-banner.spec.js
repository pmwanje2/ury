import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { flushPromises, mount } from "@vue/test-utils";

// Call waiter on the kitchen and bar boards: the same guest-request banner as
// the till, shown until the floor acknowledges it (Oracle Simphony guest-request
// alerts). Only the SDK, socket, site-name fetch and audio are replaced.

const SR = "ury.ury.api.service_requests";
const server = vi.hoisted(() => ({ open: [], settings: { sound: 1, escalate_minutes: 3 }, reads: [] }));
const socket = vi.hoisted(() => {
  const handlers = new Map();
  return {
    handlers, emitted: [], connected: true,
    on(event, fn) { handlers.set(event, [...(handlers.get(event) ?? []), fn]); },
    off(event, fn) { handlers.set(event, (handlers.get(event) ?? []).filter(h => h !== fn)); },
    emit(...args) { this.emitted.push(args); },
    fire(event, payload) { (handlers.get(event) ?? []).forEach(fn => fn(payload)); },
  };
});
const chimes = vi.hoisted(() => []);

vi.mock("socket.io-client", () => ({ default: () => socket }));
vi.mock("masonry-layout", () => ({ default: class { layout() {} } }));
vi.mock("frappe-js-sdk", () => ({
  FrappeApp: class {
    auth() { return { getLoggedInUser: async () => "cook@example.test" }; }
    call() {
      return {
        post: async () => ({}),
        get: async (method, args) => {
          server.reads.push({ method, args });
          if (method === `${SR}.list_open_service_requests`) return { message: structuredClone(server.open) };
          if (method === `${SR}.get_service_request_alert_settings`) return { message: server.settings };
          return { message: {
            Branch: "Branch A", kot_alert_time: 9999, audio_alert: 0, daily_order_number: 0,
            server_time: "2026-11-20 14:58:00", KOT: [],
          } };
        },
      };
    }
  },
}));

const callWaiter = (overrides = {}) => ({
  name: "SR-1", table: "T-2", request_type: "Assistance", status: "Open", requested_at: "2026-11-20 14:56:00",
  ...overrides,
});

let wrapper;
async function board() {
  const { default: Kot } = await import("./kot.vue");
  wrapper = mount(Kot, { props: { production: "Kitchen" }, global: { mocks: { __: (s) => s } } });
  for (let i = 0; i < 6; i++) await flushPromises();
  return wrapper;
}
const banner = () => wrapper.find('[data-testid="service-request-alert"]');

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(new Date("2026-11-20T14:58:00"));
  vi.stubGlobal("fetch", async () => ({ json: async () => ({ message: { site_name: "test" } }) }));
  vi.stubGlobal("Audio", class { constructor(path) { this.path = path; } play() { chimes.push(this.path); return Promise.resolve(); } });
  server.open = [];
  server.settings = { sound: 1, escalate_minutes: 3 };
  server.reads.length = 0;
  socket.handlers.clear();
  socket.emitted.length = 0;
  chimes.length = 0;
});
afterEach(() => {
  wrapper?.unmount();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("Mosaic call-waiter banner", () => {
  it("shows a full-width banner with table, type and minutes waiting", async () => {
    server.open = [callWaiter()];
    await board();
    expect(server.reads.some(r => r.method === `${SR}.list_open_service_requests` && r.args.branch === "Branch A"),
      "The board never reads open guest requests for its branch").toBe(true);
    expect(banner().exists(), "No call-waiter banner on the kitchen board").toBe(true);
    expect(banner().attributes("role")).toBe("alert");
    expect(banner().classes()).toContain("w-full");
    expect(banner().text()).toContain("T-2");
    expect(banner().text()).toContain("Call waiter");
    expect(banner().text()).toContain("2 min");
  });

  it("joins the permission-checked room and refreshes on its own branch only", async () => {
    await board();
    expect(socket.emitted).toContainEqual(["doctype_subscribe", "URY Service Request"]);
    const reads = () => server.reads.filter(r => r.method === `${SR}.list_open_service_requests`).length;
    const before = reads();
    socket.fire("ury_service_request", { name: "SR-X", table: "X-1", branch: "Branch B", status: "Open" });
    await flushPromises();
    expect(reads()).toBe(before);
    server.open = [callWaiter({ name: "SR-9", table: "T-5", request_type: "Bill" })];
    socket.fire("ury_service_request", { name: "SR-9", table: "T-5", branch: "Branch A", status: "Open" });
    await flushPromises(); await flushPromises();
    expect(banner().text()).toContain("T-5");
  });

  it("clears once the floor acknowledges, and polls every 15 s as the fallback", async () => {
    server.open = [callWaiter()];
    await board();
    expect(banner().exists()).toBe(true);
    server.open = [callWaiter({ status: "Acknowledged" })];
    vi.advanceTimersByTime(15000);
    await flushPromises(); await flushPromises();
    expect(banner().exists()).toBe(false);
  });

  it("repeats the chime while open and escalates after the profile minutes", async () => {
    server.open = [callWaiter({ requested_at: "2026-11-20 14:54:00" })];
    await board();
    const first = chimes.length;
    expect(first).toBeGreaterThan(0);
    vi.advanceTimersByTime(10000);
    expect(chimes.length).toBeGreaterThan(first);
    expect(banner().find('[data-escalated="true"]').exists()).toBe(true);
  });

  it("stays silent when the profile turns sound off", async () => {
    server.open = [callWaiter()];
    server.settings = { sound: 0, escalate_minutes: 3 };
    await board();
    vi.advanceTimersByTime(30000);
    expect(chimes.length).toBe(0);
  });
});
