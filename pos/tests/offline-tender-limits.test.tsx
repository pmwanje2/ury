import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { IDBFactory } from "fake-indexeddb";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { call } from "@ury/core";
import { testLocks } from "./offline-locks";
import { initI18n } from "../src/i18n";
import { usePOSStore } from "../src/store/pos-store";
import { useRootStore } from "../src/store/root-store";
import { OfflineOutbox, type OfflineProfile } from "../src/offline/outbox";
import { submitSettlementOfflineAware } from "../src/offline/cashier";
import OfflineBanner from "../src/components/OfflineBanner";

const user = { name: "cashier@example.com", roles: ["URY Cashier"] };

function makeProfile(extra: Partial<OfflineProfile> = {}): OfflineProfile {
  return {
    name: "Till A",
    ury_offline_capture: 1,
    role_allowed_for_billing: [{ role: "URY Cashier" }],
    offline_tender_limit: 500,
    room_charge_modes: ["Room Charge"],
    offline_payment_modes: [
      { mode_of_payment: "Cash", allow_offline: true, requires_reference: false },
      { mode_of_payment: "Airtel Money", allow_offline: true, requires_reference: true },
      { mode_of_payment: "Staff Meal Card", allow_offline: true, requires_reference: false },
      { mode_of_payment: "Visa Card", allow_offline: false, requires_reference: false },
      { mode_of_payment: "Room Charge", allow_offline: false, requires_reference: false },
    ],
    ...extra,
  };
}

let container: HTMLDivElement;
let root: Root;

beforeEach(async () => {
  sessionStorage.clear();
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true);
  vi.stubGlobal("indexedDB", new IDBFactory());
  vi.stubGlobal("navigator", {
    onLine: false,
    storage: { persist: vi.fn(async () => true) },
    locks: testLocks(),
  });
  vi.spyOn(window, "open").mockImplementation(
    () => ({ document: { write() {}, close() {} }, focus() {}, print() {} }) as any,
  );
  await initI18n("en");
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("Offline tender eligibility comes from Mode of Payment data, never tender-name regex", () => {
  it("contains zero tender-name regexes in offline/cashier.ts and OfflineBanner.tsx", () => {
    const cashierSrc = readFileSync(resolve("src/offline/cashier.ts"), "utf8");
    const bannerSrc = readFileSync(resolve("src/components/OfflineBanner.tsx"), "utf8");
    for (const src of [cashierSrc, bannerSrc]) {
      expect(src).not.toMatch(/\/\^cash\$\/i/);
      expect(src).not.toMatch(/mobile\.\?money|momo|m-\?pesa/i);
    }
  });

  it("allows Airtel Money and any client-configured offline tender, and refuses unflagged Cash", async () => {
    const profile = makeProfile();
    const airtelRes = await submitSettlementOfflineAware({
      profile,
      user,
      invoice: "INV-AIR-1",
      amount: 300,
      due: 300,
      modeOfPayment: "Airtel Money",
      reference: "AIR-4411",
      onlineSettle: async () => {
        throw { httpStatus: 503 };
      },
    });
    expect(airtelRes.offline).toBe(true);
    expect(airtelRes.entry?.settlement?.mode_of_payment).toBe("Airtel Money");
    expect(airtelRes.entry?.settlement?.reference).toBe("AIR-4411");

    const customRes = await submitSettlementOfflineAware({
      profile,
      user,
      invoice: "INV-SMC-1",
      amount: 200,
      due: 200,
      modeOfPayment: "Staff Meal Card",
      onlineSettle: async () => {
        throw { httpStatus: 503 };
      },
    });
    expect(customRes.offline).toBe(true);
    expect(customRes.entry?.settlement?.mode_of_payment).toBe("Staff Meal Card");

    // A mode named "Cash" with allow_offline=false MUST NOT be queued offline!
    const cashDisabledProfile = makeProfile({
      offline_payment_modes: [
        { mode_of_payment: "Cash", allow_offline: false, requires_reference: false },
      ],
    });
    await expect(
      submitSettlementOfflineAware({
        profile: cashDisabledProfile,
        user,
        invoice: "INV-NO-CASH",
        amount: 100,
        due: 100,
        modeOfPayment: "Cash",
        onlineSettle: async () => {
          throw { httpStatus: 503 };
        },
      }),
    ).rejects.toEqual({ httpStatus: 503 });
  });

  it("refuses offline settlement above per-till offline_tender_limit and requires PIN when back online", async () => {
    const profile = makeProfile({ offline_tender_limit: 500 });
    const q = new OfflineOutbox({ profile, user, print: async () => true });
    const entry = await q.capture(
      {
        pos_profile: "Till A",
        customer: "Walk In",
        items: [{ item: "Steak", item_name: "Steak", qty: 1, rate: 800 }],
        no_of_pax: 1,
        invoice: null,
        last_invoice: null,
        order_type: "Take Away",
      },
      800,
    );

    // Above 500 offline limit: refused offline (unless explicitly marked for online manager PIN)
    await expect(
      q.settle(entry.client_order_id, {
        kind: "offline",
        mode_of_payment: "Airtel Money",
        amount: 800,
        reference: "AIR-BIG",
      }),
    ).rejects.toThrow(/limit|approval|online/i);

    // Explicitly queued for online PIN review -> replay stops on server POS-AUTH-REQUIRED
    // and OfflineBanner offers ManagerApproval (offline_tender) when back online!
    await q.settle(entry.client_order_id, {
      kind: "offline",
      mode_of_payment: "Airtel Money",
      amount: 800,
      reference: "AIR-BIG",
      needs_online_approval: true,
    });

    const sends: any[] = [];
    const req = vi.fn(async (url: string, init?: RequestInit) => {
      const method = url.split("/api/method/")[1]?.split("?")[0];
      if (method === "frappe.auth.get_logged_user") {
        return new Response(JSON.stringify({ message: user.name }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      const payload = JSON.parse(String(init?.body || "{}"));
      sends.push({ method, payload });
      if (method?.endsWith("sync_order")) {
        return new Response(JSON.stringify({ message: { name: "INV-BIG-1", docstatus: 0 } }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (method?.endsWith("make_invoice")) {
        if (!payload.offline_approval) {
          return new Response(
            JSON.stringify({
              exc_type: "PermissionError",
              _server_messages: JSON.stringify([
                JSON.stringify({
                  message: "Exceeds offline limit [POS-OFFLINE-OVER-LIMIT] [POS-AUTH-REQUIRED]",
                }),
              ]),
            }),
            { status: 403, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({ message: { name: "INV-BIG-1", status: "Submitted" } }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ message: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });

    const onlineQueue = new OfflineOutbox({ profile, user, request: req, print: async () => true });
    await onlineQueue.replay();
    const parked = await onlineQueue.list();
    expect(parked).toHaveLength(1);
    expect(parked[0].state).toBe("review");
    expect(parked[0].needs_approval).toBe(true);

    // Attach manager approval token obtained online and replay succeeds!
    await onlineQueue.approveAndReplay(entry.client_order_id, "offline-pin-tok-1", "Outage large bill");
    expect(await onlineQueue.list()).toEqual([]);
    const lastInvoiceCall = sends.filter((s) => s.method.endsWith("make_invoice")).pop();
    expect(lastInvoiceCall.payload).toMatchObject({
      offline_replay: 1,
      offline_approval: "offline-pin-tok-1",
      offline_reason: "Outage large bill",
    });
  });

  it("OfflineBanner shows Airtel Money button from profile data and Approve & Replay for over-limit parked rows", async () => {
    const profile = makeProfile();
    usePOSStore.setState({
      posProfile: profile as any,
      paymentModes: ["Cash", "Airtel Money", "Visa Card", "Room Charge"],
    });
    useRootStore.setState({ user: user as any });
    vi.spyOn(call, "get").mockResolvedValue({ message: [] });

    const q = new OfflineOutbox({ profile, user, print: async () => true });
    await q.capture(
      {
        pos_profile: "Till A",
        customer: "Walk In",
        items: [{ item: "Tea", item_name: "Tea", qty: 1, rate: 100 }],
        no_of_pax: 1,
        invoice: null,
        last_invoice: null,
        order_type: "Take Away",
      },
      100,
    );

    await act(async () =>
      root.render(
        <MemoryRouter>
          <OfflineBanner />
        </MemoryRouter>,
      ),
    );
    await vi.waitFor(async () => {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
      });
      expect(container.textContent).toContain("Airtel Money");
      expect(container.textContent).not.toContain("Visa Card");
      expect(container.textContent).not.toContain("Room Charge");
    });
  });
});
