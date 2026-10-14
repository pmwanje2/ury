import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import * as Vue from "vue";
import { compile } from "@vue/compiler-dom";
import { parse } from "@vue/compiler-sfc";
import { renderToString } from "@vue/server-renderer";

const source = await readFile(new URL("./kot.vue", import.meta.url), "utf8");
const stationModule = await import("./station-print.js").catch(() => null);

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

function qzDouble({ print = async () => {}, active = true } = {}) {
  const calls = { configs: [], prints: [], security: {} };
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
    "createStationPrinter",
    componentSource.replace(/^import .*;\s*$/gm, "").replace("export default", "return"),
  )(
    class { call() { return callDouble(); } auth() { return { getLoggedInUser: async () => "chef@example.test" }; } },
    { location: { hostname: "synthetic.test", protocol: "https:", port: "", origin: "https://synthetic.test" }, addEventListener() {}, removeEventListener() {} },
    { onLine: true }, storage(), async () => ({ json: async () => ({ message: { site_name: "synthetic.test" } }) }),
    () => ({ connected: true, on() {}, off() {} }), { log() {}, error() {} },
    module.createStationPrinter,
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
  const dependencies = JSON.parse(await readFile(new URL("../../../package.json", import.meta.url), "utf8")).dependencies;
  assert.equal(dependencies["qz-tray"], undefined);
});

