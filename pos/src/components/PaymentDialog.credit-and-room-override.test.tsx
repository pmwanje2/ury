import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { call } from "@ury/core";
import PaymentDialog from "./PaymentDialog";
import { initI18n } from "../i18n";
import { useRootStore } from "../store/root-store";

const METHOD = "kamra.pos_interface.verify_room_charge";
const store = vi.hoisted(() => ({
  state: {} as Record<string, unknown>,
}));

vi.mock("../store/pos-store", () => ({
  usePOSStore: () => store.state,
}));

vi.mock("../lib/print", () => ({
  printOrder: vi.fn().mockResolvedValue("qz"),
}));

vi.mock("@ury/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@ury/core")>();
  return {
    ...actual,
    formatCurrency: (amount: number, code?: string) =>
      `${code || "UGX"} ${Number(amount).toFixed(2)}`,
    call: {
      get: vi.fn(),
      post: vi.fn().mockResolvedValue({ message: { name: "BILL-1", status: "Submitted" } }),
    },
  };
});

vi.mock("@ury/ui", () => ({
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
  Input: ({ size: _size, ...props }: any) => <input {...props} />,
  Textarea: ({ ...props }: any) => <textarea {...props} />,
  Spinner: ({ message }: any) => <div>{message || "Loading..."}</div>,
  cn: (...classes: Array<string | false | null | undefined>) => classes.filter(Boolean).join(" "),
  Dialog: ({ children, open }: any) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: any) => <div>{children}</div>,
  showToast: { success: vi.fn(), error: vi.fn() },
}));

function setProfile(extra: Record<string, unknown> = {}) {
  store.state = {
    paymentModes: ["Room Charge", "Cash"],
    fetchPaymentModes: vi.fn(),
    currency: "UGX",
    posProfile: {
      name: "Till A",
      company: "Hotel Co",
      currency: "UGX",
      customer: "Walk-in Customer",
      enable_discount: 0,
      enable_payment_without_printing: 0,
      allow_partial_payment: 1,
      order_type_customer_map: {},
      default_customer: null,
      room_charge_modes: ["Room Charge"],
      room_lookup_method: METHOD,
      ...extra,
    },
  };
}

function renderDialog(overrides: Partial<React.ComponentProps<typeof PaymentDialog>> = {}) {
  return render(
    <PaymentDialog
      order={{ invoice_printed: 1 }}
      onClose={vi.fn()}
      grandTotal={1500}
      roundedTotal={1500}
      invoice="BILL-1"
      customer="Walk-in Customer"
      posProfile="Till A"
      table={null}
      cashier="cashier@example.com"
      owner="waiter@example.com"
      fetchOrders={vi.fn(async () => {})}
      clearSelectedOrder={vi.fn()}
      {...overrides}
    />,
  );
}

beforeEach(async () => {
  await initI18n("en");
  useRootStore.setState({ user: { name: "cashier@example.com", roles: ["URY Cashier"] } as any });
  vi.mocked(call.get).mockReset();
  vi.mocked(call.post).mockReset();
  vi.mocked(call.post).mockResolvedValue({ message: { name: "BILL-1", status: "Submitted" } });
  setProfile();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("POS room charge lookup: surname/shared room, limit & balance, ManagerApproval override", () => {
  it("looks up by surname, lets cashier pick a sharer, and displays posting allowed, limit and balance", async () => {
    vi.mocked(call.get).mockResolvedValue({
      message: {
        ok: true,
        shared_room: true,
        posting_allowed: false,
        matches: [
          {
            reservation: "RES-101-A",
            room_number: "101",
            guest_name: "Asha R.",
            departure: "2026-11-26",
            posting_allowed: true,
            no_post: 0,
            limit: 2000,
            balance: 400,
            available: 1600,
            currency: "UGX",
            override_required: false,
            reason: null,
          },
          {
            reservation: "RES-101-B",
            room_number: "101",
            guest_name: "Chidi O.",
            departure: "2026-11-26",
            posting_allowed: false,
            no_post: 1,
            limit: 2000,
            balance: 0,
            available: 2000,
            currency: "UGX",
            override_required: true,
            reason_code: "POS-RC-NO-POST",
            reason: "This guest's bill is closed to room charges.",
          },
        ],
      },
    });

    renderDialog();
    const inputs = document.querySelectorAll<HTMLInputElement>('input[type="number"]');
    fireEvent.change(inputs[0], { target: { value: "1500" } });
    fireEvent.change(document.querySelector<HTMLInputElement>("#room-charge-reference")!, {
      target: { value: "Rao" },
    });
    fireEvent.click(screen.getByRole("button", { name: /check room/i }));

    await waitFor(() =>
      expect(call.get).toHaveBeenCalledWith(
        METHOD,
        expect.objectContaining({ pos_profile: "Till A", surname: "Rao" }),
      ),
    );
    // Both sharers listed; picking Asha R. shows limit, balance, posting allowed and sends reservation
    const pickAsha = await screen.findByRole("button", { name: /Asha R\./i });
    fireEvent.click(pickAsha);
    expect(screen.getByText(/2000\.00/)).toBeInTheDocument();
    expect(screen.getByText(/400\.00/)).toBeInTheDocument();

    const payBtn = screen.getByRole("button", { name: /^Pay / }) as HTMLButtonElement;
    expect(payBtn.disabled).toBe(false);
    fireEvent.click(payBtn);
    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        "ury.ury.doctype.ury_order.ury_order.make_invoice",
        expect.objectContaining({
          room_number: "101",
          room_charge_stay: "RES-101-A",
        }),
      ),
    );
  });

  it("over-limit or No Post opens ManagerApproval (room_charge_over_limit) and sends the consumed token", async () => {
    vi.mocked(call.get).mockImplementation(async (method: string) => {
      if (method === METHOD) {
        return {
          message: {
            ok: true,
            reservation: "RES-108",
            room_number: "108",
            guest_name: "Susan K.",
            departure: "2026-11-26",
            posting_allowed: false,
            no_post: 0,
            limit: 1000,
            balance: 250,
            available: 750,
            currency: "UGX",
            override_required: true,
            reason_code: "POS-RC-OVER-LIMIT",
            reason: "Charge exceeds room charge limit (1,000 UGX).",
            matches: [
              {
                reservation: "RES-108",
                room_number: "108",
                guest_name: "Susan K.",
                departure: "2026-11-26",
                posting_allowed: false,
                no_post: 0,
                limit: 1000,
                balance: 250,
                available: 750,
                currency: "UGX",
                override_required: true,
                reason_code: "POS-RC-OVER-LIMIT",
                reason: "Charge exceeds room charge limit (1,000 UGX).",
              },
            ],
          },
        };
      }
      if (method === "ury.ury_pos.override.privilege_policy") {
        return { message: { privilege: "room_charge_over_limit", label: "Room charge over limit", may_perform: false, requires_reason: true } };
      }
      if (method === "ury.ury_pos.override.list_approvers") {
        return { message: [{ user: "duty@hotel.test", full_name: "Amina Duty" }] };
      }
      if (method === "ury.ury_pos.override.list_reasons") {
        return { message: [{ code: "OTHER", description: "Other", requires_note: 1, applies_to: [] }] };
      }
      return { message: [] };
    });

    vi.mocked(call.post).mockImplementation(async (method: string) => {
      if (method === "ury.ury_pos.override.authorise") {
        return {
          message: {
            token: "rc-approved-tok",
            approver: "duty@hotel.test",
            approver_name: "Amina Duty",
            expires_in: 120,
          },
        };
      }
      return { message: { name: "BILL-1", status: "Submitted" } };
    });

    renderDialog();
    const inputs = document.querySelectorAll<HTMLInputElement>('input[type="number"]');
    fireEvent.change(inputs[0], { target: { value: "1500" } });
    fireEvent.change(document.querySelector<HTMLInputElement>("#room-charge-reference")!, {
      target: { value: "108" },
    });
    fireEvent.click(screen.getByRole("button", { name: /check room/i }));

    await waitFor(() => expect(screen.getByText(/1000\.00/)).toBeInTheDocument());
    expect(screen.getByText(/250\.00/)).toBeInTheDocument();

    // Open ManagerApproval via override button
    const overrideBtn = await screen.findByRole("button", { name: /manager approval|override/i });
    fireEvent.click(overrideBtn);

    // ManagerApproval dialog appears with approver list, reason picker, and 3x4 PIN keypad
    await screen.findByText(/manager authorisation required/i);
    const reasonSelect = await screen.findByLabelText(/reason/i);
    fireEvent.change(reasonSelect, { target: { value: "OTHER" } });
    const noteInput = await screen.findByLabelText(/note/i);
    fireEvent.change(noteInput, { target: { value: "VIP guest dinner" } });
    for (const digit of ["4", "3", "2", "1"]) {
      fireEvent.click(screen.getByRole("button", { name: digit }));
    }
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));

    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        "ury.ury_pos.override.authorise",
        expect.objectContaining({
          pos_profile: "Till A",
          privilege: "room_charge_over_limit",
          approver: "duty@hotel.test",
          pin: "4321",
          reference_doctype: "POS Invoice",
          reference_name: "BILL-1",
        }),
      ),
    );

    // Pay now sends the consumed approval token
    const payBtn = screen.getByRole("button", { name: /^Pay / }) as HTMLButtonElement;
    expect(payBtn.disabled).toBe(false);
    fireEvent.click(payBtn);

    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        "ury.ury.doctype.ury_order.ury_order.make_invoice",
        expect.objectContaining({
          room_number: "108",
          room_charge_stay: "RES-108",
          room_charge_approval: "rc-approved-tok",
        }),
      ),
    );
  });
});

describe("POS On-Account over-limit opens ManagerApproval (on_account_over_limit), never silent bypass", () => {
  it("opens ManagerApproval when shortfall exceeds account available credit and sends on_account_approval", async () => {
    setProfile({ room_charge_modes: [], room_lookup_method: "" });
    store.state = { ...store.state, paymentModes: ["Cash"] };

    vi.mocked(call.get).mockImplementation(async (method: string) => {
      if (method === "ury.ury_pos.api.credit_accounts") {
        return {
          message: [
            {
              name: "AC-VIP",
              customer_name: "Acme VIP",
              balance: 900,
              limit: 1000,
              available: 100,
              currency: "UGX",
            },
          ],
        };
      }
      if (method === "ury.ury_pos.override.privilege_policy") {
        return { message: { privilege: "on_account_over_limit", label: "On-account over limit", may_perform: false, requires_reason: true } };
      }
      if (method === "ury.ury_pos.override.list_approvers") {
        return { message: [{ user: "fin@hotel.test", full_name: "Grace Finance" }] };
      }
      if (method === "ury.ury_pos.override.list_reasons") {
        return { message: [{ code: "OTHER", description: "Other", requires_note: 1, applies_to: [] }] };
      }
      return { message: [] };
    });

    vi.mocked(call.post).mockImplementation(async (method: string) => {
      if (method === "ury.ury_pos.override.authorise") {
        return {
          message: {
            token: "acct-approved-tok",
            approver: "fin@hotel.test",
            approver_name: "Grace Finance",
            expires_in: 120,
          },
        };
      }
      return { message: { name: "BILL-1", status: "Submitted" } };
    });

    renderDialog({ grandTotal: 500, roundedTotal: 500 });
    fireEvent.change(screen.getByRole("searchbox", { name: /charge to account/i }), {
      target: { value: "Acme" },
    });
    const acctBtn = await screen.findByRole("button", { name: /AC-VIP/i });
    fireEvent.click(acctBtn);

    // Clear cash input so full 500 goes on account (> available 100)
    const cashInput = document.querySelector<HTMLInputElement>('input[type="number"]')!;
    fireEvent.change(cashInput, { target: { value: "0" } });

    // Clicking Pay must NOT silently call make_invoice; it opens ManagerApproval!
    fireEvent.click(screen.getByRole("button", { name: /^Pay / }));
    await screen.findByText(/manager authorisation required/i);
    expect(call.post).not.toHaveBeenCalledWith(
      "ury.ury.doctype.ury_order.ury_order.make_invoice",
      expect.anything(),
    );

    const reasonSelect = await screen.findByLabelText(/reason/i);
    fireEvent.change(reasonSelect, { target: { value: "OTHER" } });
    const noteInput = await screen.findByLabelText(/note/i);
    fireEvent.change(noteInput, { target: { value: "Corporate PO pending" } });
    for (const digit of ["4", "3", "2", "1"]) {
      fireEvent.click(screen.getByRole("button", { name: digit }));
    }
    fireEvent.click(screen.getByRole("button", { name: /^approve$/i }));

    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        "ury.ury_pos.override.authorise",
        expect.objectContaining({
          pos_profile: "Till A",
          privilege: "on_account_over_limit",
          approver: "fin@hotel.test",
        }),
      ),
    );

    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        "ury.ury.doctype.ury_order.ury_order.make_invoice",
        expect.objectContaining({
          customer: "AC-VIP",
          on_account_approval: "acct-approved-tok",
        }),
      ),
    );
  });
});
