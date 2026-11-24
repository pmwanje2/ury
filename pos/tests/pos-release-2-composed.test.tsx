/**
 * Composed integration contract for #143 (POS money integrity) + #144 (EFRIS fiscal receipt & copy).
 */
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import PaymentDialog from "../src/components/PaymentDialog";
import { printBillCopy, printOrder } from "../src/lib/print";
import { call } from "@ury/core";

const mockUsePOSStore = vi.fn();

vi.mock("../src/store/pos-store", () => ({
  usePOSStore: () => mockUsePOSStore(),
}));

vi.mock("../src/lib/print", () => ({
  printOrder: vi.fn().mockResolvedValue("qz"),
  printBillCopy: vi.fn().mockResolvedValue({ status: "COMPLETED", copy_number: 1 }),
}));

vi.mock("@ury/core", () => ({
  formatCurrency: (amount: number, currency?: string) =>
    currency === "UGX" ? `UGX ${Math.round(amount).toLocaleString("en-US")}` : `${currency || "USD"} ${amount.toFixed(2)}`,
  parseFrappeError: vi.fn((err: unknown, fallback?: string) =>
    err && typeof err === "object" && "message" in err && typeof err.message === "string" ? err.message : fallback,
  ),
  call: {
    get: vi.fn(),
    post: vi.fn(),
  },
}));

vi.mock("@ury/ui", () => ({
  Button: ({ children, ...props }: ComponentPropsWithoutRef<"button">) => <button {...props}>{children}</button>,
  Input: (props: ComponentPropsWithoutRef<"input">) => <input {...props} />,
  Dialog: ({ children, open }: { children?: ReactNode; open?: boolean }) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  showToast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
}));

vi.mock("../src/i18n", () => ({
  t: (key: string, params?: Record<string, unknown>) => (params ? `${key}: ${JSON.stringify(params)}` : key),
}));

const MAKE = "ury.ury.doctype.ury_order.ury_order.make_invoice";
const SUBMIT = "ury.ury.doctype.ury_order.ury_order.submit_order_invoice";
const PREVIEW = "ury.ury.doctype.ury_order.ury_order.preview_settlement_totals";
const EFRIS_STATUS = "uganda_compliance.efris.efris_queue.get_efris_status";

describe("Composed #143 + #144 settlement & fiscal receipt contract", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    vi.mocked(call.post).mockResolvedValue({ message: { name: "INV-COMP-1", status: "Submitted" } });
    vi.mocked(printOrder).mockResolvedValue("qz");
    vi.mocked(printBillCopy).mockResolvedValue({ status: "COMPLETED", copy_number: 1 });
  });

  it("preserves a received Mobile Money partial tender, settles remaining Cash, polls EFRIS, then prints", async () => {
    vi.mocked(call.get).mockImplementation(async (method: string) => {
      if (method === EFRIS_STATUS) {
        return { message: { efris_status: "Submitted", fdn: "326041111111" } };
      }
      return { message: {} };
    });
    mockUsePOSStore.mockReturnValue({
      paymentModes: ["Cash", "Mobile Money"],
      fetchPaymentModes: vi.fn(),
      currency: "UGX",
      posProfile: {
        name: "Main Till",
        currency: "UGX",
        default_payment_mode: "Cash",
        enable_discount: 1,
        enable_payment_without_printing: 1,
        print_format: "Restaurant Receipt 80mm",
        print_type: "qz",
        qz_host: "localhost",
        efris_print_wait_seconds: 2,
      },
    });

    render(
      <PaymentDialog
        order={{ invoice_printed: 0 }}
        existingPayments={[{ mode_of_payment: "Mobile Money", amount: 15000, reference_no: "PR-001", custom_tender_received: 1 }]}
        onClose={vi.fn()}
        grandTotal={35000}
        roundedTotal={35000}
        invoice="INV-COMP-1"
        customer="Walk In"
        posProfile="Main Till"
        table={null}
        cashier="cashier@example.com"
        owner="cashier@example.com"
        fetchOrders={vi.fn().mockResolvedValue(undefined)}
        clearSelectedOrder={vi.fn()}
      />,
    );

    expect(screen.getByTestId("received-tenders")).toHaveTextContent("Mobile Money");
    expect(screen.getByTestId("balance-due")).toHaveTextContent("UGX 20,000");

    const cashInput = document.querySelectorAll<HTMLInputElement>('input[type="number"][step="0.01"]')[0];
    await waitFor(() => expect(cashInput.value).toBe("20000"));

    fireEvent.click(screen.getByText(/payment\.pay_button/));

    await waitFor(() => expect(printOrder).toHaveBeenCalledTimes(1));
    expect(call.post).toHaveBeenCalledWith(
      MAKE,
      expect.objectContaining({
        submit_invoice: 0,
        payments: [
          { mode_of_payment: "Mobile Money", amount: 15000, reference_no: "PR-001" },
          { mode_of_payment: "Cash", amount: 20000 },
        ],
      }),
    );
    expect(call.post).toHaveBeenCalledWith(SUBMIT, { invoice: "INV-COMP-1" });
    const makeIdx = vi.mocked(call.post).mock.calls.findIndex(([m]) => m === MAKE);
    const submitIdx = vi.mocked(call.post).mock.calls.findIndex(([m]) => m === SUBMIT);
    expect(vi.mocked(call.post).mock.invocationCallOrder[makeIdx]).toBeLessThan(
      vi.mocked(call.post).mock.invocationCallOrder[submitIdx],
    );
    expect(vi.mocked(call.post).mock.invocationCallOrder[submitIdx]).toBeLessThan(
      vi.mocked(printOrder).mock.invocationCallOrder[0],
    );
  });

  it("applies server-previewed discount on USD cents bill, submits, polls EFRIS, and prints", async () => {
    vi.mocked(call.get).mockImplementation(async (method: string) => {
      if (method === PREVIEW) {
        return { message: { grand_total: 11.25, rounded_total: 11.25 } };
      }
      if (method === EFRIS_STATUS) {
        return { message: { efris_status: "Submitted", fdn: "326049999999" } };
      }
      return { message: {} };
    });
    mockUsePOSStore.mockReturnValue({
      paymentModes: ["Cash", "Guest Folio"],
      fetchPaymentModes: vi.fn(),
      currency: "USD",
      posProfile: {
        name: "USD Till",
        currency: "USD",
        default_payment_mode: "Cash",
        room_charge_modes: ["Guest Folio"],
        enable_discount: 1,
        enable_payment_without_printing: 1,
        print_format: "Merged POS Invoice Format",
        print_type: "browser",
        efris_print_wait_seconds: 2,
      },
    });

    render(
      <PaymentDialog
        order={{ invoice_printed: 0 }}
        onClose={vi.fn()}
        grandTotal={12.5}
        roundedTotal={12.5}
        invoice="INV-USD-1"
        customer="Walk In"
        posProfile="USD Till"
        table={null}
        cashier="cashier@example.com"
        owner="cashier@example.com"
        fetchOrders={vi.fn().mockResolvedValue(undefined)}
        clearSelectedOrder={vi.fn()}
      />,
    );

    const discountInput = document.querySelector<HTMLInputElement>('input[type="number"]:not([step])')!;
    fireEvent.change(discountInput, { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: /apply/i }));

    await waitFor(() => expect(call.get).toHaveBeenCalledWith(PREVIEW, expect.objectContaining({ additionalDiscount: 10 })));
    const cashInput = document.querySelectorAll<HTMLInputElement>('input[type="number"][step="0.01"]')[0];
    await waitFor(() => expect(cashInput.value).toBe("11.25"));

    fireEvent.click(screen.getByText(/payment\.pay_button/));
    await waitFor(() => expect(printOrder).toHaveBeenCalledTimes(1));

    expect(call.post).toHaveBeenCalledWith(
      MAKE,
      expect.objectContaining({
        additionalDiscount: 10,
        submit_invoice: 0,
        payments: [{ mode_of_payment: "Cash", amount: 11.25 }],
      }),
    );
    expect(call.post).toHaveBeenCalledWith(SUBMIT, { invoice: "INV-USD-1" });
  });
});
