import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { call } from "@ury/core";
import PaymentDialog from "./PaymentDialog";

// The till charges what the server computes and tenders what the POS Profile configures (G17, G21):
// - the total is the server's rounded total (currency precision, smallest fraction and the
//   site's rounding), or its grand total where the profile does not round; a discount applied in
//   the dialog is priced by the server, never by Math.ceil/Math.round in the browser;
// - the prefilled tender is the POS Profile payment row marked Default, not the word "Cash";
// - a room-charge tender is one the POS Profile reports as posting to Guest Ledger Clearing,
//   not a payment method that happens to be called "Room Charge".

const store = vi.hoisted(() => ({ state: {} as Record<string, unknown> }));

vi.mock("../store/pos-store", () => ({ usePOSStore: () => store.state }));
vi.mock("../lib/print", () => ({ printOrder: vi.fn().mockResolvedValue("qz") }));
vi.mock("../offline/cashier", () => ({
  assertOfflineActionAllowed: vi.fn().mockResolvedValue(undefined),
  submitSettlementOfflineAware: vi.fn(async ({ onlineSettle }: { onlineSettle: () => Promise<unknown> }) => {
    await onlineSettle();
    return { offline: false };
  }),
}));
vi.mock("@ury/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@ury/core")>();
  return {
    ...actual,
    formatCurrency: (amount: number) => `UGX ${Number(amount).toFixed(2)}`,
    call: { get: vi.fn(), post: vi.fn().mockResolvedValue({ message: { name: "BILL-1" } }) },
  };
});
vi.mock("@ury/ui", () => ({
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
  Input: ({ size: _size, ...props }: any) => <input {...props} />,
  Dialog: ({ children, open }: any) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: any) => <div>{children}</div>,
  showToast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("../i18n", () => ({ t: (key: string) => key }));

const PREVIEW = "ury.ury.doctype.ury_order.ury_order.preview_settlement_totals";
const MAKE_INVOICE = "ury.ury.doctype.ury_order.ury_order.make_invoice";

function setTill(paymentModes: string[], profile: Record<string, unknown> = {}) {
  store.state = {
    paymentModes,
    fetchPaymentModes: vi.fn(),
    posProfile: { name: "Till A", enable_discount: 1, enable_payment_without_printing: 1, ...profile },
  };
}

function renderDialog(grandTotal: number, roundedTotal: number) {
  return render(
    <PaymentDialog
      order={{ invoice_printed: 1 }}
      onClose={vi.fn()}
      grandTotal={grandTotal}
      roundedTotal={roundedTotal}
      invoice="BILL-1"
      customer="Walk-in Customer"
      posProfile="Till A"
      table={null}
      cashier="cashier@example.com"
      owner="cashier@example.com"
      fetchOrders={vi.fn(async () => {})}
      clearSelectedOrder={vi.fn()}
    />,
  );
}

const tenders = () => Array.from(document.querySelectorAll<HTMLInputElement>('input[type="number"][step="0.01"]'));
const tender = (index: number) => tenders()[index];
const payButton = () => screen.getByRole("button", { name: /^payment\.pay_button|^payment\.processing/ }) as HTMLButtonElement;
const roomBox = () => document.querySelector<HTMLInputElement>("#room-charge-reference");

beforeEach(() => {
  vi.mocked(call.get).mockReset();
  vi.mocked(call.post).mockClear();
});

describe("the default tender is the POS Profile's", () => {
  it("prefills the profile's default payment method with the total", async () => {
    setTill(["Cash", "Mobile Money"], { default_payment_mode: "Mobile Money" });
    renderDialog(1000, 1000);
    await waitFor(() => expect(tender(1).value).toBe("1000"));
    expect(tender(0).value).toBe("");
  });

  it("prefills nothing when the profile names no default", async () => {
    setTill(["Cash", "Mobile Money"]);
    renderDialog(1000, 1000);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(tenders().map((input) => input.value)).toEqual(["", ""]);
  });
});

describe("the total is the server's", () => {
  it("charges the server's rounded total, not a browser-rounded one", async () => {
    setTill(["Cash"], { default_payment_mode: "Cash" });
    renderDialog(12.34, 12.35);
    await waitFor(() => expect(tender(0).value).toBe("12.35"));
    fireEvent.change(tender(0), { target: { value: "12" } });
    expect(payButton().disabled).toBe(true);
  });

  it("charges the grand total when the profile does not round", async () => {
    setTill(["Cash"], { default_payment_mode: "Cash" });
    renderDialog(12.34, 0);
    await waitFor(() => expect(tender(0).value).toBe("12.34"));
  });

  it("prices a discount applied in the dialog on the server", async () => {
    vi.mocked(call.get).mockResolvedValue({ message: { grand_total: 1110.6, rounded_total: 1110 } });
    setTill(["Cash"], { default_payment_mode: "Cash" });
    renderDialog(1234, 1234);
    await waitFor(() => expect(tender(0).value).toBe("1234"));

    const discount = document.querySelector<HTMLInputElement>('input[type="number"]:not([step])')!;
    fireEvent.change(discount, { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "common.apply" }));

    await waitFor(() => expect(call.get).toHaveBeenCalledExactlyOnceWith(PREVIEW, {
      invoice: "BILL-1", pos_profile: "Till A", additionalDiscount: 10,
    }));
    await waitFor(() => expect(tender(0).value).toBe("1110"));
    fireEvent.change(tender(0), { target: { value: "1110" } });
    expect(payButton().disabled).toBe(false);
  });
});

describe("the room-charge tender is the POS Profile's", () => {
  it("asks for the room on a tender configured as room charge, whatever its name", async () => {
    setTill(["Cash", "Guest Folio"], { room_charge_modes: ["Guest Folio"] });
    renderDialog(100, 100);
    fireEvent.change(tender(1), { target: { value: "100" } });
    expect(roomBox(), "no room box for the configured room-charge tender").not.toBeNull();
    fireEvent.change(roomBox()!, { target: { value: "108" } });
    fireEvent.click(payButton());
    await waitFor(() => expect(call.post).toHaveBeenCalledWith(MAKE_INVOICE, expect.objectContaining({
      room_number: "108", payments: [{ mode_of_payment: "Guest Folio", amount: 100 }],
    })));
  });

  it("does not treat a method named Room Charge as room charge unless the profile says so", async () => {
    setTill(["Room Charge"], { room_charge_modes: [] });
    renderDialog(100, 100);
    fireEvent.change(tender(0), { target: { value: "100" } });
    expect(roomBox()).toBeNull();
    fireEvent.click(payButton());
    await waitFor(() => expect(call.post).toHaveBeenCalledWith(MAKE_INVOICE, expect.anything()));
    expect(vi.mocked(call.post).mock.calls[0][1]).not.toHaveProperty("room_number");
  });
});
