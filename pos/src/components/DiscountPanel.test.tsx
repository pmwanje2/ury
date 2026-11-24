import React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { call } from "@ury/core";
import DiscountPanel from "./DiscountPanel";
import type { POSInvoiceItem } from "../lib/invoice-api";
import type { CheckDiscountSummary, DiscountOptionsResponse, DiscountPreviewResponse } from "../lib/discount-api";
import { assertOfflineActionAllowed } from "../offline/cashier";

vi.mock("@ury/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@ury/core")>();
  return {
    ...actual,
    call: {
      get: vi.fn(),
      post: vi.fn(),
    },
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
  Textarea: (props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) =>
    React.createElement("textarea", props),
  Spinner: ({ message }: { message?: string }) =>
    React.createElement("span", null, message || "Loading"),
  cn: (...classes: Array<string | false | null | undefined>) => classes.filter(Boolean).join(" "),
  Dialog: ({ children, open }: { children?: React.ReactNode; open?: boolean }) =>
    open ? React.createElement("div", { "data-testid": "discount-dialog" }, children) : null,
  DialogContent: ({ children, className }: { children?: React.ReactNode; className?: string }) =>
    React.createElement("div", { className }, children),
  showToast: { success: vi.fn(), error: vi.fn() },
}));

vi.mock("../offline/cashier", () => ({
  assertOfflineActionAllowed: vi.fn().mockResolvedValue(undefined),
}));

vi.mock("../i18n", () => ({
  t: (key: string, params?: Record<string, unknown>) => {
    const map: Record<string, string> = {
      "discount.title": "Discount",
      "discount.tab_check": "Check",
      "discount.tab_line": "Line",
      "discount.presets": "Presets",
      "discount.open_entry": "Open discount",
      "discount.presets_only_notice": "Only configured discount presets are enabled for this till.",
      "discount.line_open_disabled": "Open line discounts are disabled for this till.",
      "discount.percent_off": "% off",
      "discount.amount_off": "Amount ({{currency}})",
      "discount.reason_label": "Discount reason",
      "discount.reason_placeholder": "Select a reason…",
      "discount.note_label": "Note",
      "discount.note_placeholder_required": "Enter details (required)",
      "discount.note_placeholder_optional": "Details (optional)",
      "discount.preview_title": "Check totals",
      "discount.before": "Before",
      "discount.discount_line": "Discount",
      "discount.tax": "Tax",
      "discount.after": "After",
      "discount.apply": "Apply discount",
      "discount.applying": "Applying…",
      "discount.remove": "Remove discount",
      "discount.removing": "Removing…",
      "discount.your_limit": "Your limit: {{limit}}%",
      "discount.manager_required_zero": "Manager approval required",
      "discount.approved_by": "Approved by {{name}}",
      "discount.needs_approval_over_limit": "Over your {{limit}}% limit ({{effective}}%).",
      "discount.needs_approval_comp": "100% complimentary requires a second person's PIN.",
      "discount.needs_approval_reason": "This reason requires manager approval.",
      "discount.manager_approval_title": "Manager authorisation required",
      "discount.remove_approval_title": "Authorise discount removal",
      "discount.manager_label": "Manager",
      "discount.manager_placeholder": "Select manager…",
      "discount.pin_label": "Approval PIN",
      "discount.pin_placeholder": "PIN",
      "discount.authorise": "Authorise",
      "discount.no_approvers": "No manager with an approval PIN can approve here yet.",
      "discount.item_max_cap": "Max {{max}}%",
      "discount.comp_100": "100% Comp",
      "discount.applied_toast": "Discount applied",
      "discount.removed_toast": "Discount removed",
      "manager_approval.title": "Manager approval",
      "manager_approval.subtitle": "Enter manager PIN to approve",
      "manager_approval.manager": "Manager",
      "manager_approval.select_manager": "Select manager",
      "manager_approval.pin": "PIN",
      "manager_approval.approve": "Approve",
      "manager_approval.approving": "Approving…",
      "manager_approval.no_approvers": "No approver available",
      "floor_pin.clear": "Clear",
      "floor_pin.delete": "Delete",
      "common.dismiss": "Dismiss",
      "common.cancel": "Cancel",
      "common.clear": "Clear",
      "common.loading": "Loading",
    };
    let text = map[key] ?? key;
    if (params) {
      for (const [k, v] of Object.entries(params)) {
        text = text.replace(`{{${k}}}`, String(v));
      }
    }
    return text;
  },
}));

const VENDOR_RE = /\b(?:URY|HUF|Kamra)\b/i;

const ITEMS: POSInvoiceItem[] = [
  {
    name: "ROW-1",
    item_code: "FISH",
    item_name: "Grilled Tilapia",
    qty: 1,
    rate: 40000,
    price_list_rate: 40000,
    amount: 40000,
    discount_percentage: 0,
    discount_amount: 0,
    max_discount: 20,
  },
  {
    name: "ROW-2",
    item_code: "JUICE",
    item_name: "Passion Juice",
    qty: 1,
    rate: 10000,
    price_list_rate: 10000,
    amount: 10000,
    discount_percentage: 0,
    discount_amount: 0,
    max_discount: 0,
  },
];

function makeOptions(overrides: Partial<DiscountOptionsResponse> = {}): DiscountOptionsResponse {
  return {
    reasons: [
      { name: "Corporate rate", code: "Corporate rate", description: "Corporate rate", requires_supervisor: 0, requires_note: 0, sort_order: 10 },
      { name: "Complaint", code: "Complaint", description: "Guest complaint", requires_supervisor: 0, requires_note: 0, sort_order: 20 },
      { name: "VIP Comp", code: "VIP Comp", description: "VIP complimentary", requires_supervisor: 1, requires_note: 0, sort_order: 25 },
      { name: "Other", code: "Other", description: "Other reason", requires_supervisor: 0, requires_note: 1, sort_order: 60 },
    ],
    presets: [
      {
        name: "LUNCH10",
        coupon_code: "LUNCH10",
        title: "10% Lunch Promo",
        discount_percentage: 10,
        discount_amount: 0,
        apply_discount_on: "Grand Total",
        discount_reason: "Corporate rate",
      },
    ],
    allow_discount_change: true,
    limit_percent: 10,
    can_remove_discount: true,
    currency: "UGX",
    precision: 0,
    ...overrides,
  };
}

function makePreview(overrides: Partial<DiscountPreviewResponse> = {}): DiscountPreviewResponse {
  return {
    before_total: 50000,
    discount_total: 5000,
    header_discount_amount: 5000,
    line_discount_total: 0,
    tax_total: 6864,
    after_total: 45000,
    grand_total: 45000,
    rounded_total: 45000,
    net_total: 38136,
    effective_percent: 10,
    currency: "UGX",
    precision: 0,
    limit_percent: 10,
    needs_approval: false,
    is_comp: false,
    privilege: "discount",
    approval_reasons: [],
    reason_required: false,
    note_required: false,
    reason: "Complaint",
    reason_description: "Guest complaint",
    coupon_code: null,
    claim: {
      percent: 10,
      amount: null,
      coupon: null,
      lines: [],
      reason: "Complaint",
      note: null,
    },
    lines: [],
    ...overrides,
  };
}

function setupMockCalls({
  options = makeOptions(),
  preview = makePreview(),
  approvers = [{ user: "manager@example.com", full_name: "Maya Manager" }],
}: {
  options?: DiscountOptionsResponse;
  preview?: DiscountPreviewResponse | ((params: Record<string, unknown>) => DiscountPreviewResponse);
  approvers?: Array<{ user: string; full_name: string }>;
} = {}) {
  vi.mocked(call.get).mockImplementation(async (method: string, params?: any) => {
    if (method === "ury.ury.doctype.ury_order.ury_order.list_discount_options") {
      return { message: options };
    }
    if (method === "ury.ury.doctype.ury_order.ury_order.preview_settlement_totals") {
      const p = typeof preview === "function" ? preview(params || {}) : preview;
      return { message: p };
    }
    if (method.endsWith(".list_approvers")) {
      return { message: approvers };
    }
    return { message: null };
  });

  vi.mocked(call.post).mockImplementation(async (method: string) => {
    if (method.endsWith(".authorise")) {
      return {
        message: {
          token: "TOK-VALID-123",
          approver: "manager@example.com",
          approver_name: "Maya Manager",
          expires_in: 120,
        },
      };
    }
    if (method === "ury.ury.doctype.ury_order.ury_order.apply_check_discount") {
      return { message: makePreview() };
    }
    if (method === "ury.ury.doctype.ury_order.ury_order.remove_check_discount") {
      return { message: makePreview({ discount_total: 0, after_total: 50000 }) };
    }
    return { message: {} };
  });
}

beforeEach(() => {
  vi.mocked(call.get).mockReset();
  vi.mocked(call.post).mockReset();
  vi.mocked(assertOfflineActionAllowed).mockResolvedValue(undefined);
});

describe("DiscountPanel (Simphony / SAP / PMS parity)", () => {
  it("loads reasons in sort_order, renders Check and Line tabs, and never shows vendor names", async () => {
    setupMockCalls();
    const { container } = render(
      <DiscountPanel
        open={true}
        invoice="CHK-101"
        posProfile="Lakeside Till"
        items={ITEMS}
        currency="UGX"
        existingDiscounts={null}
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByRole("tab", { name: "Check" })).toBeInTheDocument());
    expect(screen.getByRole("tab", { name: "Line" })).toBeInTheDocument();

    const reasonSelect = screen.getByLabelText("Discount reason") as HTMLSelectElement;
    const optionValues = Array.from(reasonSelect.options).slice(1).map((o) => o.value);
    expect(optionValues).toEqual(["Corporate rate", "Complaint", "VIP Comp", "Other"]);

    expect(container.textContent || "").not.toMatch(VENDOR_RE);
  });

  it("hides open amount/percent keypad when allow_discount_change is false while keeping coupon presets working", async () => {
    const onApplied = vi.fn();
    setupMockCalls({
      options: makeOptions({ allow_discount_change: false }),
      preview: makePreview({ coupon_code: "LUNCH10", reason: "Corporate rate", discount_total: 5000, after_total: 45000 }),
    });

    render(
      <DiscountPanel
        open={true}
        invoice="CHK-101"
        posProfile="Lakeside Till"
        items={ITEMS}
        currency="UGX"
        existingDiscounts={null}
        onClose={vi.fn()}
        onApplied={onApplied}
      />,
    );

    await waitFor(() => expect(screen.getByText("10% Lunch Promo")).toBeInTheDocument());
    // Open keypad & open input MUST NOT be rendered when allow_discount_change is false
    expect(screen.queryByTestId("discount-open-keypad")).toBeNull();
    expect(screen.getByText(/Only configured discount presets are enabled/i)).toBeInTheDocument();

    // Selecting the preset auto-fills its linked reason ("Corporate rate") and calls preview with coupon_code
    fireEvent.click(screen.getByRole("button", { name: /10% Lunch Promo/i }));

    await waitFor(() => {
      const reasonSelect = screen.getByLabelText("Discount reason") as HTMLSelectElement;
      expect(reasonSelect.value).toBe("Corporate rate");
    });

    await waitFor(() =>
      expect(call.get).toHaveBeenCalledWith(
        "ury.ury.doctype.ury_order.ury_order.preview_settlement_totals",
        expect.objectContaining({ invoice: "CHK-101", pos_profile: "Lakeside Till", coupon_code: "LUNCH10" }),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Apply discount" }));
    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        "ury.ury.doctype.ury_order.ury_order.apply_check_discount",
        expect.objectContaining({
          invoice: "CHK-101",
          pos_profile: "Lakeside Till",
          coupon_code: "LUNCH10",
          discount_reason: "Corporate rate",
        }),
      ),
    );
    expect(onApplied).toHaveBeenCalled();
  });

  it("renders open 48px touch keypad, hides decimal key in UGX amount mode, shows live Before/Discount/Tax/After preview, and requires note when reason demands one", async () => {
    setupMockCalls({
      preview: (params) =>
        makePreview({
          before_total: 50000,
          discount_total: Number(params.discount_amount || 5000),
          tax_total: 6864,
          after_total: 50000 - Number(params.discount_amount || 5000),
          note_required: params.discount_reason === "Other",
        }),
    });

    render(
      <DiscountPanel
        open={true}
        invoice="CHK-101"
        posProfile="Lakeside Till"
        items={ITEMS}
        currency="UGX"
        existingDiscounts={null}
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByTestId("discount-open-keypad")).toBeInTheDocument());
    const keypad = screen.getByTestId("discount-open-keypad");

    // Switch to Amount (UGX) mode -> decimal key "." must be hidden for 0-decimal currency
    fireEvent.click(screen.getByRole("button", { name: /Amount \(UGX\)/i }));
    expect(within(keypad).queryByRole("button", { name: "." })).toBeNull();

    // Tap 5, 0, 0, 0 on 48px touch keypad (min-h-12)
    const key5 = within(keypad).getByRole("button", { name: "5" });
    expect(key5.className).toMatch(/min-h-12|h-12|h-14/);
    fireEvent.click(key5);
    fireEvent.click(within(keypad).getByRole("button", { name: "0" }));
    fireEvent.click(within(keypad).getByRole("button", { name: "0" }));
    fireEvent.click(within(keypad).getByRole("button", { name: "0" }));

    await waitFor(() => expect(screen.getByTestId("preview-before")).toHaveTextContent(/50,000/));
    expect(screen.getByTestId("preview-discount")).toHaveTextContent(/5,000/);
    expect(screen.getByTestId("preview-tax")).toHaveTextContent(/6,864/);
    expect(screen.getByTestId("preview-after")).toHaveTextContent(/45,000/);

    // Without reason, Apply is disabled
    const applyBtn = screen.getByRole("button", { name: "Apply discount" }) as HTMLButtonElement;
    expect(applyBtn.disabled).toBe(true);

    // Selecting "Other" (requires_note=1) keeps Apply disabled until a note is entered
    fireEvent.change(screen.getByLabelText("Discount reason"), { target: { value: "Other" } });
    expect(applyBtn.disabled).toBe(true);

    fireEvent.change(screen.getByLabelText("Note"), { target: { value: "Manager approved birthday dessert" } });
    await waitFor(() => expect(applyBtn.disabled).toBe(false));
  });

  it("shows inline ManagerApproval ONLY when preview.needs_approval is true, uses privilege='complimentary' for 100% comp, and clears approval if edited afterward", async () => {
    setupMockCalls({
      preview: (params) => {
        const pct = Number(params.discount_percent || 0);
        const isComp = pct >= 100;
        const needsApproval = pct > 10 || isComp;
        return makePreview({
          before_total: 50000,
          discount_total: Math.round((50000 * pct) / 100),
          after_total: 50000 - Math.round((50000 * pct) / 100),
          effective_percent: pct,
          needs_approval: needsApproval,
          is_comp: isComp,
          privilege: isComp ? "complimentary" : "discount",
          approval_reasons: isComp ? ["comp"] : pct > 10 ? ["over_limit"] : [],
          claim: {
            percent: pct || null,
            amount: null,
            coupon: null,
            lines: [],
            reason: (params.discount_reason as string) || null,
            note: null,
          },
        });
      },
    });

    render(
      <DiscountPanel
        open={true}
        invoice="CHK-202"
        posProfile="Lakeside Till"
        items={ITEMS}
        currency="UGX"
        existingDiscounts={null}
        onClose={vi.fn()}
        onApplied={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByLabelText("Discount reason")).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Discount reason"), { target: { value: "Complaint" } });

    // 5% is within 10% limit -> NO ManagerApproval rendered
    const valueInput = screen.getByLabelText("Open discount") as HTMLInputElement;
    fireEvent.change(valueInput, { target: { value: "5" } });
    await waitFor(() => expect(screen.getByTestId("preview-after")).toHaveTextContent(/47,500/));
    expect(screen.queryByTestId("manager-approval-card")).toBeNull();

    // 100% Comp -> inline approval banner appears; clicking Authorise opens B2a ManagerApproval with privilege="complimentary"
    fireEvent.change(valueInput, { target: { value: "100" } });
    await waitFor(() => expect(screen.getByTestId("manager-approval-card")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Apply discount" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Authorise" }));
    const modal = await screen.findByRole("dialog");

    // Approve via B2a ManagerApproval PIN keypad
    await waitFor(() =>
      expect(call.get).toHaveBeenCalledWith(
        "ury.ury_pos.override.list_approvers",
        expect.objectContaining({ pos_profile: "Lakeside Till", privilege: "complimentary" }),
      ),
    );
    await waitFor(() => expect(within(modal).getByLabelText("Manager")).toBeInTheDocument());
    fireEvent.change(within(modal).getByLabelText("Manager"), { target: { value: "manager@example.com" } });
    fireEvent.click(within(modal).getByRole("button", { name: "4" }));
    fireEvent.click(within(modal).getByRole("button", { name: "3" }));
    fireEvent.click(within(modal).getByRole("button", { name: "2" }));
    fireEvent.click(within(modal).getByRole("button", { name: "1" }));
    fireEvent.click(within(modal).getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(screen.getByText(/Approved by Maya Manager/i)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Apply discount" })).not.toBeDisabled();

    // Changing discount AFTER approval MUST clear the token and require fresh approval!
    fireEvent.change(valueInput, { target: { value: "50" } });
    await waitFor(() => expect(screen.queryByText(/Approved by Maya Manager/i)).toBeNull());
    expect(screen.getByRole("button", { name: "Apply discount" })).toBeDisabled();
  });

  it("supports combining a Line discount with a Check discount and sends both to apply_check_discount", async () => {
    const onApplied = vi.fn();
    setupMockCalls({
      preview: () => makePreview({ discount_total: 6000, after_total: 44000, effective_percent: 12, limit_percent: 25, needs_approval: false }),
      options: makeOptions({ limit_percent: 25 }),
    });

    render(
      <DiscountPanel
        open={true}
        invoice="CHK-303"
        posProfile="Lakeside Till"
        items={ITEMS}
        currency="UGX"
        existingDiscounts={null}
        onClose={vi.fn()}
        onApplied={onApplied}
      />,
    );

    await waitFor(() => expect(screen.getByRole("tab", { name: "Line" })).toBeInTheDocument());

    // Enter 5% check discount on Check tab
    fireEvent.change(screen.getByLabelText("Open discount"), { target: { value: "5" } });

    // Switch to Line tab, select Grilled Tilapia (shows Max 20% cap), enter 10%
    fireEvent.click(screen.getByRole("tab", { name: "Line" }));
    expect(screen.getByText("Grilled Tilapia")).toBeInTheDocument();
    expect(screen.getByText("Max 20%")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Grilled Tilapia/i }));
    fireEvent.change(screen.getByLabelText("Open discount"), { target: { value: "10" } });

    // Select reason and apply
    fireEvent.change(screen.getByLabelText("Discount reason"), { target: { value: "Complaint" } });
    await waitFor(() => expect(screen.getByRole("button", { name: "Apply discount" })).not.toBeDisabled());
    fireEvent.click(screen.getByRole("button", { name: "Apply discount" }));

    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        "ury.ury.doctype.ury_order.ury_order.apply_check_discount",
        expect.objectContaining({
          invoice: "CHK-303",
          discount_percent: 5,
          line_discounts: [{ row: "ROW-1", percent: 10, amount: null }],
          discount_reason: "Complaint",
        }),
      ),
    );
    expect(onApplied).toHaveBeenCalled();
  });

  it("gates Remove discount with discount_remove ManagerApproval when can_remove_discount is false", async () => {
    const onApplied = vi.fn();
    const existing: CheckDiscountSummary = {
      invoice: "CHK-404",
      additional_discount_percentage: 10,
      discount_amount: 5000,
      coupon_code: null,
      reason: "Complaint",
      reason_description: "Guest complaint",
      note: null,
      currency: "UGX",
      precision: 0,
      can_remove_discount: false,
      remove_claim: { action: "remove", row: null, discount_amount: 5000, additional_discount_percentage: 10 },
      line_discounts: [],
    };
    setupMockCalls({ options: makeOptions({ can_remove_discount: false }) });

    render(
      <DiscountPanel
        open={true}
        invoice="CHK-404"
        posProfile="Lakeside Till"
        items={ITEMS}
        currency="UGX"
        existingDiscounts={existing}
        onClose={vi.fn()}
        onApplied={onApplied}
      />,
    );

    await waitFor(() => expect(screen.getByRole("button", { name: "Remove discount" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Remove discount" }));

    // Must open B2a ManagerApproval for discount_remove rather than calling remove_check_discount directly
    const modal = await screen.findByRole("dialog");
    expect(call.post).not.toHaveBeenCalledWith(
      "ury.ury.doctype.ury_order.ury_order.remove_check_discount",
      expect.anything(),
    );

    await waitFor(() => expect(within(modal).getByLabelText("Manager")).toBeInTheDocument());
    fireEvent.change(within(modal).getByLabelText("Manager"), { target: { value: "manager@example.com" } });
    fireEvent.click(within(modal).getByRole("button", { name: "4" }));
    fireEvent.click(within(modal).getByRole("button", { name: "3" }));
    fireEvent.click(within(modal).getByRole("button", { name: "2" }));
    fireEvent.click(within(modal).getByRole("button", { name: "1" }));
    fireEvent.click(within(modal).getByRole("button", { name: "Approve" }));

    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        "ury.ury.doctype.ury_order.ury_order.remove_check_discount",
        expect.objectContaining({
          invoice: "CHK-404",
          pos_profile: "Lakeside Till",
          approval: "TOK-VALID-123",
        }),
      ),
    );
    expect(onApplied).toHaveBeenCalled();
  });
});
