import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { JSDOM } from "jsdom";
import postcss from "postcss";
import tailwindcss from "tailwindcss";
import { compile } from "@vue/compiler-dom";
import { parse } from "@vue/compiler-sfc";
import * as kitchenHelpers from "./kitchen-actions.js";
const helpers = { ...kitchenHelpers, ...await import("./station-print.js").catch(() => ({})) };

const dom = new JSDOM("<!doctype html><html><head></head><body></body></html>", { url: "https://synthetic.test" });
for (const key of ["window", "document", "navigator", "Element", "HTMLElement", "SVGElement", "localStorage"]) {
  Object.defineProperty(globalThis, key, { value: dom.window[key], configurable: true });
}
const Vue = await import("vue");
const source = await readFile(new URL("./kot.vue", import.meta.url), "utf8");
const css = await postcss([tailwindcss({ content: [{ raw: source, extension: "vue" }], corePlugins: { preflight: false } })])
  .process("@tailwind utilities;\n" + source.split("<style>")[1].split("</style>")[0], { from: undefined });
const style = document.createElement("style");
style.textContent = css.css;
document.head.append(style);
const { code } = compile(parse(source).descriptor.template.content, { mode: "function", prefixIdentifiers: true });
const render = new Function("Vue", code)(Vue);

function fixture(t, type = "Order Modified") {
  const requests = [], timers = new Map();
  let timerId = 0;
  const call = { post: async (method, args) => { requests.push({ method, args }); return {}; } };
  const component = new Function("FrappeApp", "io", "fetch", "setTimeout", "clearTimeout", ...Object.keys(helpers),
    source.split("<script>")[1].split("</script>")[0].replace(/^import .*;\s*$/gm, "").replace("export default", "return"),
  )(class { call() { return call; } }, () => ({ connected: true, on() {}, off() {} }),
    async () => ({ json: async () => ({ message: { site_name: "synthetic.test" } }) }),
    (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; }, id => timers.delete(id), ...Object.values(helpers));
  const ticket = {
    name: "KOT-041", restaurant_table: "T-07", tableortakeaway: "T-07", production: "Kitchen", user: "Synthetic waiter",
    type, invoice: "SYNTHETIC-041", order_no: 41, timeRemaining: "0 : 18", timecolor: "text-red-700", late: true,
    color: "bg-white", isRotated: false,
    comments: "Priority: serve together. Guest has a severe peanut allergy.",
    kot_items: [{ name: "ROW-1", item_name: "Grilled tilapia with garden vegetables and lemon butter", quantity: 4, cancelled_qty: 1,
      qty: type === "Partially cancelled" ? 3 : 4, comments: "NO PEANUTS. Severe allergy — use separate utensils and clean preparation surface.", serve_priority: 1 }],
  };
  const host = document.createElement("div"); document.body.append(host);
  // Lifecycle I/O and Masonry geometry belong to browser/integration checks; keep native template, data, computed and actions here.
  const app = Vue.createApp({ ...component, render, mounted: undefined, beforeUnmount: undefined }, { production: "Kitchen" });
  const vm = app.mount(host);
  vm.kot = [ticket]; vm.loadingKots = false; vm.daily_order_number = 1; vm.socketStale = false;
  vm.masonryLoading = () => {};
  t.after(() => { app.unmount(); host.remove(); });
  return { host, vm, ticket, timers, requests };
}
const pixels = element => {
  assert.ok(element, "Expected ticket text is rendered");
  const size = window.getComputedStyle(element).fontSize;
  return Number.parseFloat(size) * (size.endsWith("rem") ? 16 : 1);
};
const findText = (host, text) => [...host.querySelectorAll("span, p, button, div")].find(element => [...element.childNodes].filter(node => node.nodeType === 3).map(node => node.textContent).join("").trim() === text);

test("kitchen operators see quantity before each dish", async t => {
  const { host, ticket } = fixture(t);
  await Vue.nextTick();
  const dish = findText(host, ticket.kot_items[0].item_name);
  const quantity = findText(host, "4");
  assert.ok(quantity.compareDocumentPosition(dish) & window.Node.DOCUMENT_POSITION_FOLLOWING, "The quantity must precede its dish in reading order");
});

test("long dishes remain readable at kitchen working distance", async t => {
  const { host, ticket } = fixture(t);
  await Vue.nextTick();
  assert.ok(pixels(findText(host, ticket.kot_items[0].item_name)) >= 20, "Dishes need at least 20px type for kitchen working distance");
});

test("critical preparation notes are readable and can wrap in full", async t => {
  const { host, ticket } = fixture(t);
  await Vue.nextTick();
  const note = findText(host, ticket.kot_items[0].comments);
  assert.ok(pixels(note) >= 18, "Critical preparation notes need at least 18px type");
  const computed = window.getComputedStyle(note);
  assert.notEqual(computed.whiteSpace, "nowrap");
  assert.notEqual(computed.textOverflow, "ellipsis");
  assert.notEqual(computed.overflow, "hidden");
});

test("table identity leads the ticket at kitchen working distance", async t => {
  const { host } = fixture(t);
  await Vue.nextTick();
  assert.ok(pixels(findText(host, "T-07")) >= 24, "Table identity needs at least 24px type");
});

test("elapsed time is readable and late state stays explicit", async t => {
  const { host } = fixture(t);
  await Vue.nextTick();
  assert.ok(pixels(findText(host, "18 min")) >= 28, "Elapsed time needs at least 28px type");
  assert.ok(findText(host, "LATE"));
});

test("modified ticket state is explicit in text", async t => {
  const { host } = fixture(t);
  await Vue.nextTick();
  assert.ok(findText(host, "Order Modified"), "Modified tickets must expose their status in text");
});

test("native Serve and Undo are reachable in a touch-sized footer and preserve the five-second window", async t => {
  const { host, vm, timers, requests, ticket } = fixture(t);
  await Vue.nextTick();
  const serve = findText(host, "Serve");
  assert.ok(serve.closest("footer"), "Native ticket actions must be grouped below the preparation details");
  assert.notEqual(window.getComputedStyle(serve).display, "none");
  assert.ok(Number.parseFloat(window.getComputedStyle(serve).minHeight) >= 44, "Touch actions need a 44px minimum target");
  serve.click(); serve.click(); await Vue.nextTick();
  assert.equal(timers.size, 1); assert.equal(requests.length, 0);
  assert.equal([...timers.values()][0].delay, 5000);
  const undo = findText(host, "Undo");
  assert.ok(undo.closest("footer")); undo.click(); await Vue.nextTick();
  assert.equal(timers.size, 0); assert.equal(requests.length, 0);
  assert.equal(vm.kot[0].name, ticket.name);
});

test("partial cancellation retains current and old quantity with native Confirm and failed-action visibility", async t => {
  const { host, vm, ticket, requests } = fixture(t, "Partially cancelled");
  vm.call.post = async (method, args) => { requests.push({ method, args }); throw new Error("Synthetic network unavailable"); };
  await Vue.nextTick();
  assert.ok(findText(host, "3")); assert.ok(findText(host, "[Old Qty = 4]"));
  const confirm = findText(host, "Confirm");
  assert.ok(confirm.closest("footer")); confirm.click(); await Vue.nextTick(); await Vue.nextTick();
  assert.deepEqual(requests, [{ method: "ury.ury.api.ury_kot_display.confirm_cancel_kot", args: { name: "KOT-041" } }]);
  assert.match(host.querySelector('[role="alert"]').textContent, /Synthetic network unavailable/);
  assert.equal(vm.kot[0].name, ticket.name);
});


test("Masonry receives only visible tickets for the selected production unit", async t => {
  const { host, vm, ticket } = fixture(t);
  vm.kot = [
    { ...structuredClone(ticket), name: "BAR-001", tableortakeaway: "BAR-ONLY", production: "Bar" },
    { ...structuredClone(ticket), name: "HIDDEN-001", tableortakeaway: "HIDDEN-TABLE", showDiv: true },
    ticket,
  ];
  await Vue.nextTick();
  const layoutItems = [...host.querySelectorAll(".masonry-item")];
  assert.equal(layoutItems.length, 1, "Other-station and hidden tickets must not reserve Masonry positions");
  assert.match(layoutItems[0].textContent, /T-07/);
  assert.doesNotMatch(host.textContent, /BAR-ONLY|HIDDEN-TABLE/);
});


test("highlighted ticket footer background dismisses the existing header selection", async t => {
  const { host, vm } = fixture(t);
  await Vue.nextTick();
  const header = host.querySelector(".kot-header");
  const footer = host.querySelector("footer");
  header.click(); await Vue.nextTick();
  assert.equal(vm.kot[0].isRotated, true);
  assert.ok(footer.classList.contains("ring-2"));
  footer.click(); await Vue.nextTick();
  assert.equal(vm.kot[0].isRotated, false, "Footer background must dismiss the selected ticket highlight");
  assert.ok(!footer.classList.contains("ring-2"));
  footer.click(); await Vue.nextTick();
  assert.equal(vm.kot[0].isRotated, false, "Unselected footer background must not select a ticket");
});

test("native Confirm button does not bubble into the footer dismiss gesture", async t => {
  const { host, vm, requests } = fixture(t, "Partially cancelled");
  vm.call.post = async (method, args) => { requests.push({ method, args }); throw new Error("Synthetic network unavailable"); };
  await Vue.nextTick();
  host.querySelector(".kot-header").click(); await Vue.nextTick();
  assert.equal(vm.kot[0].isRotated, true);
  findText(host, "Confirm").click(); await Vue.nextTick();
  assert.equal(vm.kot[0].isRotated, true, "Button activation must remain separate from background dismissal");
  assert.deepEqual(requests, [{ method: "ury.ury.api.ury_kot_display.confirm_cancel_kot", args: { name: "KOT-041" } }]);
});

const luminance = color => {
  const channels = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(value => {
    const normalized = value / 255;
    return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
};
test("all preparation and order notes are emphasized at high contrast without classifying their text", async t => {
  const { host, vm } = fixture(t);
  for (const message of ["NO PEANUTS", "Bring sauce separately", "Serve together"]) {
    vm.kot[0].comments = message;
    vm.kot[0].kot_items[0].comments = message;
    await Vue.nextTick();
    const notes = [...host.querySelectorAll(".kot-note")];
    assert.equal(notes.length, 2);
    for (const note of notes) {
      assert.match(note.textContent, new RegExp(message));
      const computed = window.getComputedStyle(note);
      assert.ok(Number(computed.fontWeight) >= 600, "Every note must be emphasized");
      assert.ok((1.05 / (luminance(computed.color) + 0.05)) >= 7, "Every note needs high contrast against the light ticket surface");
    }
  }
});
test("elapsed labels distinguish hours and minutes without changing timing state", async t => {
  const { host, vm } = fixture(t);
  for (const [value, label] of [["0 : 18", "18 min"], ["1 : 18", "1 h 18 min"]]) {
    vm.kot[0].timeRemaining = value;
    await Vue.nextTick();
    assert.ok(findText(host, label));
    assert.equal(vm.kot[0].timeRemaining, value);
  }
});

for (const actionError of ["", "Synthetic network unavailable"]) {
test(`long tickets expose every item before the footer without an inner scroll${actionError ? " during an action error" : ""}`, async t => {
  const { host, vm } = fixture(t);
  vm.actionError = actionError;
  vm.kot[0].kot_items = Array.from({ length: 12 }, (_, index) => ({
    name: `LONG-${index}`, item_name: `Preparation dish ${index + 1}`, qty: 2, quantity: 2,
    comments: `Preparation note ${index + 1}`, serve_priority: index + 1,
  }));
  await Vue.nextTick();
  const card = host.querySelector(".kot-card");
  const body = card.querySelector(".check"), footer = card.querySelector("footer");
  for (const element of [card, body]) {
    const computed = window.getComputedStyle(element);
    assert.ok(["", "none"].includes(computed.maxHeight), "Ticket content must grow rather than hide behind a height cap");
    assert.ok(["", "visible"].includes(computed.overflowY), "Ticket content must not scroll or clip independently of the board");
  }
  assert.equal(body.querySelectorAll(".kot-item").length, 12);
  assert.ok(body.textContent.includes("Preparation note 12"));
  assert.ok(body.compareDocumentPosition(footer) & window.Node.DOCUMENT_POSITION_FOLLOWING);
  assert.equal(window.getComputedStyle(footer).position, "", "Serve must stay in the ticket flow after all items");
});
}

test("Takeaway and order identity do not break inside words beside an hour-long age", async t => {
  const { host, vm } = fixture(t);
  vm.kot[0].tableortakeaway = "Takeaway";
  vm.kot[0].timeRemaining = "1 : 5";
  await Vue.nextTick();
  const identity = host.querySelector(".kot-identity");
  assert.ok(identity.textContent.includes("Takeaway"));
  assert.equal(host.querySelector(".kot-age").textContent.trim(), "1 h 05 min LATE");
  assert.equal(window.getComputedStyle(identity).overflowWrap, "normal", "Table/order identity must wrap only at word boundaries");
  assert.notEqual(window.getComputedStyle(identity).wordBreak, "break-all");
});

for (const type of ["Cancelled", "Partially cancelled"]) {
test(`${type} order and item notes are cancellation remarks, not special requests`, async t => {
  const { host, vm } = fixture(t, type);
  vm.kot[0].comments = "Guest cancelled this order";
  vm.kot[0].kot_items[0].comments = "One portion cancelled";
  await Vue.nextTick();
  const notes = [...host.querySelectorAll(".kot-note")];
  assert.equal(notes.length, 2);
  for (const note of notes) {
    assert.match(note.textContent, /cancellation remarks/i);
    assert.doesNotMatch(note.textContent, /special request/i);
    assert.ok(Number(window.getComputedStyle(note).fontWeight) >= 600);
  }
  assert.ok(host.querySelector("footer").textContent.includes("Confirm"));
});
}
