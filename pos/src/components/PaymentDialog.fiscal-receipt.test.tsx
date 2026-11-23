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
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import PaymentDialog from "./PaymentDialog";
import { printOrder } from "../lib/print";
import { call } from "@ury/core";
import { showToast } from "@ury/ui";

const mockUsePOSStore = vi.fn();

vi.mock("../store/pos-store", () => ({
  usePOSStore: () => mockUsePOSStore(),
}));

vi.mock("../lib/print", () => ({
  printOrder: vi.fn().mockResolvedValue("qz"),
}));

vi.mock("@ury/core", () => ({
  formatCurrency: (amount: number) => `UGX ${amount.toFixed(2)}`,
  parseFrappeError: vi.fn((err: any, fallback?: string) => err?.message || fallback),
  call: {
    post: vi.fn().mockResolvedValue({ ok: true }),
  },
}));

vi.mock("@ury/ui", () => ({
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
  Input: ({ ...props }: any) => <input {...props} />,
  Dialog: ({ children, open }: any) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: any) => <div data-testid="dialog-content">{children}</div>,
  showToast: {
    success: vi.fn(),
    error: vi.fn(),
  },
}));

vi.mock("../i18n", () => ({
  t: (key: string, params?: any) => (params ? `${key}: ${JSON.stringify(params)}` : key),
}));

const MAKE = "ury.ury.doctype.ury_order.ury_order.make_invoice";
const SUBMIT = "ury.ury.doctype.ury_order.ury_order.submit_order_invoice";

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
    vi.mocked(call.post).mockResolvedValue({ ok: true } as any);
    vi.mocked(printOrder).mockResolvedValue("qz");
    mockUsePOSStore.mockReturnValue({
      paymentModes: ["Cash"],
      fetchPaymentModes: vi.fn(),
      posProfile: { enable_discount: 1, enable_payment_without_printing: 1, print_format: "Restaurant Receipt 80mm" },
    });
  });

  it("submits the invoice before it prints the receipt", async () => {
    renderDialog();
    expect((screen.getByRole("checkbox") as HTMLInputElement).checked).toBe(true);

    fireEvent.click(screen.getByText(/payment.pay_button/));

    await waitFor(() => expect(printOrder).toHaveBeenCalledWith(expect.objectContaining({ orderId: "INV001" })));
    const printedAt = vi.mocked(printOrder).mock.invocationCallOrder[0];
    expect(callOrder(MAKE)).toBeLessThan(callOrder(SUBMIT));
    expect(callOrder(SUBMIT)).toBeLessThan(printedAt);
    expect(call.post).toHaveBeenCalledWith(MAKE, expect.objectContaining({ submit_invoice: 0 }));
    expect(call.post).toHaveBeenCalledWith(SUBMIT, { invoice: "INV001" });
  });

  it("prints nothing when the settlement is refused", async () => {
    vi.mocked(call.post).mockImplementation(async (method: string) => {
      if (method === SUBMIT) throw new Error("Customer is on hold");
      return { ok: true } as any;
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
});
