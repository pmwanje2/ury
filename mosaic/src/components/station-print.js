// QZ remains Frappe's unmodified desktop bridge; signing stays on the server.
export function loadQz({ window = globalThis.window, document = globalThis.document } = {}) {
  if (window.qz) return Promise.resolve(window.qz);
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = "/assets/frappe/node_modules/qz-tray/qz-tray.js";
    script.onload = () => window.qz ? resolve(window.qz) : reject(Error("QZ_UNAVAILABLE"));
    script.onerror = () => { script.remove(); reject(Error("QZ_UNAVAILABLE")); };
    document.head.appendChild(script);
  });
}

export function createStationPrinter({
  production, user, call, storage = globalThis.localStorage, qz,
  fetch = globalThis.fetch, now = Date.now, onChange = () => {},
}) {
  const prefix = `ury:mosaic:station-print:${production}`;
  const read = (key, fallback) => {
    try { return JSON.parse(storage.getItem(`${prefix}:${key}`)) ?? fallback; }
    catch { return fallback; }
  };
  let settings = { enabled: false, enabledAt: null, host: "", port: 9100, printFormat: "", ...read("settings", {}) };
  const printed = new Set(read("printed", []));
  const attempted = new Set();
  const failures = new Map();
  const pending = new Set();
  let qzReady;

  async function connectQz() {
    if (!qzReady) {
      qzReady = (async () => {
        const bridge = qz || await loadQz();
        bridge.security.setCertificatePromise((resolve, reject) => {
          call.get("ury.ury.api.ury_print.qz_certificate", {}).then(async ({ message }) => {
            if (!message) throw Error("QZ certificate is not configured");
            if (message.includes("-----BEGIN CERTIFICATE-----")) return message;
            const response = await fetch(`/private/${message.split("/").map(encodeURIComponent).join("/")}`, { cache: "no-store", credentials: "same-origin" });
            if (!response.ok) throw Error("QZ certificate is unavailable");
            return response.text();
          }).then(resolve, reject);
        });
        bridge.security.setSignatureAlgorithm("SHA512");
        bridge.security.setSignaturePromise(toSign => (resolve, reject) => {
          call.post("ury.ury.api.ury_print.signature_promise", { toSign }).then(({ message }) => {
            if (!message) throw Error("QZ signature is unavailable");
            return message;
          }).then(resolve, reject);
        });
        if (!bridge.websocket.isActive()) await bridge.websocket.connect();
        return bridge;
      })().catch(error => { qzReady = null; throw error; });
    }
    const bridge = await qzReady;
    if (!bridge.websocket.isActive()) {
      qzReady = null;
      return connectQz();
    }
    return bridge;
  }

  function notify(error = "") {
    onChange({ failed: [...failures.keys()], pending: [...pending], printed: [...printed], error });
  }

  async function comment(kot, content) {
    try {
      await call.post("frappe.desk.form.utils.add_comment", {
        reference_doctype: "URY KOT", reference_name: kot.name, content,
        comment_email: user, comment_by: user,
      });
    } catch {
      notify(`Could not record the print outcome for ${kot.name}. Check the ticket before retrying.`);
    }
  }

  async function process(kot, { retry = false } = {}) {
    if (!settings.enabled || kot.production !== production || !kot.name || printed.has(kot.name) || pending.has(kot.name)) return { printed: false };
    const created = new Date(String(kot.creation).replace(" ", "T")).getTime();
    if (!Number.isFinite(created) || created < new Date(settings.enabledAt).getTime()) return { printed: false };
    if (attempted.has(kot.name) && !retry) return { printed: false };
    attempted.add(kot.name);
    pending.add(kot.name);
    notify();

    let expired = false;
    let timer;
    let code = "RENDER_FAILED";
    const job = async () => {
      const { message } = await call.get("frappe.www.printview.get_html_and_style", {
        doc: "URY KOT", name: kot.name, print_format: settings.printFormat,
      });
      if (!message?.html) throw Error("RENDER_FAILED");
      code = "QZ_UNAVAILABLE";
      const bridge = await connectQz();
      if (expired) return;
      const data = `${message.style ? `<style>${message.style}</style>` : ""}${message.html}`;
      await bridge.print(bridge.configs.create({ host: settings.host, port: Number(settings.port) }), [
        { type: "raw", format: "html", flavor: "plain", data, options: { language: "ESCPOS" } },
        { type: "raw", format: "command", flavor: "plain", data: "\n\n\n\x1dV\x00" },
      ]);
    };
    let outcome;
    try {
      await Promise.race([
        job(),
        new Promise((_resolve, reject) => {
          timer = setTimeout(() => { expired = true; code = "PRINTER_TIMEOUT"; reject(Error(code)); }, 8000);
        }),
      ]);
      printed.add(kot.name);
      storage.setItem(`${prefix}:printed`, JSON.stringify([...printed]));
      failures.delete(kot.name);
      outcome = { printed: true };
    } catch {
      failures.set(kot.name, kot);
      outcome = { printed: false, code };
    } finally {
      clearTimeout(timer);
      pending.delete(kot.name);
      notify();
    }
    await comment(kot, outcome.printed ? `[KOT-PRINTED] ${production}` : `[KOT-PRINT-FAILED] ${outcome.code}`);
    return outcome;
  }

  return {
    get settings() { return { ...settings }; },
    configure(next) {
      if (next.enabled && (!next.host?.trim() || !next.printFormat?.trim() || !Number.isInteger(Number(next.port)) || Number(next.port) < 1 || Number(next.port) > 65535)) throw Error("Enter a printer host, port (1–65535), and print format.");
      const enabledAt = next.enabled && !settings.enabled ? new Date(now()).toISOString() : settings.enabledAt;
      settings = { enabled: !!next.enabled, enabledAt, host: next.host.trim(), port: Number(next.port), printFormat: next.printFormat.trim() };
      storage.setItem(`${prefix}:settings`, JSON.stringify(settings));
      return { ...settings };
    },
    process,
    failed: () => [...failures.keys()],
    retry: () => Promise.all([...failures.values()].map(kot => process(kot, { retry: true }))),
  };
}
