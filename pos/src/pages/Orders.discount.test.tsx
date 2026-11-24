import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import Orders from "./Orders";
import type { CheckDiscountSummary } from "../lib/discount-api";

vi.mock("@ury/ui", () => ({
  Spinner: () => <div data-testid="spinner">Loading...</div>,
  Button: ({ children, onClick, ...props }: any) => (
    <button onClick={onClick} {...props}>
      {children}
    </button>
  ),
  Badge: ({ children }: any) => <span data-testid="badge">{children}</span>,
  Card: ({ children }: any) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: any) => <div>{children}</div>,
  Dialog: ({ children, open }: any) => (open ? <div data-testid="dialog">{children}</div> : null),
  DialogContent: ({ children }: any) => <div>{children}</div>,
  DialogHeader: ({ children }: any) => <div>{children}</div>,
  DialogTitle: ({ children }: any) => <h2>{children}</h2>,
  DialogDescription: ({ children }: any) => <p>{children}</p>,
  DialogFooter: ({ children }: any) => <div>{children}</div>,
  Select: ({ children }: any) => <div>{children}</div>,
  SelectItem: ({ children }: any) => <div>{children}</div>,
  showToast: { success: vi.fn(), error: vi.fn() },
  Textarea: (props: any) => <textarea {...props} />,
}));

const mockDiscounts: CheckDiscountSummary = {
  invoice: "CHK-OPEN-1",
  additional_discount_percentage: 10,
  discount_amount: 4600,
  coupon_code: "LUNCH10",
  reason: "Corporate rate",
  reason_description: "Corporate rate",
  note: "VIP table",
  currency: "UGX",
  precision: 0,
  can_remove_discount: true,
  remove_claim: { action: "remove", row: null, discount_amount: 4600, additional_discount_percentage: 10 },
  line_discounts: [
    {
      row: "ROW-1",
      item_code: "FISH",
      item_name: "Grilled Tilapia",
      discount_percentage: 10,
      discount_amount: 4000,
      line_discount_total: 4000,
      reason: "Guest complaint",
      automatic: false,
    },
  ],
};

const mockRootStoreState: Record<string, any> = {
  ordersOwner: "cashier@example.com",
  ordersBranch: "Lakeside",
  orders: [],
  ordersOffline: false,
  orderLoading: false,
  error: null,
  selectedStatus: "Unbilled",
  pagination: { currentPage: 1, hasNextPage: false },
  selectedOrder: null,
  selectedOrderItems: [],
  selectedOrderTaxes: [],
  selectedOrderDiscounts: null,
  selectedOrderLoading: false,
  selectedOrderError: null,
  fetchOrders: vi.fn().mockResolvedValue(undefined),
  setSelectedStatus: vi.fn(),
  goToNextPage: vi.fn(),
  goToPreviousPage: vi.fn(),
  selectOrder: vi.fn().mockResolvedValue(true),
  clearSelectedOrder: vi.fn(),
  orderSearchQuery: "",
};

vi.mock("../store/root-store", () => ({
  useRootStore: vi.fn(() => mockRootStoreState),
}));

vi.mock("../store/pos-store", () => ({
  usePOSStore: () => ({
    posProfile: { name: "Lakeside Till", currency: "UGX", allow_discount_change: 1, enable_payment_without_printing: 1 },
    currency: "UGX",
  }),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
  useLocation: () => ({ pathname: "/orders" }),
  Link: ({ children }: any) => <a>{children}</a>,
}));

vi.mock("@ury/core", () => ({
  getSessionUser: () => "cashier@example.com",
  formatCurrency: (amount: number, curr?: string) =>
    curr ? `${curr} ${Math.round(amount).toLocaleString("en-US")}` : `Sh ${Math.round(amount).toLocaleString("en-US")}`,
  normalizeFrappeTime: (time: any) => time || "2026-11-24 12:00:00",
  parseFrappeError: (e: any, fallback: string) => e?.message || fallback,
  call: {
    get: vi.fn().mockResolvedValue({ message: { has_permission: true } }),
    post: vi.fn().mockResolvedValue({ message: {} }),
  },
}));

vi.mock("../lib/order-api", () => ({
  splitBill: vi.fn(),
  cancelOrder: vi.fn(),
  reduceOrderItemQty: vi.fn(),
  isOrderTypeNotAllowedError: () => false,
  isLastItemCannotBeRemovedError: () => false,
}));

vi.mock("../lib/invoice-api", () => ({
  getOrdersTabForInvoice: vi.fn(),
  getOpenCheckCounts: vi.fn().mockResolvedValue({}),
  getSplitGroup: vi.fn(),
  getCombinedOrderTotals: vi.fn((order: any) => ({
    grandTotal: order.grand_total || 0,
    roundedTotal: order.rounded_total || order.grand_total || 0,
  })),
  isMergedBill: vi.fn(() => false),
  resolvePrintFormat: vi.fn(() => "Thermal"),
  mapSplitGroupInvoiceToPOSInvoice: vi.fn(),
}));

const removeDiscountMock = vi.fn().mockResolvedValue({});
vi.mock("../lib/discount-api", () => ({
  removeCheckDiscount: (...args: any[]) => removeDiscountMock(...args),
  formatCurrencyPrecision: (amount: number, curr?: string) => `${curr || "UGX"} ${Math.round(amount).toLocaleString("en-US")}`,
}));

vi.mock("../lib/print", () => ({ printOrder: vi.fn() }));
vi.mock("../lib/table-utils", () => ({ formatMergedTableLabel: (t: string) => t }));
vi.mock("../lib/invoice-print-warning", () => ({
  getPrintWarningState: () => ({ exceeded: false, elapsedLabel: "" }),
  formatFrappeDatetime: () => "2026-11-24 12:00",
}));
vi.mock("../lib/use-session-owner", () => ({ useSessionOwner: () => "cashier@example.com" }));
vi.mock("../lib/use-staff-names", () => ({ useStaffNames: () => new Map() }));
vi.mock("../components/OrderStatusSidebar", () => ({ default: () => <div /> }));
vi.mock("../components/BillRequestStrip", () => ({ default: () => <div /> }));
vi.mock("../components/Notifications", () => ({ default: () => <div /> }));
vi.mock("../components/OrderActionsMenu", () => ({ default: () => <div /> }));
vi.mock("../components/SplitGroupPanel", () => ({ default: () => <div /> }));
vi.mock("../components/MergedBillPanel", () => ({ default: () => <div /> }));
vi.mock("../components/BillMergeDialog", () => ({ default: () => <div /> }));
vi.mock("../components/BillSplitDialog", () => ({ default: () => <div /> }));
vi.mock("../components/PaymentDialog", () => ({ default: () => <div data-testid="payment-dialog" /> }));
vi.mock("../components/DiscountPanel", () => ({
  default: ({ open, invoice }: any) =>
    open ? <div data-testid="open-discount-panel">DiscountPanel:{invoice}</div> : null,
}));
vi.mock("../i18n", () => ({
  t: (key: string) => key,
}));

beforeEach(() => {
  removeDiscountMock.mockClear();
  mockRootStoreState.selectOrder.mockClear();
  mockRootStoreState.fetchOrders.mockClear();
});

describe("Orders open-check Discount key and check/line discount lines", () => {
  it("shows Discount key on open checks, renders line and check discount lines with reasons, and opens DiscountPanel", async () => {
    const openOrder = {
      name: "CHK-OPEN-1",
      status: "Draft",
      invoice_printed: 0,
      grand_total: 41400,
      rounded_total: 41400,
      net_total: 35085,
      total_taxes_and_charges: 6315,
      customer: "Walk-in",
      waiter: "waiter@example.com",
      cashier: "cashier@example.com",
      restaurant_table: "T-01",
      posting_date: "2026-11-24",
      posting_time: "12:00:00",
      order_type: "Dine In",
      mobile_number: "",
      additional_discount_percentage: 10,
      discount_amount: 4600,
    };
    mockRootStoreState.orders = [openOrder];
    mockRootStoreState.selectedOrder = openOrder;
    mockRootStoreState.selectedOrderItems = [
      { name: "ROW-1", item_code: "FISH", item_name: "Grilled Tilapia", qty: 1, price_list_rate: 40000, rate: 36000, amount: 36000, discount_percentage: 10, discount_amount: 4000 },
      { name: "ROW-2", item_code: "JUICE", item_name: "Passion Juice", qty: 1, price_list_rate: 10000, rate: 10000, amount: 10000, discount_percentage: 0, discount_amount: 0 },
    ];
    mockRootStoreState.selectedOrderTaxes = [{ description: "VAT 18%", rate: 6315 }];
    mockRootStoreState.selectedOrderDiscounts = mockDiscounts;

    render(<Orders />);

    // Discount key sits directly on the open check bar
    const discBtn = screen.getByTestId("open-check-discount-btn");
    expect(discBtn).toBeInTheDocument();

    // Line discount is rendered as its own row under Grilled Tilapia with its reason
    const lineDiscRow = screen.getByTestId("line-discount-row");
    expect(lineDiscRow).toHaveTextContent("Guest complaint");
    expect(lineDiscRow).toHaveTextContent(/4,000/);

    // Check discount is rendered as its own row with reason and coupon BEFORE Taxes & Charges
    const checkDiscRow = screen.getByTestId("check-discount-row");
    expect(checkDiscRow).toHaveTextContent("Corporate rate");
    expect(checkDiscRow).toHaveTextContent("LUNCH10");
    expect(checkDiscRow).toHaveTextContent(/UGX 4,600/);
    expect(checkDiscRow).not.toHaveTextContent(/\bSh\b/);

    // Clicking Discount opens DiscountPanel for this check
    fireEvent.click(discBtn);
    await waitFor(() => expect(screen.getByTestId("open-discount-panel")).toHaveTextContent("DiscountPanel:CHK-OPEN-1"));
  });

  it("P1-7d: fr, es, and ar locales translate orders.settled_closed and order_status_types.outstanding", async () => {
    const fr = (await import("../i18n/locales/fr.json")).default as any;
    const es = (await import("../i18n/locales/es.json")).default as any;
    const ar = (await import("../i18n/locales/ar.json")).default as any;

    for (const [lang, loc] of [
      ["fr", fr],
      ["es", es],
      ["ar", ar],
    ] as const) {
      expect(loc.orders?.settled_closed, `${lang} orders.settled_closed`).toBeTruthy();
      expect(loc.orders?.settled_closed, `${lang} settled_closed not English`).not.toBe("Settled / Closed");
      expect(loc.order_status_types?.outstanding, `${lang} order_status_types.outstanding`).toBeTruthy();
      expect(loc.order_status_types?.outstanding, `${lang} outstanding not English`).not.toBe("Outstanding");
    }
  });

  it("hides Discount key on settled (Paid) checks", () => {
    const paidOrder = {
      name: "CHK-PAID-1",
      status: "Paid",
      invoice_printed: 1,
      grand_total: 45000,
      rounded_total: 45000,
      net_total: 45000,
      total_taxes_and_charges: 0,
      customer: "Walk-in",
      waiter: "waiter@example.com",
      cashier: "cashier@example.com",
      restaurant_table: null,
      posting_date: "2026-11-24",
      posting_time: "12:00:00",
      order_type: "Take Away",
      mobile_number: "",
    };
    mockRootStoreState.orders = [paidOrder];
    mockRootStoreState.selectedOrder = paidOrder;
    mockRootStoreState.selectedOrderItems = [
      { name: "ROW-1", item_code: "FISH", item_name: "Grilled Tilapia", qty: 1, price_list_rate: 45000, rate: 45000, amount: 45000 },
    ];
    mockRootStoreState.selectedOrderTaxes = [];
    mockRootStoreState.selectedOrderDiscounts = null;

    render(<Orders />);
    expect(screen.queryByTestId("open-check-discount-btn")).toBeNull();
  });
});
