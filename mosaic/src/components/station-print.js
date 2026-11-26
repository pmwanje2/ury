import { connectQz, QzSigningRefusedError, runSignedQz, withQzDeadline } from '../../../ury/public/js/qz-signing.js';

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

function resolveBackupTarget(settings) {
  const backupPrinter = String(settings.backupPrinter ?? "").trim();
  const backupHost = String(settings.backupHost ?? "").trim();
  const backupPort = Number(settings.backupPort);
  if (backupPrinter) {
    return { target: backupPrinter, label: backupPrinter };
  }
  if (backupHost) {
    const port = Number.isInteger(backupPort) && backupPort >= 1 && backupPort <= 65535 ? backupPort : 9100;
    return { target: { host: backupHost, port }, label: `${backupHost}:${port}` };
  }
  return null;
}

function printFailureCode(error, fallback) {
  if (error instanceof QzSigningRefusedError) return "QZ_SIGNING_REFUSED";
  if (["QZ_TRUST_TIMEOUT", "PRINTER_TIMEOUT", "QZ_NOT_RUNNING"].includes(error?.message)) return error.message;
  if (fallback === "QZ_UNAVAILABLE" && /Cannot find printer/i.test(String(error?.message ?? error))) {
    return "PRINTER_NOT_FOUND";
  }
  return fallback;
}

export function createStationPrinter({
  production, user, call, storage = globalThis.localStorage, qz,
  now = Date.now, onChange = () => {},
  serverSettings = null,
}) {
  const prefix = `ury:mosaic:station-print:${production}`;
  const read = (key, fallback) => {
    try { return JSON.parse(storage.getItem(`${prefix}:${key}`)) ?? fallback; }
    catch { return fallback; }
  };
  const defaults = {
    enabled: false,
    enabledAt: null,
    printer: "",
    host: "",
    port: 9100,
    backupPrinter: "",
    backupHost: "",
    backupPort: 9100,
    printFormat: "",
    pageWidth: 576,
    commandMode: "standard",
  };
  const persisted = read("settings", null);
  let settings = {
    ...defaults,
    ...(serverSettings && typeof serverSettings === "object" ? serverSettings : {}),
    ...(persisted && typeof persisted === "object" ? persisted : {}),
  };
  if (settings.enabled && !settings.enabledAt) {
    settings.enabledAt = new Date(now()).toISOString();
  }
  const printed = new Set(read("printed", []));
  const attempted = new Set();
  const failures = new Map();
  const pending = new Set();
  let qzReady;

  async function publicCertificate() {
    const { message } = await call.get("ury.ury.api.ury_print.qz_certificate", {});
    if (!message?.startsWith("-----BEGIN CERTIFICATE-----")) throw Error("QZ certificate is not configured");
    return message;
  }

  function getQz() {
    if (!qzReady) {
      qzReady = (async () => {
        const bridge = qz || await loadQz();
        bridge.security.setCertificatePromise((resolve, reject) => {
          publicCertificate().then(resolve, reject);
        }, { rejectOnFailure: true });
        return bridge;
      })().catch(error => { qzReady = null; throw error; });
    }
    return qzReady;
  }

  const postSignature = (method, args) => call.post(method, args);

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

  function sendToTarget(bridge, target, data, signal, startPrinting, onConnected) {
    // QZ rasterises raw HTML at pageWidth dots (without it the ticket is an 8-dot blank strip). Double density
    // prints one dot per column; QZ's single-density default prints each column twice and cuts the ticket in half.
    // Raster mode is for generic ESC/POS clones that mishandle ESC * images: QZ sends GS v 0 instead.
    const image = settings.commandMode === "raster" ? { imageEncoding: "GS_V_0" } : { dotDensity: "double" };
    return runSignedQz(bridge, postSignature, async () => {
      if (!bridge.websocket.isActive()) {
        try { await connectQz(bridge, publicCertificate); }
        catch (cause) { throw new Error("QZ_NOT_RUNNING", { cause }); }
      }
      signal.throwIfAborted();
      startPrinting();
      onConnected?.();
      await bridge.print(bridge.configs.create(target), [
        { type: "raw", format: "html", flavor: "plain", data, options: { language: "ESCPOS", pageWidth: settings.pageWidth, ...image } },
        { type: "raw", format: "command", flavor: "plain", data: "\n\n\n\x1dV\x00" },
      ]);
    }, signal);
  }

  async function process(kot, { retry = false } = {}) {
    if (!settings.enabled || kot.production !== production || !kot.name || printed.has(kot.name) || pending.has(kot.name)) return { printed: false };
    const created = new Date(String(kot.creation).replace(" ", "T")).getTime();
    if (!Number.isFinite(created) || created < new Date(settings.enabledAt).getTime()) return { printed: false };
    if (attempted.has(kot.name) && !retry) return { printed: false };
    attempted.add(kot.name);
    pending.add(kot.name);
    notify();

    let code = "RENDER_FAILED";
    let renderedData = "";
    let bridgeInstance = null;

    const job = async (signal, startPrinting) => {
      const { message } = await call.get("frappe.www.printview.get_html_and_style", {
        doc: "URY KOT", name: kot.name, print_format: settings.printFormat,
      });
      if (!message?.html) throw Error("RENDER_FAILED");
      code = "QZ_UNAVAILABLE";
      signal.throwIfAborted();
      const bridge = await getQz();
      renderedData = `${message.style ? `<style>${message.style}</style>` : ""}${message.html}`;
      // QZ resolves a name to an OS print queue (USB or installed network printer); host is a raw socket.
      const target = settings.printer || { host: settings.host, port: Number(settings.port) };
      await sendToTarget(bridge, target, renderedData, signal, startPrinting, () => { bridgeInstance = bridge; });
    };

    let outcome;
    let failoverLabel = "";
    try {
      await withQzDeadline(job);
      printed.add(kot.name);
      storage.setItem(`${prefix}:printed`, JSON.stringify([...printed]));
      failures.delete(kot.name);
      outcome = { printed: true };
    } catch (error) {
      code = printFailureCode(error, code);
      // Both printers share the site's signer, so a refusal cannot fail over.
      const backup = code === "QZ_SIGNING_REFUSED" ? null : resolveBackupTarget(settings);
      if (backup && renderedData && bridgeInstance) {
        try {
          await withQzDeadline((signal, startPrinting) => sendToTarget(bridgeInstance, backup.target, renderedData, signal, startPrinting));
          printed.add(kot.name);
          storage.setItem(`${prefix}:printed`, JSON.stringify([...printed]));
          failures.delete(kot.name);
          failoverLabel = backup.label;
          outcome = { printed: true, failoverUsed: true, backupTarget: backup.label };
        } catch (backupError) {
          failures.set(kot.name, kot);
          outcome = { printed: false, code: printFailureCode(backupError, code) };
        }
      } else {
        failures.set(kot.name, kot);
        outcome = { printed: false, code };
      }
    } finally {
      pending.delete(kot.name);
      notify();
    }
    const logMessage = outcome.printed
      ? (failoverLabel ? `[KOT-FAILOVER-PRINTED] ${failoverLabel}` : `[KOT-PRINTED] ${production}`)
      : `[KOT-PRINT-FAILED] ${outcome.code}`;
    await comment(kot, logMessage);
    return outcome;
  }

  return {
    get settings() { return { ...settings }; },
    hydrateFromServer(remote) {
      if (!remote || typeof remote !== "object") return { ...settings };
      const localStored = read("settings", null);
      if (localStored) {
        // Only backfill server backup target when local storage has neither backup queue nor backup host.
        const hasLocalBackup = Boolean(
          String(settings.backupPrinter ?? "").trim() || String(settings.backupHost ?? "").trim(),
        );
        if (!hasLocalBackup) {
          settings = {
            ...settings,
            backupPrinter: String(remote.backupPrinter ?? "").trim(),
            backupHost: String(remote.backupHost ?? "").trim(),
            backupPort: Number(remote.backupPort) || 9100,
          };
        }
        return { ...settings };
      }
      settings = {
        ...defaults,
        ...remote,
        enabledAt: remote.enabled ? (remote.enabledAt || new Date(now()).toISOString()) : null,
      };
      return { ...settings };
    },
    configure(next) {
      const printer = String(next.printer ?? "").trim();
      const host = String(next.host ?? "").trim();
      const port = Number(next.port);
      const validPort = Number.isInteger(port) && port >= 1 && port <= 65535;
      const backupPrinter = String(next.backupPrinter ?? settings.backupPrinter ?? "").trim();
      const backupHost = String(next.backupHost ?? settings.backupHost ?? "").trim();
      const backupPortRaw = Number(next.backupPort ?? settings.backupPort ?? 9100);
      const validBackupPort = Number.isInteger(backupPortRaw) && backupPortRaw >= 1 && backupPortRaw <= 65535;
      // 80 mm and 58 mm ESC/POS heads at 203 dpi.
      const pageWidth = next.pageWidth ?? settings.pageWidth;
      if (pageWidth !== 576 && pageWidth !== 384) throw Error("Choose a paper width: 80 mm or 58 mm.");
      const commandMode = next.commandMode ?? settings.commandMode;
      if (commandMode !== "standard" && commandMode !== "raster") throw Error("Choose a printer command mode: Standard (ESC *) or Raster (GS v 0).");
      if (printer && host) throw Error("Choose an installed printer or a network host, not both.");
      if (backupPrinter && backupHost) throw Error("Choose a backup installed printer or a backup network host, not both.");
      if (next.enabled && !printer && !host) throw Error("Choose an installed printer or enter a network host and port.");
      if (next.enabled && (!next.printFormat?.trim() || (host && !validPort))) throw Error(printer ? "Enter a print format." : "Enter a printer host, port (1–65535), and print format.");
      const enabledAt = next.enabled && !settings.enabled ? new Date(now()).toISOString() : settings.enabledAt;
      settings = {
        enabled: !!next.enabled,
        enabledAt,
        printer,
        host,
        port: validPort ? port : 9100,
        backupPrinter,
        backupHost,
        backupPort: validBackupPort ? backupPortRaw : 9100,
        printFormat: String(next.printFormat ?? "").trim(),
        pageWidth,
        commandMode,
      };
      storage.setItem(`${prefix}:settings`, JSON.stringify(settings));
      return { ...settings };
    },
    publicCertificate,
    async listPrinters() {
      return withQzDeadline(async (signal, startLookup) => {
        const bridge = await getQz();
        return runSignedQz(bridge, postSignature, async () => {
          await connectQz(bridge, publicCertificate);
          signal.throwIfAborted();
          startLookup();
          return bridge.printers.find();
        }, signal);
      });
    },
    process,
    failed: () => [...failures.keys()],
    retry: () => Promise.all([...failures.values()].map(kot => process(kot, { retry: true }))),
  };
}
