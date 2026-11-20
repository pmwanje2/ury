import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { readFile } from "node:fs/promises";
import test from "node:test";
import * as Vue from "vue";
import { compile } from "@vue/compiler-dom";
import { parse } from "@vue/compiler-sfc";
import { renderToString } from "@vue/server-renderer";

// Call waiter: the kitchen and bar boards show the same guest request banner as
// the till until the floor acknowledges it (Oracle Simphony: guest requests
// alert every revenue-centre device until acknowledged). Real kot.vue script
// and template; only the API, socket, timers and audio are replaced.
const source = await readFile(new URL("./kot.vue", import.meta.url), "utf8");
const script = source.split("<script>")[1].split("</script>")[0];
const helpers = {
  ...await import("./kitchen-actions.js"),
  ...await import("./station-print.js").catch(() => ({})),
};
const SR = "ury.ury.api.service_requests";
const settle = () => new Promise(setImmediate);

function board({ open = [], settings = { sound: 1, escalate_minutes: 3 } } = {}) {
  const requests = [], emitted = [], chimes = [], intervals = new Map();
  const state = { open, settings };
  const socket = new EventEmitter();
  socket.connected = true;
  const fire = socket.emit.bind(socket);
  socket.emit = (...args) => { emitted.push(args); return fire(...args); };
  let timerId = 0;
  const call = {
    post: async (method, args) => { requests.push({ method, args }); return {}; },
    get: async (method, args) => {
      requests.push({ method, args });
      if (method === `${SR}.list_open_service_requests`) return { message: structuredClone(state.open) };
      if (method === `${SR}.get_service_request_alert_settings`) return { message: state.settings };
      return { message: {
        Branch: "Branch A", kot_alert_time: 9999, audio_alert: 0, daily_order_number: 0,
        server_time: "2026-11-20 14:58:00", KOT: [],
      } };
    },
  };
  const component = new Function(
    "FrappeApp", "window", "navigator", "localStorage", "setTimeout", "clearTimeout", "fetch", "io", "console",
    "document", "Audio", "setInterval", "clearInterval",
    ...Object.keys(helpers),
    script.replace(/^import .*;\s*$/gm, "").replace("export default", "return"),
  )(
    class { call() { return call; } auth() { return { getLoggedInUser: async () => "cook@example.test" }; } },
    { location: { hostname: "test", protocol: "https:", port: "", origin: "https://test" },
      addEventListener() {}, removeEventListener() {} },
    { onLine: true }, { getItem: () => null, setItem() {}, removeItem() {} },
    () => 0, () => {},
    async () => ({ json: async () => ({ message: { site_name: "test" } }) }),
    () => socket, { log() {}, error() {} },
    { addEventListener() {}, removeEventListener() {} },
    class { constructor(path) { this.path = path; } play() { chimes.push(this.path); return Promise.resolve(); } },
    (callback, delay) => { intervals.set(++timerId, { callback, delay }); return timerId; },
    id => intervals.delete(id), ...Object.values(helpers),
  );
  const mounted = { ...component.data(), production: "Kitchen" };
  for (const [name, method] of Object.entries(component.methods)) mounted[name] = method.bind(mounted);
  mounted.masonryLoading = () => {};
  const html = () => {
    const { code } = compile(parse(source).descriptor.template.content, { mode: "function", prefixIdentifiers: true });
    const render = new Function("Vue", code)(Vue);
    return renderToString(Vue.createSSRApp({ data: () => mounted, computed: component.computed, render }));
  };
  const tick = async delay => {
    for (const timer of [...intervals.values()].filter(t => t.delay === delay)) await timer.callback();
    await settle();
  };
  return {
    state, socket, requests, emitted, chimes, intervals, mounted, html, tick,
    async start() { await settle(); component.mounted.call(mounted); await settle(); await settle(); },
    stop() { component.beforeUnmount.call(mounted); },
    reconnect() { socket.connected = false; fire("disconnect", "transport close"); socket.connected = true; fire("connect"); },
  };
}

const callWaiter = (overrides = {}) => ({
  name: "SR-1", table: "T-2", request_type: "Assistance", status: "Open", requested_at: "2026-11-20 14:56:00",
  ...overrides,
});
const banner = markup => markup.match(/<div[^>]*data-testid="service-request-alert"[\s\S]*?<\/div>\s*<\/div>/)?.[0] ?? "";

test("the board shows a full-width call-waiter banner with table, type and minutes waiting", async t => {
  const env = board({ open: [callWaiter()] });
  await env.start();
  t.after(() => env.stop());
  assert.ok(env.requests.some(r => r.method === `${SR}.list_open_service_requests` && r.args.branch === "Branch A"),
    "The kitchen board never reads open guest requests for its branch");
  const markup = await env.html();
  assert.match(markup, /data-testid="service-request-alert"/, "No call-waiter banner on the kitchen board");
  assert.match(markup, /role="alert"/);
  assert.match(banner(markup), /inset-x-0|w-full/);
  assert.match(banner(markup), /T-2/);
  assert.match(banner(markup), /Call waiter/);
  assert.match(banner(markup), /2 min/);
});

test("realtime: the board joins the permission-checked room and refreshes on its own branch only", async t => {
  const env = board();
  await env.start();
  t.after(() => env.stop());
  assert.ok(env.emitted.some(([event, doctype]) => event === "doctype_subscribe" && doctype === "URY Service Request"),
    "The board does not subscribe to service-request events");
  assert.doesNotMatch(await env.html(), /data-testid="service-request-alert"/);

  const reads = () => env.requests.filter(r => r.method === `${SR}.list_open_service_requests`).length;
  const before = reads();
  env.socket.emit("ury_service_request", { name: "SR-X", table: "X-1", branch: "Branch B", status: "Open" });
  await settle();
  assert.equal(reads(), before, "Another branch's request must not refresh this board");

  env.state.open = [callWaiter({ name: "SR-9", table: "T-5", request_type: "Bill" })];
  env.socket.emit("ury_service_request", { name: "SR-9", table: "T-5", branch: "Branch A", status: "Open" });
  await settle(); await settle();
  assert.match(banner(await env.html()), /T-5/);
});

test("the banner clears once the floor acknowledges the request", async t => {
  const env = board({ open: [callWaiter()] });
  await env.start();
  t.after(() => env.stop());
  assert.match(await env.html(), /data-testid="service-request-alert"/);
  env.state.open = [callWaiter({ status: "Acknowledged" })];
  env.socket.emit("ury_service_request", { name: "SR-1", table: "T-2", branch: "Branch A", status: "Acknowledged" });
  await settle(); await settle();
  assert.doesNotMatch(await env.html(), /data-testid="service-request-alert"/);
});

test("the poll stays as the fallback, and a reconnect re-joins the room", async t => {
  const env = board();
  await env.start();
  t.after(() => env.stop());
  assert.ok([...env.intervals.values()].some(timer => timer.delay === 15000), "No 15 s fallback poll");
  env.state.open = [callWaiter({ table: "T-3" })];
  await env.tick(15000);
  assert.match(banner(await env.html()), /T-3/);
  const joins = () => env.emitted.filter(([event]) => event === "doctype_subscribe").length;
  const before = joins();
  env.reconnect();
  await settle();
  assert.ok(joins() > before, "After a reconnect the board must re-join the service-request room");
});

test("the chime repeats while a request is open, never when the profile turns sound off", async t => {
  const on = board({ open: [callWaiter()] });
  await on.start();
  t.after(() => on.stop());
  const first = on.chimes.length;
  assert.ok(first > 0, "No chime for an open guest request");
  await on.tick(10000);
  assert.ok(on.chimes.length > first, "The chime does not repeat");

  const off = board({ open: [callWaiter()], settings: { sound: 0, escalate_minutes: 3 } });
  await off.start();
  t.after(() => off.stop());
  await off.tick(10000);
  assert.equal(off.chimes.length, 0);
});

test("a request older than the profile minutes turns to the escalation colour", async t => {
  const env = board({ open: [callWaiter({ requested_at: "2026-11-20 14:54:00" })] });
  await env.start();
  t.after(() => env.stop());
  assert.match(banner(await env.html()), /data-escalated="true"/);
  const calm = board({ open: [callWaiter({ requested_at: "2026-11-20 14:54:00" })], settings: { sound: 1, escalate_minutes: 5 } });
  await calm.start();
  t.after(() => calm.stop());
  assert.doesNotMatch(banner(await calm.html()), /data-escalated="true"/);
});
