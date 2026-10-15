// @vitest-environment jsdom
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { afterAll, afterEach, beforeEach, expect, test } from "vitest";
import { compile } from "@vue/compiler-dom";
import { parse } from "@vue/compiler-sfc";
import * as Vue from "vue";
import postcss from "postcss";
import tailwindcss from "tailwindcss";
import * as kitchenHelpers from "./kitchen-actions.js";
const helpers = { ...kitchenHelpers, ...await import("./station-print.js").catch(() => ({})) };

const source = await readFile(join(dirname(fileURLToPath(import.meta.url)), "kot.vue"), "utf8");
const { descriptor } = parse(source);
const { code } = compile(descriptor.template.content, { mode: "function", prefixIdentifiers: true });
const render = new Function("Vue", code)(Vue);
const css = await postcss([tailwindcss({
  content: [{ raw: source, extension: "vue" }], corePlugins: { preflight: false },
})]).process("@tailwind utilities;\n" + descriptor.styles[0].content, { from: undefined });
const style = document.createElement("style");
style.textContent = css.css;
document.head.append(style);
afterAll(() => style.remove());

let host, app, vm;
beforeEach(async () => {
  // Replace only external API/socket boundaries; render the native template,
  // data and computed properties. Browser checks cover lifecycle and layout.
  const component = new Function("FrappeApp", "io", "fetch", ...Object.keys(helpers),
    descriptor.script.content.replace(/^import .*;\s*$/gm, "").replace("export default", "return"),
  )(
    class { call() { return { post: async () => ({}) }; } },
    () => ({ connected: true, on() {}, off() {} }),
    async () => ({ json: async () => ({ message: { site_name: "synthetic.test" } }) }),
    ...Object.values(helpers),
  );
  host = document.createElement("div");
  document.body.append(host);
  app = Vue.createApp({ ...component, render, mounted: undefined, beforeUnmount: undefined }, { production: "Kitchen" });
  vm = app.mount(host);
  vm.masonryLoading = () => {};
  vm.loadingKots = false;
  vm.socketStale = false;
  vm.kot = [{
    name: "KOT-041", production: "Kitchen", type: "Order", tableortakeaway: "T-07",
    user: "Synthetic waiter", invoice: "SYNTHETIC-041", timeRemaining: "0 : 18", color: "bg-white",
    comments: "Priority: serve together. Guest has a severe peanut allergy.",
    kot_items: [{ name: "ROW-1", item_name: "Grilled tilapia", qty: 4, quantity: 4, serve_priority: 1,
      comments: "NO PEANUTS. Severe allergy — use separate utensils and clean preparation surface." }],
  }];
  await Vue.nextTick();
});
afterEach(() => { app.unmount(); host.remove(); });

const luminance = color => {
  const channels = color.match(/[\d.]+/g).slice(0, 3).map(Number).map(value => {
    const normalized = value / 255;
    return normalized <= 0.04045 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4;
  });
  return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
};

test.each(["NO PEANUTS. Severe allergy", "Sauce on the side"])(
  "both note levels visibly label and highlight special requests: %s", async message => {
    vm.kot[0].comments = message;
    vm.kot[0].kot_items[0].comments = message;
    await Vue.nextTick();
    const notes = [...host.querySelectorAll(".kot-note")];
    expect(notes).toHaveLength(2);
    for (const note of notes) {
      expect(note.textContent).toContain(message);
      expect(note.textContent).toMatch(/special request/i);
      const computed = window.getComputedStyle(note);
      expect(Number(computed.fontWeight)).toBeGreaterThanOrEqual(600);
      expect(Number.parseFloat(computed.fontSize)).toBeGreaterThanOrEqual(18);
      expect(computed.backgroundColor).not.toBe("rgba(0, 0, 0, 0)");
      expect(Number.parseFloat(computed.borderLeftWidth)).toBeGreaterThanOrEqual(3);
      expect(computed.borderLeftStyle).toBe("solid");
      const foreground = luminance(computed.color), background = luminance(computed.backgroundColor);
      expect((Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)).toBeGreaterThanOrEqual(7);
    }
  },
);

test.each([
  ["0 : 0", "0 min"], ["0 : 5", "5 min"], ["0 : 18", "18 min"], ["0 : 59", "59 min"],
  ["1 : 0", "1 h 00 min"], ["1 : 5", "1 h 05 min"], ["1 : 18", "1 h 18 min"], ["2 : 9", "2 h 09 min"],
])("elapsed %s renders as %s without changing the native timer state", async (value, label) => {
  vm.kot[0].timeRemaining = value;
  await Vue.nextTick();
  expect(host.querySelector(".kot-age").textContent.trim()).toBe(label);
  expect(vm.kot[0].timeRemaining).toBe(value);
});
