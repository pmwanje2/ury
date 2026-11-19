import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import * as Vue from "vue";
import { compile } from "@vue/compiler-dom";
import { parse } from "@vue/compiler-sfc";
import { renderToString } from "@vue/server-renderer";

const source = await readFile(new URL("./kot.vue", import.meta.url), "utf8");
const stationModule = await import("./station-print.js").catch(() => null);
const kitchenActions = await import("./kitchen-actions.js");

function requireStationModule() {
  assert.ok(stationModule, "Mosaic station printing is not implemented");
  return stationModule;
}

function storage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: key => values.has(key) ? values.get(key) : null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key),
  };
}

function qzDouble({ print = async () => {}, active = true, installed = ["KITCHEN-RAW", "BAR-RAW"] } = {}) {
  const calls = { configs: [], prints: [], security: {}, finds: [] };
  const qz = {
    security: {
      setCertificatePromise: callback => { calls.security.certificate = callback; },
      setSignatureAlgorithm: algorithm => { calls.security.algorithm = algorithm; },
      setSignaturePromise: callback => { calls.security.signature = callback; },
    },
    websocket: {
      isActive: () => active,
      connect: async () => { calls.connected = true; },
    },
    configs: {
      create: config => { calls.configs.push(config); return config; },
    },
    printers: {
      find: async (...args) => { calls.finds.push(args); return installed; },
    },
    print: async (config, data) => {
      calls.prints.push({ config, data });
      return print(config, data);
    },
  };
  return { qz, calls };
}

function ticket(name, production = "Kitchen", creation = "2026-10-01T12:01:00.000Z") {
  return {
    name, production, creation, type: "New Order", invoice: `INV-${name}`,
    tableortakeaway: "T-01", order_no: 1, kot_items: [],
  };
}

function callDouble({ html = "<div>Kitchen ticket</div>", signature = "signed" } = {}) {
  const calls = [];
  return {
    calls,
    get: async (method, args) => {
      calls.push({ method, args });
      if (method === "frappe.www.printview.get_html_and_style") return { message: { html } };
      if (method === "ury.ury.api.ury_print.qz_certificate") return { message: "qz-cert.pem" };
      return { message: {} };
    },
    post: async (method, args) => {
      calls.push({ method, args });
      if (method === "ury.ury.api.ury_print.signature_promise") return { message: signature };
      return { message: {} };
    },
  };
}

function settings(overrides = {}) {
  return {
    enabled: true,
    enabledAt: "2026-10-01T12:00:00.000Z",
    host: "192.0.2.10",
    port: 9100,
    printFormat: "Salama Kitchen Ticket 80mm",
    ...overrides,
  };
}

async function makePrinter(overrides = {}) {
  const call = overrides.call || callDouble();
  const qzDoubleValue = overrides.qzDouble || qzDouble();
  const printer = requireStationModule().createStationPrinter({
    production: "Kitchen",
    user: "chef@example.test",
    call,
    qz: qzDoubleValue.qz,
    storage: overrides.storage || storage({
      "ury:mosaic:station-print:Kitchen:settings": JSON.stringify(settings()),
    }),
    fetch: overrides.fetch || (async () => ({ ok: true, text: async () => "CERTIFICATE" })),
    document: overrides.document || { createElement: () => ({}) },
    ...overrides,
  });
  return { printer, call, qz: qzDoubleValue, storage: overrides.storage };
}

test("prints one new KOT for this unit and ignores duplicates, other units, and pre-enabledAt tickets", async () => {
  const { printer, qz } = await makePrinter();

  const results = await Promise.all([
    printer.process(ticket("NEW")),
    printer.process(ticket("NEW")),
    printer.process(ticket("BAR", "Bar")),
    printer.process(ticket("OLD", "Kitchen", "2026-10-01T11:59:00.000Z")),
  ]);

  assert.equal(results.filter(result => result.printed).length, 1);
  assert.equal(qz.calls.prints.length, 1);
  assert.deepEqual(printer.failed(), []);
});

test("QZ failure writes a failed comment while the caller can keep the ticket displayed", async () => {
  const call = callDouble();
  const { printer } = await makePrinter({
    call,
    qzDouble: qzDouble({ print: async () => { throw new Error("QZ is unavailable"); } }),
  });

  const result = await printer.process(ticket("FAIL"));

  assert.deepEqual(result, { printed: false, code: "QZ_UNAVAILABLE" });
  assert.deepEqual(printer.failed(), ["FAIL"]);
  assert.ok(call.calls.some(({ method, args }) => method === "frappe.desk.form.utils.add_comment" && args.content === "[KOT-PRINT-FAILED] QZ_UNAVAILABLE"));
});

test("an eight-second printer timeout writes PRINTER_TIMEOUT and does not reject the display update", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { printer, call } = await makePrinter({
    qzDouble: qzDouble({ print: () => new Promise(() => {}) }),
  });
  const pending = printer.process(ticket("TIMEOUT"));
  await new Promise(setImmediate);
  t.mock.timers.tick(8000);

  assert.deepEqual(await pending, { printed: false, code: "PRINTER_TIMEOUT" });
  assert.ok(call.calls.some(({ method, args }) => method === "frappe.desk.form.utils.add_comment" && args.content === "[KOT-PRINT-FAILED] PRINTER_TIMEOUT"));
});

test("missed tickets print once after reconnect and the job targets host:port with ESC/POS cut", async () => {
  const { printer, qz, call } = await makePrinter();
  await printer.process(ticket("SEEN"));
  await printer.process(ticket("SEEN"));
  await printer.process(ticket("MISSED", "Kitchen", "2026-10-01T12:02:00.000Z"));

  assert.equal(qz.calls.prints.length, 2);
  assert.deepEqual(qz.calls.configs[1], { host: "192.0.2.10", port: 9100 });
  assert.deepEqual(qz.calls.prints[1].data[0], {
    type: "raw", format: "html", flavor: "plain", data: "<div>Kitchen ticket</div>",
    options: { language: "ESCPOS" },
  });
  assert.equal(qz.calls.prints[1].data[1].type, "raw");
  assert.match(qz.calls.prints[1].data[1].data, /\u001dV/);
  await new Promise((resolve, reject) => qz.calls.security.certificate(resolve, reject));
  const signatureHandler = qz.calls.security.signature("payload");
  await new Promise((resolve, reject) => signatureHandler(resolve, reject));
  assert.equal(qz.calls.security.algorithm, "SHA512");
  assert.ok(call.calls.some(({ method }) => method === "ury.ury.api.ury_print.qz_certificate"));
  assert.ok(call.calls.some(({ method, args }) => method === "ury.ury.api.ury_print.signature_promise" && args.toSign === "payload"));
});

test("an installed printer name is the QZ config and the job keeps the ESC/POS cut", async () => {
  const device = storage({
    "ury:mosaic:station-print:Kitchen:settings": JSON.stringify(settings({ host: "", printer: "KITCHEN-RAW" })),
  });
  const { printer, qz } = await makePrinter({ storage: device });

  assert.deepEqual(await printer.process(ticket("USB")), { printed: true });

  assert.deepEqual(qz.calls.configs, ["KITCHEN-RAW"]);
  assert.equal(qz.calls.prints[0].config, "KITCHEN-RAW");
  assert.match(qz.calls.prints[0].data[1].data, /\u001dV/);
});

test("configure requires exactly one of an installed printer or a network host", async () => {
  const { printer } = await makePrinter({ storage: storage() });

  assert.throws(
    () => printer.configure(settings({ printer: "KITCHEN-RAW", host: "192.0.2.10" })),
    { message: "Choose an installed printer or a network host, not both." },
  );
  assert.throws(
    () => printer.configure(settings({ printer: "", host: "" })),
    { message: "Choose an installed printer or enter a network host and port." },
  );
  assert.throws(
    () => printer.configure(settings({ printer: "   ", host: undefined })),
    { message: "Choose an installed printer or enter a network host and port." },
  );
  assert.equal(printer.settings.enabled, false);

  const saved = printer.configure(settings({ printer: " KITCHEN-RAW ", host: "", port: "" }));
  assert.equal(saved.printer, "KITCHEN-RAW");
  assert.equal(saved.host, "");
  assert.equal(saved.enabled, true);
  const network = printer.configure(settings({ printer: "" }));
  assert.equal(network.printer, "");
  assert.equal(network.host, "192.0.2.10");
});

test("a fresh device saves with printing disabled and no target, but never with both targets", async () => {
  const { printer } = await makePrinter({ storage: storage() });

  const saved = printer.configure({ ...printer.settings, enabled: false });
  assert.equal(saved.enabled, false);
  assert.equal(saved.printer, "");
  assert.equal(saved.host, "");
  assert.throws(
    () => printer.configure(settings({ enabled: false, printer: "KITCHEN-RAW", host: "192.0.2.10" })),
    { message: "Choose an installed printer or a network host, not both." },
  );
  assert.throws(
    () => printer.configure(settings({ printer: "", host: "" })),
    { message: "Choose an installed printer or enter a network host and port." },
  );
});

test("listPrinters returns the printers the OS has installed through qz.printers.find()", async () => {
  const { printer, qz } = await makePrinter();

  assert.deepEqual(await printer.listPrinters(), ["KITCHEN-RAW", "BAR-RAW"]);
  assert.deepEqual(qz.calls.finds, [[]]);
});

test("a missing installed printer writes PRINTER_NOT_FOUND and stays retryable", async () => {
  const call = callDouble();
  let missing = true;
  const device = storage({
    "ury:mosaic:station-print:Kitchen:settings": JSON.stringify(settings({ host: "", printer: "KITCHEN-RAW" })),
  });
  const { printer } = await makePrinter({
    call, storage: device,
    qzDouble: qzDouble({ print: async () => { if (missing) throw Error('Cannot find printer with name "KITCHEN-RAW"'); } }),
  });

  assert.deepEqual(await printer.process(ticket("UNPLUGGED")), { printed: false, code: "PRINTER_NOT_FOUND" });
  assert.deepEqual(printer.failed(), ["UNPLUGGED"]);
  assert.ok(call.calls.some(({ method, args }) => method === "frappe.desk.form.utils.add_comment" && args.content === "[KOT-PRINT-FAILED] PRINTER_NOT_FOUND"));

  missing = false;
  await printer.retry();
  assert.deepEqual(printer.failed(), []);
});

test("render failure writes RENDER_FAILED without marking the KOT printed", async () => {
  const call = callDouble({ html: undefined });
  call.get = async (method, args) => {
    call.calls.push({ method, args });
    if (method === "frappe.www.printview.get_html_and_style") throw new Error("render failed");
    return { message: {} };
  };
  const { printer } = await makePrinter({ call });

  assert.deepEqual(await printer.process(ticket("RENDER")), { printed: false, code: "RENDER_FAILED" });
  assert.deepEqual(printer.failed(), ["RENDER"]);
});

const componentSource = source.split("<script>")[1].split("</script>")[0];
const templateCode = compile(parse(source).descriptor.template.content, { mode: "function", prefixIdentifiers: true }).code;
const render = new Function("Vue", templateCode)(Vue);

function componentDefinition() {
  const module = stationModule || {};
  return new Function(
    "FrappeApp", "window", "navigator", "localStorage", "fetch", "io", "console",
    "createStationPrinter", "kitchenErrorMessage",
    componentSource.replace(/^import .*;\s*$/gm, "").replace("export default", "return"),
  )(
    class { call() { return callDouble(); } auth() { return { getLoggedInUser: async () => "chef@example.test" }; } },
    { location: { hostname: "synthetic.test", protocol: "https:", port: "", origin: "https://synthetic.test" }, addEventListener() {}, removeEventListener() {} },
    { onLine: true }, storage(), async () => ({ json: async () => ({ message: { site_name: "synthetic.test" } }) }),
    () => ({ connected: true, on() {}, off() {} }), { log() {}, error() {} },
    module.createStationPrinter, kitchenActions.kitchenErrorMessage,
  );
}

test("each KOT card has Print, failures show the retry banner, and managers can open settings", async () => {
  const component = componentDefinition();
  const app = Vue.createSSRApp({
    ...component, props: [],
    data: () => ({
      ...component.data(), production: "Kitchen", loadingKots: false,
      kot: [ticket("VISIBLE")], failedPrints: [ticket("MISSED")],
      showPrintSettings: true, canManagePrintSettings: true,
      stationSettings: settings(), socketStale: false,
    }),
    render,
  });
  const html = await renderToString(app);

  assert.match(html, /1 tickets not printed — Retry/);
  assert.match(html, />Print</);
  assert.match(html, /Manager station printing settings/);
  assert.match(html, /Printer host/);
});

async function renderSettings(stationSettings, installedPrinters) {
  const component = componentDefinition();
  const app = Vue.createSSRApp({
    ...component, props: [],
    data: () => ({
      ...component.data(), production: "Kitchen", loadingKots: false, kot: [],
      showPrintSettings: true, canManagePrintSettings: true,
      stationSettings, installedPrinters, socketStale: false,
    }),
    render,
  });
  return renderToString(app);
}

test("the settings dialog offers installed printers plus a manual network host option", async () => {
  const html = await renderSettings(settings({ host: "", printer: "KITCHEN-RAW" }), ["KITCHEN-RAW", "BAR-RAW"]);

  assert.match(html, /<select[^>]*>/);
  // Vue SSR writes an empty value as a bare attribute and has no SSR props for v-model selection.
  assert.match(html, /<option value>Network printer \(host and port\)<\/option>/);
  assert.match(html, /<option value="KITCHEN-RAW"[^>]*>KITCHEN-RAW<\/option>/);
  assert.match(html, /<option value="BAR-RAW"[^>]*>BAR-RAW<\/option>/);
  assert.doesNotMatch(html, /Printer host/);

  const manual = await renderSettings(settings({ printer: "" }), ["KITCHEN-RAW"]);
  assert.match(manual, /Printer host/);
  assert.match(manual, /Printer port/);
});

test("opening settings lists installed printers and saving a printer clears the host", async () => {
  const component = componentDefinition();
  const configured = [];
  const vm = {
    ...component.data(), production: "Kitchen", loggeduser: "Administrator",
    call: { get: async () => ({ message: { roles: [] } }), post: async () => ({ message: {} }) },
    stationPrinter: {
      settings: settings(),
      listPrinters: async () => ["KITCHEN-RAW"],
      configure: next => { configured.push(next); return next; },
    },
  };
  for (const [name, method] of Object.entries(component.methods)) vm[name] = method.bind(vm);

  await vm.openPrintSettings();
  await new Promise(setImmediate);
  assert.deepEqual(vm.installedPrinters, ["KITCHEN-RAW"]);
  assert.equal(vm.showPrintSettings, true);

  vm.stationSettings = { ...vm.stationSettings, printer: "KITCHEN-RAW" };
  vm.savePrintSettings();
  assert.equal(configured[0].printer, "KITCHEN-RAW");
  assert.equal(configured[0].host, "");
  assert.equal(vm.stationSettingsError, "");
});

test("qz.printers.find() rejecting shows printerListError and changes no ticket state", async () => {
  const qzValue = qzDouble();
  qzValue.qz.printers.find = async () => { throw Error("Unable to establish connection with QZ"); };
  const call = callDouble();
  const changes = [];
  const { printer } = await makePrinter({ call, qzDouble: qzValue, onChange: state => changes.push(state) });

  await assert.rejects(printer.listPrinters(), { message: "Unable to establish connection with QZ" });
  assert.deepEqual(changes, []);
  assert.deepEqual(printer.failed(), []);
  assert.equal(call.calls.some(({ method }) => method === "frappe.desk.form.utils.add_comment"), false);

  const component = componentDefinition();
  const failedPrints = [ticket("WAITING")];
  const vm = {
    ...component.data(), production: "Kitchen", loggeduser: "Administrator",
    call: { get: async () => ({ message: { roles: [] } }), post: async () => ({ message: {} }) },
    stationPrinter: printer, failedPrints, printingKots: ["PENDING"], printedKots: ["DONE"],
  };
  for (const [name, method] of Object.entries(component.methods)) vm[name] = method.bind(vm);

  await vm.openPrintSettings();
  await new Promise(setImmediate);

  assert.equal(vm.showPrintSettings, true);
  assert.equal(vm.printerListError, "Unable to establish connection with QZ");
  assert.deepEqual(vm.installedPrinters, []);
  assert.equal(vm.printError, "");
  assert.equal(vm.failedPrints, failedPrints);
  assert.deepEqual(vm.printingKots, ["PENDING"]);
  assert.deepEqual(vm.printedKots, ["DONE"]);

  const app = Vue.createSSRApp({
    ...component, props: [],
    data: () => ({
      ...component.data(), production: "Kitchen", loadingKots: false, kot: [],
      showPrintSettings: true, canManagePrintSettings: true, stationSettings: settings(),
      installedPrinters: [], printerListError: vm.printerListError, socketStale: false,
    }),
    render,
  });
  assert.match(await renderToString(app), /Unable to establish connection with QZ/);
});

test("Kitchen and Bar each print only to their own installed printer", async () => {
  const device = storage({
    "ury:mosaic:station-print:Kitchen:settings": JSON.stringify(settings({ host: "", printer: "KITCHEN-RAW" })),
    "ury:mosaic:station-print:Bar:settings": JSON.stringify(settings({ host: "", printer: "BAR-RAW" })),
  });
  const kitchenQz = qzDouble();
  const barQz = qzDouble();
  const kitchen = await makePrinter({ storage: device, qzDouble: kitchenQz });
  const bar = await makePrinter({ storage: device, qzDouble: barQz, production: "Bar" });
  const food = ticket("FOOD", "Kitchen");
  const drink = ticket("DRINK", "Bar");

  for (const station of [kitchen.printer, bar.printer]) {
    await station.process(food);
    await station.process(drink);
  }

  assert.deepEqual(kitchenQz.calls.configs, ["KITCHEN-RAW"]);
  assert.deepEqual(kitchenQz.calls.prints.map(({ config }) => config), ["KITCHEN-RAW"]);
  assert.deepEqual(barQz.calls.configs, ["BAR-RAW"]);
  assert.deepEqual(barQz.calls.prints.map(({ config }) => config), ["BAR-RAW"]);
  const rendered = ({ call }) => call.calls
    .filter(({ method }) => method === "frappe.www.printview.get_html_and_style")
    .map(({ args }) => args.name);
  assert.deepEqual(rendered(kitchen), ["FOOD"]);
  assert.deepEqual(rendered(bar), ["DRINK"]);
  assert.equal(kitchen.printer.settings.printer, "KITCHEN-RAW");
  assert.equal(bar.printer.settings.printer, "BAR-RAW");
});

test("a URY Manager without System Manager opens settings from session roles", async () => {
  const requests = [];
  const call = {
    get: async (method, args) => {
      requests.push({ method, args });
      if (method === "frappe.client.get") throw Object.assign(Error("User read is not permitted"), { exc_type: "PermissionError" });
      if (method === "ury.ury.doctype.ury_order.ury_order.get_captain_context") {
        return { message: { user: "manager@example.test", roles: ["URY Manager"] } };
      }
      return { message: {} };
    },
    post: async () => ({ message: {} }),
  };
  const component = componentDefinition();
  const vm = {
    ...component.data(), production: "Kitchen", loggeduser: "manager@example.test", call,
    ...component.methods,
  };
  for (const [name, method] of Object.entries(component.methods)) vm[name] = method.bind(vm);

  await vm.openPrintSettings();

  assert.equal(vm.canManagePrintSettings, true);
  assert.equal(vm.showPrintSettings, true);
  assert.equal(vm.printError, "");
  assert.ok(requests.some(({ method }) => method === "ury.ury.doctype.ury_order.ury_order.get_captain_context"));
  assert.equal(requests.some(({ method }) => method === "frappe.client.get"), false);
});

test("successful jobs persist the printed set across reloads and record the KOT and unit", async () => {
  const device = storage({ "ury:mosaic:station-print:Kitchen:settings": JSON.stringify(settings()) });
  const first = await makePrinter({ storage: device });
  await first.printer.process(ticket("PERSISTED"));
  const second = await makePrinter({ storage: device });
  await second.printer.process(ticket("PERSISTED"));
  assert.equal(second.qz.calls.prints.length, 0);
  assert.ok(first.call.calls.some(({ method, args }) =>
    method === "frappe.desk.form.utils.add_comment" &&
    args.reference_doctype === "URY KOT" && args.reference_name === "PERSISTED" &&
    args.content === "[KOT-PRINTED] Kitchen",
  ));
});

test("failed jobs do not repeat on duplicate events but Retry submits once and clears the banner state", async () => {
  let broken = true;
  const { printer, qz } = await makePrinter({
    qzDouble: qzDouble({ print: async () => { if (broken) throw Error("offline"); } }),
  });
  await printer.process(ticket("RETRY"));
  await printer.process(ticket("RETRY"));
  assert.equal(qz.calls.prints.length, 1);
  broken = false;
  await printer.retry();
  await printer.retry();
  assert.equal(qz.calls.prints.length, 2);
  assert.deepEqual(printer.failed(), []);
});

test("device settings start disabled and enabling sets a durable cutoff without resetting it on host edits", async () => {
  const device = storage();
  const { printer, qz } = await makePrinter({ storage: device, now: () => Date.parse("2026-10-01T12:00:00Z") });
  await printer.process(ticket("DISABLED"));
  assert.equal(qz.calls.prints.length, 0);
  printer.configure(settings({ enabledAt: "1900-01-01" }));
  assert.equal(printer.settings.enabledAt, "2026-10-01T12:00:00.000Z");
  printer.configure({ ...printer.settings, host: "192.0.2.11" });
  const reloaded = await makePrinter({ storage: device });
  assert.equal(reloaded.printer.settings.enabledAt, "2026-10-01T12:00:00.000Z");
  assert.equal(reloaded.printer.settings.host, "192.0.2.11");
});

test("permission-checked rendering includes the selected format and its CSS", async () => {
  const call = callDouble();
  call.get = async (method, args) => {
    call.calls.push({ method, args });
    return { message: { html: "<div>Ticket</div>", style: ".ticket { color: black; }" } };
  };
  const { printer, qz } = await makePrinter({ call });
  await printer.process(ticket("STYLE"));
  assert.deepEqual(call.calls.find(({ method }) => method === "frappe.www.printview.get_html_and_style").args, {
    doc: "URY KOT", name: "STYLE", print_format: "Salama Kitchen Ticket 80mm",
  });
  assert.match(qz.calls.prints[0].data[0].data, /<style>\.ticket \{ color: black; \}<\/style><div>Ticket<\/div>/);
});

test("QZ loads only Frappe's existing copy without a new npm dependency", async () => {
  const module = requireStationModule();
  const scripts = [];
  const browser = {};
  const document = {
    createElement: tag => ({ tag }),
    head: { appendChild(script) { scripts.push(script); browser.qz = qzDouble().qz; script.onload(); } },
  };
  const loaded = await module.loadQz({ window: browser, document });
  assert.equal(loaded, browser.qz);
  assert.equal(scripts[0].src, "/assets/frappe/node_modules/qz-tray/qz-tray.js");
  const dependencies = JSON.parse(await readFile(new URL("../../package.json", import.meta.url), "utf8")).dependencies;
  assert.equal(dependencies["qz-tray"], undefined);
});
