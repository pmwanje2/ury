/**
 * The receipt printed from the payment dialog is the post-settlement customer receipt.
 *
 * Oracle Simphony prints the guest check during service (non-fiscal, pro forma) and the
 * customer / fiscal receipt only when the check is tendered and closed. URA EFRIS
 * fiscalises a POS Invoice on submit (uganda_compliance efris_queue.on_submit_invoice),
 * so a receipt printed while the invoice is still a draft (docstatus 0) can never carry
 * the FDN. With "payment without printing" on and "Print invoice" ticked, the dialog
 * must therefore settle (make_invoice, then submit_order_invoice) and only then print.
 * A printer failure after settlement leaves the payment recorded and asks for a reprint.
 */
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import PaymentDialog from "./PaymentDialog";
import { printBillCopy, printOrder } from "../lib/print";
import { call } from "@ury/core";
import { showToast } from "@ury/ui";

const mockUsePOSStore = vi.fn();

vi.mock("../store/pos-store", () => ({
  usePOSStore: () => mockUsePOSStore(),
}));

vi.mock("../lib/print", () => ({
  printOrder: vi.fn().mockResolvedValue("qz"),
  printBillCopy: vi.fn().mockResolvedValue({ status: "COMPLETED", copy_number: 1 }),
}));

vi.mock("@ury/core", () => ({
  formatCurrency: (amount: number) => `UGX ${amount.toFixed(2)}`,
  parseFrappeError: vi.fn((err: unknown, fallback?: string) =>
    err && typeof err === "object" && "message" in err && typeof err.message === "string"
      ? err.message
      : fallback
  ),
  call: {
    get: vi.fn().mockResolvedValue({ message: { efris_status: null, fdn: null } }),
    post: vi.fn().mockResolvedValue({ ok: true }),
  },
}));

vi.mock("@ury/ui", () => ({
  Button: ({ children, ...props }: ComponentPropsWithoutRef<"button">) => <button {...props}>{children}</button>,
  Input: (props: ComponentPropsWithoutRef<"input">) => <input {...props} />,
  Dialog: ({ children, open }: { children?: ReactNode; open?: boolean }) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: { children?: ReactNode }) => <div data-testid="dialog-content">{children}</div>,
  showToast: {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
  },
}));

vi.mock("../i18n", () => ({
  t: (key: string, params?: Record<string, unknown>) => (params ? `${key}: ${JSON.stringify(params)}` : key),
}));

const MAKE = "ury.ury.doctype.ury_order.ury_order.make_invoice";
const SUBMIT = "ury.ury.doctype.ury_order.ury_order.submit_order_invoice";
const EFRIS_STATUS = "uganda_compliance.efris.efris_queue.get_efris_status";
const QUEUE_REPRINT = "ury.ury_pos.api.queue_fiscal_reprint";

function renderDialog(overrides: Record<string, unknown> = {}) {
  const props = {
    onClose: vi.fn(),
    grandTotal: 1000,
    roundedTotal: 1000,
    invoice: "INV001",
    customer: "CUST001",
    posProfile: "POS001",
    table: null,
    cashier: "user1",
    owner: "user1",
    fetchOrders: vi.fn().mockResolvedValue(undefined),
    clearSelectedOrder: vi.fn(),
    ...overrides,
  };
  render(<PaymentDialog {...props} />);
  return props;
}

function callOrder(method: string) {
  const index = vi.mocked(call.post).mock.calls.findIndex(([name]) => name === method);
  expect(index, `${method} was not called`).toBeGreaterThanOrEqual(0);
  return vi.mocked(call.post).mock.invocationCallOrder[index];
}

describe("PaymentDialog fiscal receipt order", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(call.get).mockResolvedValue({ message: { efris_status: null, fdn: null } });
    vi.mocked(call.post).mockResolvedValue({ ok: true });
    vi.mocked(printOrder).mockResolvedValue("qz");
    vi.mocked(printBillCopy).mockResolvedValue({ status: "COMPLETED", copy_number: 1 });
    mockUsePOSStore.mockReturnValue({
      paymentModes: ["Cash"],
      fetchPaymentModes: vi.fn(),
      posProfile: {
        name: "POS001",
        enable_discount: 1,
        enable_payment_without_printing: 1,
        print_format: "Restaurant Receipt 80mm",
        print_type: "qz",
        qz_host: "localhost",
        efris_print_wait_seconds: 1,
      },
    });
  });

  it("submits the invoice before it prints the receipt", async () => {
    renderDialog();
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);

    fireEvent.click(screen.getByText(/payment.pay_button/));

    await waitFor(() => expect(printOrder).toHaveBeenCalledWith(expect.objectContaining({ orderId: "INV001", skipStatusUpdate: true })));
    const printedAt = vi.mocked(printOrder).mock.invocationCallOrder[0];
    expect(callOrder(MAKE)).toBeLessThan(callOrder(SUBMIT));
    expect(callOrder(SUBMIT)).toBeLessThan(printedAt);
    expect(call.post).toHaveBeenCalledWith(MAKE, expect.objectContaining({ submit_invoice: 0 }));
    expect(call.post).toHaveBeenCalledWith(SUBMIT, { invoice: "INV001" });
  });

  it("prints nothing when the settlement is refused", async () => {
    vi.mocked(call.post).mockImplementation(async (method: string) => {
      if (method === SUBMIT) throw new Error("Customer is on hold");
      return { ok: true };
    });
    const props = renderDialog();

    fireEvent.click(screen.getByText(/payment.pay_button/));

    await waitFor(() => expect(screen.getByText("Customer is on hold")).toBeInTheDocument());
    expect(printOrder).not.toHaveBeenCalled();
    expect(props.onClose).not.toHaveBeenCalled();
  });

  it("keeps the recorded payment when the receipt then fails to print and asks for a reprint", async () => {
    vi.mocked(printOrder).mockRejectedValueOnce(new Error("Printer is offline"));
    const props = renderDialog();

    fireEvent.click(screen.getByText(/payment.pay_button/));

    await waitFor(() => expect(props.onClose).toHaveBeenCalled());
    expect(call.post).toHaveBeenCalledWith(SUBMIT, { invoice: "INV001" });
    expect(props.clearSelectedOrder).toHaveBeenCalled();
    expect(props.fetchOrders).toHaveBeenCalled();
    const errors = vi.mocked(showToast.error).mock.calls.map(([message]) => String(message));
    expect(errors).toEqual([expect.stringContaining("payment.receipt_print_failed")]);
    expect(errors[0]).toContain("Printer is offline");
    expect(errors.join(" ")).not.toContain("payment.payment_not_processed");
  });

  it("polls get_efris_status until the FDN arrives before printing the fiscal receipt", async () => {
    let pollCount = 0;
    vi.mocked(call.get).mockImplementation(async (method: string) => {
      if (method === EFRIS_STATUS) {
        pollCount += 1;
        if (pollCount === 1) {
          return { message: { efris_status: "Pending", fdn: null } };
        }
        return { message: { efris_status: "Submitted", fdn: "324013579864" } };
      }
      return { message: null };
    });

    renderDialog();
    fireEvent.click(screen.getByText(/payment.pay_button/));

    await waitFor(() => expect(printOrder).toHaveBeenCalledWith(expect.objectContaining({ orderId: "INV001" })), {
      timeout: 3000,
    });
    expect(pollCount).toBe(2);
    expect(call.get).toHaveBeenCalledWith(EFRIS_STATUS, { doctype: "POS Invoice", name: "INV001" });
    const lastPollOrder = vi.mocked(call.get).mock.invocationCallOrder[1];
    const printedAt = vi.mocked(printOrder).mock.invocationCallOrder[0];
    expect(lastPollOrder).toBeLessThan(printedAt);
    expect(call.post).not.toHaveBeenCalledWith(QUEUE_REPRINT, expect.anything());
  });

  it("on EFRIS timeout prints pending receipt, queues automatic reprint, and prints COPY #n once FDN arrives", async () => {
    mockUsePOSStore.mockReturnValue({
      paymentModes: ["Cash"],
      fetchPaymentModes: vi.fn(),
      posProfile: {
        name: "POS001",
        enable_discount: 1,
        enable_payment_without_printing: 1,
        print_format: "Restaurant Receipt 80mm",
        print_type: "qz",
        qz_host: "localhost",
        efris_print_wait_seconds: 0,
      },
    });
    vi.mocked(call.get).mockResolvedValue({ message: { efris_status: "Pending", fdn: null } });

    const props = renderDialog();
    fireEvent.click(screen.getByText(/payment.pay_button/));

    await waitFor(() => expect(props.onClose).toHaveBeenCalled());
    expect(printOrder).toHaveBeenCalledWith(expect.objectContaining({ orderId: "INV001" }));
    expect(call.post).toHaveBeenCalledWith(
      QUEUE_REPRINT,
      expect.objectContaining({ invoice: "INV001" }),
    );

    // Once URA later issues the FDN, flushing pending reprints dispatches COPY #n via printBillCopy.
    const efrisModulePath = resolve("src/lib/efris-print.ts");
    expect(existsSync(efrisModulePath), "efris-print helper is not implemented").toBe(true);
    const { flushPendingFiscalReprints, resolveEfrisPrintWaitSeconds } = (await import(
      /* @vite-ignore */ efrisModulePath
    )) as {
      flushPendingFiscalReprints: (profile: Record<string, unknown>) => Promise<void>;
      resolveEfrisPrintWaitSeconds: (raw: unknown) => number;
    };
    expect(resolveEfrisPrintWaitSeconds(undefined)).toBe(5);
    expect(resolveEfrisPrintWaitSeconds(0)).toBe(0);
    expect(resolveEfrisPrintWaitSeconds(10)).toBe(10);
    expect(resolveEfrisPrintWaitSeconds(15)).toBe(15);
    expect(resolveEfrisPrintWaitSeconds(16)).toBe(15);
    expect(resolveEfrisPrintWaitSeconds(500)).toBe(15);
    expect(resolveEfrisPrintWaitSeconds(-3)).toBe(0);
    vi.mocked(call.get).mockResolvedValue({ message: { efris_status: "Submitted", fdn: "324013579864" } });
    await flushPendingFiscalReprints(mockUsePOSStore().posProfile);

    expect(printBillCopy).toHaveBeenCalledWith(
      expect.objectContaining({
        orderId: "INV001",
        idempotencyKey: "efris-auto-INV001",
      }),
    );
  });

  it("short-circuits immediately on efris_status Failed and queues automatic reprint", async () => {
    const { waitForEfrisFdn } = await import("../lib/efris-print");
    vi.mocked(call.get).mockResolvedValue({ message: { efris_status: "Failed", fdn: null } });

    const started = Date.now();
    const res = await waitForEfrisFdn({ invoice: "INV-FAIL", timeoutSeconds: 15, pollIntervalMs: 500 });
    expect(res).toEqual({ outcome: "timed_out", status: "Failed" });
    expect(Date.now() - started).toBeLessThan(400);
    expect(call.get).toHaveBeenCalledTimes(1);
  });

  it("keeps polling and retains localStorage reprint queue on transient HTTP errors", async () => {
    const { flushPendingFiscalReprints, queuePendingFiscalReprint, waitForEfrisFdn } = await import("../lib/efris-print");
    const profile = mockUsePOSStore().posProfile;

    // 1) Transient 502 on poll 1 followed by Submitted FDN on poll 2 recovers instead of returning not_applicable.
    vi.mocked(call.get)
      .mockRejectedValueOnce(new Error("502 Bad Gateway"))
      .mockResolvedValueOnce({ message: { efris_status: "Submitted", fdn: "324013579999" } });
    const recovered = await waitForEfrisFdn({ invoice: "INV-T1", timeoutSeconds: 2, pollIntervalMs: 10 });
    expect(recovered).toEqual({ outcome: "fiscalised", fdn: "324013579999", status: "Submitted" });

    // 2) Persistent transient error returns timed_out (so caller queues reprint), NOT not_applicable.
    vi.mocked(call.get).mockRejectedValue(new Error("503 Service Unavailable"));
    const timedOut = await waitForEfrisFdn({ invoice: "INV-T2", timeoutSeconds: 0.03, pollIntervalMs: 10 });
    expect(timedOut.outcome).toBe("timed_out");

    // 3) flushPendingFiscalReprints keeps entry in localStorage across transient errors and prints once reachable.
    await queuePendingFiscalReprint({ invoice: "INV-T2", posProfile: profile });
    expect(localStorage.getItem("ury_pending_fiscal_reprints")).toContain("INV-T2");

    await flushPendingFiscalReprints(profile);
    expect(printBillCopy).not.toHaveBeenCalled();
    expect(localStorage.getItem("ury_pending_fiscal_reprints")).toContain("INV-T2");

    vi.mocked(call.get).mockResolvedValueOnce({ message: { efris_status: "Submitted", fdn: "324013570002" } });
    await flushPendingFiscalReprints(profile);
    expect(printBillCopy).toHaveBeenCalledWith(
      expect.objectContaining({ orderId: "INV-T2", idempotencyKey: "efris-auto-INV-T2" }),
    );
    expect(localStorage.getItem("ury_pending_fiscal_reprints")).toBeNull();
  });

  it("blocks double-click race synchronously across step 1 with a ref mutex", async () => {
    let releaseMake: (() => void) | undefined;
    vi.mocked(call.post).mockImplementation((method: string) => {
      if (method === MAKE) {
        return new Promise((resolve) => {
          releaseMake = () => resolve({ ok: true });
        });
      }
      return Promise.resolve({ ok: true });
    });

    renderDialog();
    const payButton = screen.getByText(/payment.pay_button/);
    // Two clicks fired synchronously in one tick before React re-renders disabled state
    act(() => {
      payButton.click();
      payButton.click();
    });

    await waitFor(() => expect(releaseMake).toBeDefined());
    releaseMake?.();
    await waitFor(() => expect(printOrder).toHaveBeenCalledTimes(1));

    const makeCalls = vi.mocked(call.post).mock.calls.filter(([method]) => method === MAKE);
    expect(makeCalls).toHaveLength(1);
  });

  it("locks the print toggle checked and non-skippable when enable_payment_without_printing is 0", async () => {
    mockUsePOSStore.mockReturnValue({
      paymentModes: ["Cash"],
      fetchPaymentModes: vi.fn(),
      posProfile: {
        name: "POS001",
        enable_discount: 1,
        enable_payment_without_printing: 0,
        print_format: "Restaurant Receipt 80mm",
        print_type: "qz",
        qz_host: "localhost",
      },
    });

    renderDialog({ order: { invoice_printed: 1 } });
    const checkbox = screen.getByRole("checkbox") as HTMLInputElement;
    expect(checkbox).toBeInTheDocument();
    expect(checkbox.checked).toBe(true);
    expect(checkbox.disabled).toBe(true);

    // Attempting to click the locked checkbox must have no effect: fiscal receipt printing stays mandatory.
    fireEvent.click(checkbox);
    expect(checkbox.checked).toBe(true);

    fireEvent.click(screen.getByText(/payment.pay_button/));

    await waitFor(() =>
      expect(printOrder).toHaveBeenCalledWith(expect.objectContaining({ orderId: "INV001", skipStatusUpdate: true })),
    );
    expect(callOrder(MAKE)).toBeLessThan(callOrder(SUBMIT));
    expect(callOrder(SUBMIT)).toBeLessThan(vi.mocked(printOrder).mock.invocationCallOrder[0]);
  });
});
