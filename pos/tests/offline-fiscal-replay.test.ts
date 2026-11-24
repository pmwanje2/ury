/**
 * Offline replay carries room_number and triggers post-replay fiscal printing
 * through the shared pending -> COPY #n reprint path once URA issues the FDN.
 */
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { call } from "@ury/core";
import { testLocks } from "./offline-locks";
import { cashierOutbox, submitSettlementOfflineAware } from "../src/offline/cashier";
import { OfflineOutbox, type OfflineProfile, type OfflineUser } from "../src/offline/outbox";

const json = (message: unknown, status = 200) =>
  new Response(JSON.stringify({ message }), { status, headers: { "Content-Type": "application/json" } });

const user: OfflineUser = { name: "cashier@example.com", roles: ["Cashier"] };
const makeProfile = (): OfflineProfile => ({
  name: "Till A",
  ury_offline_capture: 1,
  role_allowed_for_billing: [{ role: "Cashier" }],
  offline_payment_modes: [
    { mode_of_payment: "Cash", type: "Cash", allow_offline: true, requires_reference: false },
  ],
});

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  vi.stubGlobal("indexedDB", new IDBFactory());
  vi.stubGlobal("navigator", {
    onLine: true,
    storage: { persist: vi.fn(async () => true) },
    locks: testLocks(),
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

test("offline settlement carries room_number through gateway queueing, provisional slip, and replay make_invoice", async () => {
  const profile = makeProfile();
  const written: string[] = [];
  vi.spyOn(window, "open").mockImplementation(
    () =>
      ({
        opener: null,
        document: { write: (html: string) => written.push(html), close: () => undefined },
        focus: () => undefined,
        print: () => undefined,
      }) as unknown as Window,
  );

  // 1) Gateway 502 queues settlement carrying room_number
  const outcome = await submitSettlementOfflineAware({
    profile,
    user,
    invoice: "INV-ROOM-1",
    customer: "Guest 204",
    table: "T2",
    cashier: user.name,
    owner: user.name,
    amount: 20000,
    due: 20000,
    modeOfPayment: "Cash",
    roomNumber: "204",
    onlineSettle: async () => {
      throw { httpStatus: 502 };
    },
  } as Parameters<typeof submitSettlementOfflineAware>[0] & { roomNumber: string });
  expect(outcome.offline).toBe(true);

  // 2) Replay forwards room_number in make_invoice and invokes onSettled callback
  const sends: Array<{ method: string; data: Record<string, unknown> }> = [];
  const settledInvoices: string[] = [];
  const request = vi.fn(async (url: string, init?: RequestInit) => {
    const method = url.split("/api/method/")[1]?.split("?")[0] || "";
    if (method === "frappe.auth.get_logged_user") return json(user.name);
    const payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
    sends.push({ method, data: payload });
    if (method.endsWith("make_invoice")) return json({ name: payload.invoice, status: "Submitted" });
    return json(true);
  });

  const q = new OfflineOutbox({
    profile,
    user,
    request,
    print: () => true,
    onSettled: ({ invoice }) => {
      settledInvoices.push(invoice);
    },
  } as ConstructorParameters<typeof OfflineOutbox>[0] & { onSettled?: (arg: { invoice: string }) => void });
  await q.replay();

  const makeCall = sends.find((s) => s.method.endsWith("make_invoice"));
  expect(makeCall?.data.room_number).toBe("204");
  expect(settledInvoices).toEqual(["INV-ROOM-1"]);
});

test("replayed offline settlement enters the pending->reprint path and prints COPY #n once FDN arrives", async () => {
  const profile = {
    ...makeProfile(),
    print_type: "socket",
    print_format: "Restaurant Receipt 80mm",
    efris_print_wait_seconds: 0,
  };
  const openSpy = vi.spyOn(window, "open").mockReturnValue(null);
  const getSpy = vi.spyOn(call, "get").mockResolvedValue({ message: { efris_status: "Pending", fdn: null } });
  const postSpy = vi.spyOn(call, "post").mockImplementation(async (method: string) => {
    if (method === "ury.ury_pos.api.queue_fiscal_reprint") {
      return { message: { status: "AWAITING_FDN" } };
    }
    if (method === "ury.ury_pos.api.print_bill_copy") {
      return {
        message: {
          status: "COMPLETED",
          copy_number: 1,
          delivery: "browser",
          html: "<article><h1>COPY #1</h1></article>",
          print_url: "/printview?doctype=POS%20Invoice&name=INV-OFF-1&format=URY%20Bill%20Copy&copy_number=1",
        },
      };
    }
    return { message: { ok: true } };
  });

  const queue = cashierOutbox(profile, user);
  await queue.queueGatewaySettlement({
    clientOrderId: "11111111-1111-4111-8111-111111111111",
    roundId: "22222222-2222-4222-8222-222222222222",
    invoice: "INV-OFF-1",
    customer: "Walk In",
    table: null,
    cashier: user.name,
    owner: user.name,
    settlement: { kind: "cash", mode_of_payment: "Cash", amount: 15000, room_number: "305" } as {
      kind: "cash";
      mode_of_payment: string;
      amount: number;
      room_number?: string;
    },
  });

  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = url.split("/api/method/")[1]?.split("?")[0] || "";
      if (method === "frappe.auth.get_logged_user") return json(user.name);
      const payload = JSON.parse(String(init?.body)) as Record<string, unknown>;
      if (method.endsWith("make_invoice")) return json({ name: payload.invoice, status: "Submitted" });
      return json(true);
    }),
  );

  await queue.replay();

  // While FDN is still pending, it queues the automatic fiscal reprint rather than printing a false FDN.
  expect(postSpy).toHaveBeenCalledWith(
    "ury.ury_pos.api.queue_fiscal_reprint",
    expect.objectContaining({ invoice: "INV-OFF-1" }),
  );

  // Once URA issues the FDN, flushing pending reprints prints COPY #1 via print_bill_copy.
  getSpy.mockResolvedValue({ message: { efris_status: "Submitted", fdn: "326099887766" } });
  const efrisModulePath = resolve("src/lib/efris-print.ts");
  expect(existsSync(efrisModulePath), "efris-print helper is not implemented").toBe(true);
  const { flushPendingFiscalReprints } = (await import(/* @vite-ignore */ efrisModulePath)) as {
    flushPendingFiscalReprints: (p: Record<string, unknown>) => Promise<void>;
  };
  await flushPendingFiscalReprints(profile);

  expect(postSpy).toHaveBeenCalledWith(
    "ury.ury_pos.api.print_bill_copy",
    expect.objectContaining({
      invoice: "INV-OFF-1",
      idempotency_key: "efris-auto-INV-OFF-1",
      delivery: "browser",
    }),
  );
  expect(openSpy).toHaveBeenCalledWith(expect.stringContaining("copy_number=1"), "_blank", "noopener,noreferrer");
});
