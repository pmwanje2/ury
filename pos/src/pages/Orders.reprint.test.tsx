/**
 * Orders' "Print receipt" must print COPY #n and log a URY Bill Reprint Log row
 * via print_bill_copy (including browser and QZ tills without CUPS), while
 * open/draft checks still print a pro-forma guest check via printOrder.
 */
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Orders from "./Orders";
import * as printLib from "../lib/print";
import { call, printWithQz } from "@ury/core";
import type { PosProfileCombined } from "../lib/pos-profile-api";

vi.mock("@ury/ui", () => ({
  Spinner: () => <div data-testid="spinner" />,
  Button: ({ children, ...props }: ComponentPropsWithoutRef<"button">) => <button {...props}>{children}</button>,
  Badge: ({ children }: { children?: ReactNode }) => <span>{children}</span>,
  Card: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  CardContent: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  Dialog: ({ children, open }: { children?: ReactNode; open?: boolean }) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  DialogHeader: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  DialogTitle: ({ children }: { children?: ReactNode }) => <h2>{children}</h2>,
  DialogDescription: ({ children }: { children?: ReactNode }) => <p>{children}</p>,
  DialogFooter: ({ children }: { children?: ReactNode }) => <div>{children}</div>,
  showToast: { success: vi.fn(), error: vi.fn(), info: vi.fn() },
  Textarea: (props: ComponentPropsWithoutRef<"textarea">) => <textarea {...props} />,
}));

const mockRootState = {
  ordersOwner: "cashier@example.com",
  orders: [],
  ordersOffline: false,
  orderLoading: false,
  error: null,
  selectedStatus: "Recently Paid",
  pagination: { currentPage: 1, hasNextPage: false },
  selectedOrder: {
    name: "POS-INV-00024",
    status: "Paid",
    docstatus: 1,
    invoice_printed: 1,
    grand_total: 25000,
    rounded_total: 25000,
    net_total: 21186,
    total_taxes_and_charges: 3814,
    posting_date: "2026-11-23",
    posting_time: "12:00:00",
    customer: "Walk In",
    customer_name: "Walk In",
    order_type: "Dine In",
    restaurant_table: "T1",
    cashier: "cashier@example.com",
    waiter: "waiter@example.com",
    mobile_number: "",
  },
  selectedOrderItems: [{ name: "ROW-1", item_name: "Tea", qty: 1, rate: 25000, price_list_rate: 25000, amount: 25000 }],
  selectedOrderTaxes: [],
  selectedOrderLoading: false,
  selectedOrderError: null,
  fetchOrders: vi.fn(),
  setSelectedStatus: vi.fn(),
  goToNextPage: vi.fn(),
  goToPreviousPage: vi.fn(),
  selectOrder: vi.fn(),
  clearSelectedOrder: vi.fn(),
  orderSearchQuery: "",
};

vi.mock("../store/root-store", () => ({
  useRootStore: vi.fn(() => mockRootState),
}));

const mockPosProfile = {
  name: "Restaurant Till",
  print_type: "socket",
  print_format: "Restaurant Receipt 80mm",
  qz_print: 0,
  qz_host: null,
  printer: null,
  cashier: "cashier@example.com",
  multiple_cashier: 0,
} as unknown as PosProfileCombined;

vi.mock("../store/pos-store", () => ({
  usePOSStore: () => ({ posProfile: mockPosProfile }),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
  useLocation: () => ({ pathname: "/orders" }),
  Link: ({ children, to }: { children?: ReactNode; to: string }) => <a href={to}>{children}</a>,
}));

vi.mock("@ury/core", () => ({
  getSessionUser: () => "cashier@example.com",
  formatCurrency: (amount: number) => `UGX ${amount}`,
  normalizeFrappeTime: (time?: string) => time || "2026-11-23 12:00:00",
  parseFrappeError: (err: unknown, fallback?: string) => (err instanceof Error ? err.message : fallback),
  printWithQz: vi.fn().mockResolvedValue(undefined),
  call: {
    get: vi.fn().mockResolvedValue({ message: { has_permission: true } }),
    post: vi.fn().mockImplementation(async (method: string) => method.endsWith("begin_bill_copy_dispatch") ? {message: {status: "DISPATCHING"}} : method.endsWith("confirm_bill_copy")
      ? { message: { status: "COMPLETED" } } : ({
      message: {
        status: "RESERVED", dispatch_token: "attempt-1",
        copy_number: 2,
        delivery: "browser",
        html: "<article><h1>COPY #2</h1></article>",
        print_url: "/printview?doctype=POS%20Invoice&name=POS-INV-00024&format=URY%20Bill%20Copy&copy_number=2&no_letterhead=1&trigger_print=1",
      },
    })),
  },
}));

vi.mock("../lib/order-api", () => ({
  splitBill: vi.fn(),
  reduceOrderItemQty: vi.fn(),
}));

vi.mock("../lib/invoice-api", () => ({
  getOrdersTabForInvoice: vi.fn(),
  getOpenCheckCounts: vi.fn().mockResolvedValue({}),
  getSplitGroup: vi.fn(),
  getCombinedOrderTotals: vi.fn(() => ({ grandTotal: 25000, roundedTotal: 25000 })),
  isMergedBill: vi.fn(() => false),
  resolvePrintFormat: vi.fn(() => "Restaurant Receipt 80mm"),
  mapSplitGroupInvoiceToPOSInvoice: vi.fn(),
  getInvoicePrintHtml: vi.fn().mockResolvedValue("<html/>"),
  networkPrint: vi.fn(),
  selectNetworkPrinter: vi.fn(),
  updatePrintStatus: vi.fn(),
}));

vi.mock("../lib/table-utils", () => ({
  formatMergedTableLabel: vi.fn((table: string) => table),
}));

vi.mock("../i18n", () => ({
  t: (key: string) => key,
}));

vi.mock("../components/OrderStatusSidebar", () => ({ default: () => <div /> }));
vi.mock("../components/PaymentDialog", () => ({ default: () => <div /> }));
vi.mock("../components/BillSplitDialog", () => ({ default: () => <div /> }));
vi.mock("../components/BillMergeDialog", () => ({ default: () => <div /> }));
vi.mock("../components/HoldFireControls", () => ({ default: () => <div /> }));

describe("Orders settled receipt reprint (COPY #n + URY Bill Reprint Log)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRootState.selectedStatus = "Recently Paid";
    mockRootState.selectedOrder.status = "Paid";
    mockRootState.selectedOrder.docstatus = 1;
    mockPosProfile.print_type = "socket";
    mockPosProfile.qz_host = null;
  });

  it("prints a settled order via print_bill_copy (COPY #n) instead of printOrder", async () => {
    const navigate = vi.fn();
  const openSpy = vi.spyOn(window, "open").mockReturnValue({ closed: true, location: { replace: navigate } } as unknown as Window);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const printOrderSpy = vi.spyOn(printLib, "printOrder");

    render(<Orders />);
    const btn = screen.getByRole("button", { name: /order\.print_receipt/ });
    fireEvent.click(btn);

    await waitFor(() =>
      expect(call.post).toHaveBeenCalledWith(
        "ury.ury_pos.api.print_bill_copy",
        expect.objectContaining({
          invoice: "POS-INV-00024",
          delivery: "browser",
          idempotency_key: expect.any(String),
        }),
      ),
    );
    expect(printOrderSpy).not.toHaveBeenCalled();
    expect(openSpy).toHaveBeenCalledWith("about:blank", "_blank");
    expect(navigate).toHaveBeenCalledWith(expect.stringContaining("copy_number=2"));
    openSpy.mockRestore();
  });

  it("sends COPY #n HTML to QZ Tray on a QZ till without requiring CUPS", async () => {
    vi.mocked(call.post).mockResolvedValueOnce({
      message: {
        status: "RESERVED", dispatch_token: "attempt-1",
        copy_number: 3,
        delivery: "qz",
        html: "<article><h1>COPY #3</h1></article>",
      },
    });
    const qzProfile = { ...mockPosProfile, print_type: "qz", qz_host: "localhost", printer: "Cashier" } as PosProfileCombined;

    const fn = (printLib as Record<string, unknown>).printBillCopy as
      | ((args: { orderId: string; posProfile: PosProfileCombined; idempotencyKey: string }) => Promise<{ copy_number?: number }>)
      | undefined;
    expect(fn, "printBillCopy helper must be exported from src/lib/print.ts").toBeTypeOf("function");

    const res = await fn!({ orderId: "POS-INV-00024", posProfile: qzProfile, idempotencyKey: "k-qz-1" });
    expect(call.post).toHaveBeenCalledWith(
      "ury.ury_pos.api.print_bill_copy",
      { invoice: "POS-INV-00024", idempotency_key: "k-qz-1", delivery: "qz" },
    );
    expect(printWithQz).toHaveBeenCalledWith("localhost", "<article><h1>COPY #3</h1></article>", "Cashier", expect.any(Function));
    expect(call.post).toHaveBeenLastCalledWith("ury.ury_pos.api.confirm_bill_copy",
      { invoice: "POS-INV-00024", idempotency_key: "k-qz-1", dispatch_token: "attempt-1" });
    expect(vi.mocked(call.post).mock.invocationCallOrder.at(-1)).toBeGreaterThan(vi.mocked(printWithQz).mock.invocationCallOrder[0]);
    expect(res.copy_number).toBe(3);
  });

  it("still uses printOrder for a draft guest check (not print_bill_copy)", async () => {
    mockRootState.selectedStatus = "Draft";
    mockRootState.selectedOrder.status = "Draft";
    mockRootState.selectedOrder.docstatus = 0;
    const printOrderSpy = vi.spyOn(printLib, "printOrder").mockResolvedValue("socket");

    render(<Orders />);
    const btn = screen.getByRole("button", { name: /order\.print_bill/ });
    fireEvent.click(btn);

    await waitFor(() => expect(printOrderSpy).toHaveBeenCalledWith(expect.objectContaining({ orderId: "POS-INV-00024" })));
    expect(call.post).not.toHaveBeenCalledWith("ury.ury_pos.api.print_bill_copy", expect.anything());
  });
});
