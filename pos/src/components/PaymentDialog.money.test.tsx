import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { call } from "@ury/core";
import PaymentDialog from "./PaymentDialog";
import { printOrder } from "../lib/print";

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
    formatCurrency: vi.fn(
      (amount: number, currency?: string | null) => `${currency || "USD"} ${Number(amount).toFixed(2)}`,
    ),
    call: { get: vi.fn(), post: vi.fn().mockResolvedValue({ message: { name: "BILL-1" } }) },
  };
});
vi.mock("@ury/ui", () => ({
  Button: ({ children, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement>) =>
    React.createElement("button", props, children),
  Input: (props: Omit<React.InputHTMLAttributes<HTMLInputElement>, "size"> & { size?: string | number }) => {
    const rest = { ...props };
    delete rest.size;
    return React.createElement("input", rest);
  },
  Dialog: ({ children, open }: { children?: React.ReactNode; open?: boolean }) =>
    open ? React.createElement("div", null, children) : null,
  DialogContent: ({ children }: { children?: React.ReactNode }) => React.createElement("div", null, children),
  showToast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("../i18n", () => ({ t: (key: string) => key }));

const PREVIEW = "ury.ury.doctype.ury_order.ury_order.preview_settlement_totals";
const MAKE_INVOICE = "ury.ury.doctype.ury_order.ury_order.make_invoice";
const SUBMIT_INVOICE = "ury.ury.doctype.ury_order.ury_order.submit_order_invoice";

function setTill(
  paymentModes: string[],
  profile: Record<string, unknown> = {},
  extra: Record<string, unknown> = {},
) {
  store.state = {
    paymentModes,
    fetchPaymentModes: vi.fn(),
    posProfile: { name: "Till A", enable_discount: 1, enable_payment_without_printing: 1, ...profile },
    ...extra,
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

  it("shows a clear setup message when room charge has no clearing account mapped", async () => {
    setTill(["Cash", "Guest Folio"], {
      default_payment_mode: "Cash",
      room_charge_modes: [],
      room_charge_unconfigured: true,
    });
    renderDialog(100, 100);
    const notice = screen.getByTestId("room-charge-setup-notice");
    expect(notice).toHaveTextContent("payment.room_charge_not_configured");
    expect(roomBox()).toBeNull();
    await waitFor(() => expect(tender(0).value).toBe("100"));
    expect(payButton().disabled).toBe(false);
  });

  it("hides the setup message once room charge is mapped", () => {
    setTill(["Cash", "Guest Folio"], {
      default_payment_mode: "Cash",
      room_charge_modes: ["Guest Folio"],
      room_charge_unconfigured: false,
    });
    renderDialog(100, 100);
    expect(screen.queryByTestId("room-charge-setup-notice")).toBeNull();
  });
});

describe("unapplied typed discount is never sent on Pay", () => {
  const discountInput = () =>
    document.querySelector<HTMLInputElement>('input[type="number"]:not([step])')!;

  it("does not send an unapplied typed discount on direct Pay", async () => {
    setTill(["Cash"], { default_payment_mode: "Cash", enable_payment_without_printing: 1 });
    renderDialog(35.4, 35.4);
    await waitFor(() => expect(tender(0).value).toBe("35.4"));

    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.change(discountInput(), { target: { value: "10" } });
    fireEvent.click(payButton());

    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        MAKE_INVOICE,
        expect.objectContaining({
          additionalDiscount: null,
          payments: [{ mode_of_payment: "Cash", amount: 35.4 }],
          submit_invoice: 1,
        }),
      ),
    );
  });

  it("does not send an unapplied typed discount on print-then-submit Pay", async () => {
    setTill(["Cash"], { default_payment_mode: "Cash", enable_payment_without_printing: 1 });
    renderDialog(35.4, 35.4);
    await waitFor(() => expect(tender(0).value).toBe("35.4"));

    fireEvent.change(discountInput(), { target: { value: "10" } });
    fireEvent.click(payButton());

    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        MAKE_INVOICE,
        expect.objectContaining({
          additionalDiscount: null,
          payments: [{ mode_of_payment: "Cash", amount: 35.4 }],
          submit_invoice: 0,
        }),
      ),
    );
  });

  it("sends the last server-previewed discount when the box is edited without pressing Apply again", async () => {
    vi.mocked(call.get).mockResolvedValue({ message: { grand_total: 31.86, rounded_total: 31.86 } });
    setTill(["Cash"], { default_payment_mode: "Cash", enable_payment_without_printing: 0 });
    renderDialog(35.4, 35.4);
    await waitFor(() => expect(tender(0).value).toBe("35.4"));

    fireEvent.change(discountInput(), { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "common.apply" }));
    await waitFor(() => expect(tender(0).value).toBe("31.86"));

    fireEvent.change(discountInput(), { target: { value: "25" } });
    fireEvent.click(payButton());

    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        MAKE_INVOICE,
        expect.objectContaining({
          additionalDiscount: 10,
          payments: [{ mode_of_payment: "Cash", amount: 31.86 }],
        }),
      ),
    );
  });

  it("preserves a carried fixed bill discount without inventing a percentage", async () => {
    setTill(["Cash"], { default_payment_mode: "Cash", enable_payment_without_printing: 0 });
    render(
      <PaymentDialog
        order={{ invoice_printed: 1 }}
        onClose={vi.fn()}
        grandTotal={23.01}
        roundedTotal={23.01}
        invoice="BILL-1"
        customer="Walk-in Customer"
        posProfile="Till A"
        table={null}
        cashier="cashier@example.com"
        owner="cashier@example.com"
        fetchOrders={vi.fn(async () => {})}
        clearSelectedOrder={vi.fn()}
        discountPercentage={0}
        discountAmount={0.59}
      />,
    );
    await waitFor(() => expect(tender(0).value).toBe("23.01"));
    expect(discountInput().value).toBe("");

    fireEvent.click(payButton());
    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        MAKE_INVOICE,
        expect.objectContaining({
          additionalDiscount: null,
          payments: [{ mode_of_payment: "Cash", amount: 23.01 }],
        }),
      ),
    );
  });

  it("clearing an applied percentage discount to blank flags unapplied change and resets to 0 on Apply", async () => {
    vi.mocked(call.get)
      .mockResolvedValueOnce({ message: { grand_total: 31.86, rounded_total: 31.86 } })
      .mockResolvedValueOnce({ message: { grand_total: 35.4, rounded_total: 35.4 } });
    setTill(["Cash"], { default_payment_mode: "Cash", enable_payment_without_printing: 0, currency: "USD" });
    renderDialog(35.4, 35.4);
    await waitFor(() => expect(tender(0).value).toBe("35.4"));

    fireEvent.change(discountInput(), { target: { value: "10" } });
    fireEvent.click(screen.getByRole("button", { name: "common.apply" }));
    await waitFor(() => expect(tender(0).value).toBe("31.86"));

    fireEvent.change(discountInput(), { target: { value: "" } });
    expect(screen.getByRole("status")).toHaveTextContent("payment.discount_not_applied");

    fireEvent.click(screen.getByRole("button", { name: "common.apply" }));
    await waitFor(() =>
      expect(call.get).toHaveBeenLastCalledWith(PREVIEW, {
        invoice: "BILL-1",
        pos_profile: "Till A",
        additionalDiscount: 0,
      }),
    );
    await waitFor(() => expect(tender(0).value).toBe("35.4"));
    expect(screen.queryByText("payment.discount_not_applied")).toBeNull();

    fireEvent.click(payButton());
    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        MAKE_INVOICE,
        expect.objectContaining({
          additionalDiscount: null,
          payments: [{ mode_of_payment: "Cash", amount: 35.4 }],
        }),
      ),
    );
  });
});

describe("tenders already received on the check (Simphony parity)", () => {
  it("shows already-received tenders, prefills only balance due, and settles keeping earlier plus new tender", async () => {
    setTill(["Cash", "Mobile Money"], {
      default_payment_mode: "Mobile Money",
      enable_payment_without_printing: 0,
      currency: "USD",
    });
    render(
      <PaymentDialog
        order={{
          invoice_printed: 1,
          payments: [{ mode_of_payment: "Cash", amount: 5.31 }],
        }}
        onClose={vi.fn()}
        grandTotal={23.01}
        roundedTotal={23.01}
        invoice="LKG-00061"
        customer="Walk-in Customer"
        posProfile="Till A"
        table={null}
        cashier="cashier@example.com"
        owner="cashier@example.com"
        fetchOrders={vi.fn(async () => {})}
        clearSelectedOrder={vi.fn()}
        discountPercentage={0}
        discountAmount={0.59}
      />,
    );

    const received = screen.getByTestId("received-tenders");
    expect(received).toHaveTextContent("Cash");
    expect(received).toHaveTextContent("5.31");
    expect(screen.getByTestId("balance-due")).toHaveTextContent("17.70");
    await waitFor(() => expect(tender(1).value).toBe("17.7"));
    expect(tender(0).value).toBe("");

    fireEvent.click(payButton());
    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        MAKE_INVOICE,
        expect.objectContaining({
          invoice: "LKG-00061",
          additionalDiscount: null,
          payments: [
            { mode_of_payment: "Cash", amount: 5.31 },
            { mode_of_payment: "Mobile Money", amount: 17.7 },
          ],
        }),
      ),
    );
  });

  it("preserves external payment reference on earlier tender when settling remaining balance", async () => {
    setTill(["Cash", "Card"], {
      default_payment_mode: "Cash",
      enable_payment_without_printing: 0,
      currency: "USD",
    });
    render(
      <PaymentDialog
        order={{
          invoice_printed: 1,
          payments: [{ mode_of_payment: "Card", amount: 10, reference_no: "AUTH-991" }],
        }}
        onClose={vi.fn()}
        grandTotal={23.01}
        roundedTotal={23.01}
        invoice="BILL-REF"
        customer="Walk-in Customer"
        posProfile="Till A"
        table={null}
        cashier="cashier@example.com"
        owner="cashier@example.com"
        fetchOrders={vi.fn(async () => {})}
        clearSelectedOrder={vi.fn()}
      />,
    );

    const received = screen.getByTestId("received-tenders");
    expect(received).toHaveTextContent("Card");
    expect(received).toHaveTextContent("10.00");
    await waitFor(() => expect(tender(0).value).toBe("13.01"));

    fireEvent.click(payButton());
    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        MAKE_INVOICE,
        expect.objectContaining({
          payments: [
            { mode_of_payment: "Card", amount: 10, reference_no: "AUTH-991" },
            { mode_of_payment: "Cash", amount: 13.01 },
          ],
        }),
      ),
    );
  });

  it("allows settling with no new tender when already-received tenders cover the total", async () => {
    setTill(["Cash", "Mobile Money"], {
      default_payment_mode: "Mobile Money",
      enable_payment_without_printing: 0,
      currency: "USD",
    });
    render(
      <PaymentDialog
        order={{
          invoice_printed: 1,
          payments: [{ mode_of_payment: "Cash", amount: 23.01 }],
        }}
        onClose={vi.fn()}
        grandTotal={23.01}
        roundedTotal={23.01}
        invoice="BILL-PAID"
        customer="Walk-in Customer"
        posProfile="Till A"
        table={null}
        cashier="cashier@example.com"
        owner="cashier@example.com"
        fetchOrders={vi.fn(async () => {})}
        clearSelectedOrder={vi.fn()}
      />,
    );

    expect(screen.getByTestId("balance-due")).toHaveTextContent("0.00");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(tender(1).value).toBe("");
    expect(payButton().disabled).toBe(false);

    fireEvent.click(payButton());
    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        MAKE_INVOICE,
        expect.objectContaining({
          payments: [{ mode_of_payment: "Cash", amount: 23.01 }],
        }),
      ),
    );
  });

  it("computes UGX 100-unit over-tender change (19,100 / 20,000 / 900) with a prior partial tender", async () => {
    setTill(["Cash", "Mobile Money"], {
      default_payment_mode: "Cash",
      enable_payment_without_printing: 0,
      currency: "UGX",
    });
    render(
      <PaymentDialog
        order={{
          invoice_printed: 1,
          payments: [{ mode_of_payment: "Cash", amount: 19000 }],
        }}
        onClose={vi.fn()}
        grandTotal={19057}
        roundedTotal={19100}
        invoice="KBG-00003"
        customer="Walk-in Customer"
        posProfile="Till A"
        table={null}
        cashier="cashier@example.com"
        owner="cashier@example.com"
        fetchOrders={vi.fn(async () => {})}
        clearSelectedOrder={vi.fn()}
      />,
    );

    expect(screen.getByTestId("balance-due")).toHaveTextContent("100.00");
    await waitFor(() => expect(tender(0).value).toBe("100"));

    fireEvent.change(tender(0), { target: { value: "1000" } });
    expect(screen.getByText("payment.change_due")).toBeInTheDocument();
    expect(document.body.textContent).toContain("900.00");

    fireEvent.click(payButton());
    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        MAKE_INVOICE,
        expect.objectContaining({
          payments: [
            { mode_of_payment: "Cash", amount: 19000 },
            { mode_of_payment: "Cash", amount: 1000 },
          ],
        }),
      ),
    );
  });

  it("ignores draft payment rows marked custom_tender_received=0 (unconfirmed Phone / placeholder)", async () => {
    setTill(["Cash", "Mobile Money"], {
      default_payment_mode: "Mobile Money",
      enable_payment_without_printing: 0,
      currency: "USD",
    });
    render(
      <PaymentDialog
        order={{
          invoice_printed: 1,
          payments: [
            { mode_of_payment: "Online Mobile Money", amount: 5.9, custom_tender_received: 0 },
          ],
        }}
        onClose={vi.fn()}
        grandTotal={5.9}
        roundedTotal={5.9}
        invoice="LKG-UNCONFIRMED"
        customer="Walk-in Customer"
        posProfile="Till A"
        table={null}
        cashier="cashier@example.com"
        owner="cashier@example.com"
        fetchOrders={vi.fn(async () => {})}
        clearSelectedOrder={vi.fn()}
      />,
    );

    expect(screen.queryByTestId("received-tenders")).toBeNull();
    expect(screen.queryByTestId("balance-due")).toBeNull();
    await waitFor(() => expect(tender(1).value).toBe("5.9"));

    // Cashier switches to Cash at the counter.
    fireEvent.change(tender(1), { target: { value: "" } });
    fireEvent.focus(tender(0));
    expect(tender(0).value).toBe("5.9");
    fireEvent.click(payButton());
    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        MAKE_INVOICE,
        expect.objectContaining({
          payments: [{ mode_of_payment: "Cash", amount: 5.9 }],
        }),
      ),
    );
  });

  it("allows changing payment method and retrying after Step-2 submit failure", async () => {
    vi.mocked(call.get).mockResolvedValue({ message: { efris_status: null, fdn: null } });
    let submitAttempts = 0;
    vi.mocked(call.post).mockImplementation(async (method: string) => {
      if (method === SUBMIT_INVOICE) {
        submitAttempts += 1;
        if (submitAttempts === 1) {
          throw new Error("Payment for Mobile Money has not been confirmed yet.");
        }
      }
      return { message: { name: "BILL-1" } };
    });
    vi.mocked(printOrder).mockClear();
    setTill(["Cash", "Mobile Money"], {
      default_payment_mode: "Mobile Money",
      enable_payment_without_printing: 1,
      currency: "USD",
    });
    renderDialog(5.9, 5.9);
    await waitFor(() => expect(tender(1).value).toBe("5.9"));

    fireEvent.click(payButton());
    await waitFor(() =>
      expect(screen.getByText(/Payment for Mobile Money has not been confirmed yet/)).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("received-tenders")).toBeNull();
    expect(printOrder).not.toHaveBeenCalled();

    // Switch method to Cash and retry cleanly.
    fireEvent.change(tender(1), { target: { value: "" } });
    fireEvent.focus(tender(0));
    expect(tender(0).value).toBe("5.9");
    fireEvent.click(payButton());

    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        MAKE_INVOICE,
        expect.objectContaining({
          payments: [{ mode_of_payment: "Cash", amount: 5.9 }],
          submit_invoice: 0,
        }),
      ),
    );
    await waitFor(() => expect(submitAttempts).toBe(2));
    await waitFor(() => expect(printOrder).toHaveBeenCalledTimes(1));
  });
});

describe("getRemainingBalance uses the currency's rounding", () => {
  it("rounds USD focus autofill to 2 decimals without IEEE-754 noise (17.7 - 10 = 7.7)", async () => {
    setTill(["Cash", "Mobile Money"], { default_payment_mode: "", currency: "USD" });
    renderDialog(17.7, 17.7);
    fireEvent.change(tender(1), { target: { value: "10" } });
    fireEvent.focus(tender(0));
    expect(tender(0).value).toBe("7.7");
  });

  it("preserves 3 decimal places for 3-decimal currencies without float noise (OMR: 10.3 - 3.125 = 7.175)", async () => {
    setTill(["Cash", "Card"], { default_payment_mode: "", currency: "OMR" });
    renderDialog(10.3, 10.3);
    fireEvent.change(tender(1), { target: { value: "3.125" } });
    fireEvent.focus(tender(0));
    expect(tender(0).value).toBe("7.175");
  });

  it("rounds 0-decimal currencies to whole units (UGX: 19100 - 5000.4 = 14100)", async () => {
    setTill(["Cash", "Mobile Money"], { default_payment_mode: "", currency: "UGX" });
    renderDialog(19100, 19100);
    fireEvent.change(tender(1), { target: { value: "5000.4" } });
    fireEvent.focus(tender(0));
    expect(tender(0).value).toBe("14100");
  });

  it("formats and rounds using POS Profile currency when site default currency differs", async () => {
    setTill(["Cash", "Mobile Money"], { default_payment_mode: "Cash", currency: "UGX" }, { currency: "USD" });
    renderDialog(19057, 19100);
    await waitFor(() => expect(tender(0).value).toBe("19100"));
    expect(document.body.textContent).toContain("UGX 19100.00");
    expect(document.body.textContent).not.toContain("USD 19100.00");
  });
});
