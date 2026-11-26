/**
 * Orders' "Print receipt" must print COPY #n and log a URY Bill Reprint Log row
 * via print_bill_copy (including browser and QZ tills without CUPS), while
 * open/draft checks still print a pro-forma guest check via printOrder.
 */
import type { ComponentPropsWithoutRef, ReactNode } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import Orders from "./Orders";
import SettledBills from "./SettledBills";
import { clearAppStorage } from "../../../packages/core/src/storage";
import { showToast } from "@ury/ui";
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
  usePOSStore: Object.assign(() => ({ posProfile: mockPosProfile }), { getState: () => ({ posProfile: mockPosProfile }) }),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
  useLocation: () => ({ pathname: "/orders" }),
  Link: ({ children, to }: { children?: ReactNode; to: string }) => <a href={to}>{children}</a>,
}));

vi.mock("@ury/core", () => ({
  getSessionUser: () => mockRootState.ordersOwner,
  formatCurrency: (amount: number) => `UGX ${amount}`,
  normalizeFrappeTime: (time?: string) => time || "2026-11-23 12:00:00",
  parseFrappeError: (err: unknown, fallback?: string) => (err instanceof Error ? err.message : fallback),
  printWithQz: vi.fn().mockResolvedValue(undefined),
  call: {
    get: vi.fn().mockResolvedValue({ message: { has_permission: true } }),
    post: vi.fn().mockImplementation(async (method: string) => method.endsWith("begin_bill_copy_dispatch") ? {message: {status: "DISPATCHING", render_grant: 'one-use'}} : method.endsWith("confirm_bill_copy")
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

vi.mock("../components/OrderStatusSidebar", () => ({ default: () => <div /> }));
vi.mock("../components/PaymentDialog", () => ({ default: () => <div /> }));
vi.mock("../components/BillSplitDialog", () => ({ default: () => <div /> }));
vi.mock("../components/BillMergeDialog", () => ({ default: () => <div /> }));
vi.mock("../components/HoldFireControls", () => ({ default: () => <div /> }));

describe("Orders settled receipt reprint (COPY #n + URY Bill Reprint Log)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    mockRootState.ordersOwner = "cashier@example.com";
    document.cookie = "user_id=cashier%40example.com; path=/";
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
    const btn = screen.getByRole("button", { name: /Print copy/i });
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

  it.each(["Orders", "Settled Bills"])("%s recovers copy #1 after reload and isolates cashier sessions", async page => {
    const { name } = mockRootState.selectedOrder;
    const bill = { ...mockRootState.selectedOrder, pos_profile: "Till", items: [], taxes: [], payments: [] };
    let liveLogin = "login-one";
    vi.mocked(call.get).mockImplementation(async method => method === "ury.www.pos.get_receipt_copy_session"
      ? {message: {user: mockRootState.ordersOwner, receipt_copy_session: liveLogin, csrf_token: "csrf-" + liveLogin}}
      : {message: [bill]});
    const boot = {receipt_copy_session: "login-one"};
    vi.stubGlobal("frappe", {boot});
    const requests: string[] = [];
    const numbers = new Map<string, number>();
    const confirms: string[] = [];
    const navigate = vi.fn();
    const open = vi.spyOn(window, "open").mockReturnValue({closed: true, location: {replace: navigate}} as unknown as Window);
    const printed = vi.spyOn(window, "confirm").mockReturnValue(false);
    const mount = async () => {
      const view = render(page === "Orders" ? <Orders /> : <SettledBills />);
      if (page === "Settled Bills") {
        fireEvent.click(screen.getByRole("button", {name: "Search"}));
        fireEvent.click(await screen.findByRole("button", {name: new RegExp(name)}));
      }
      return view;
    };
    const click = () => fireEvent.click(screen.getByRole("button", {name: /print receipt|print copy|retry copy/i}));
    await vi.mocked(call.post).withImplementation(async (method, args) => {
      const key = args.idempotency_key;
      if (method.endsWith("confirm_bill_copy")) {
        confirms.push(key);
        return { message: {status: "COMPLETED"} };
      }
      if (method.endsWith("begin_bill_copy_dispatch")) return {message: {status: "DISPATCHING", render_grant: "once"}};
      requests.push(key);
      if (numbers.has(key)) return {message: {status: "BROWSER_DISPATCHING", recovery_token: "confirm-only", copy_number: numbers.get(key)}};
      const number = numbers.size + 1;
      numbers.set(key, number);
      return {message: {status: "RESERVED", delivery: "browser", copy_number: number, dispatch_token: "lease"}};
    }, async () => {
      let view = await mount();
      click();
      await waitFor(() => expect(printed).toHaveBeenCalledTimes(1));
      view.unmount();

      printed.mockReturnValue(true);
      view = await mount();
      click();
      await waitFor(() => expect(confirms).toHaveLength(1));
      expect(requests[1]).toBe(requests[0]);
      expect(numbers.size).toBe(1);
      expect(navigate).toHaveBeenCalledTimes(1);
      view.unmount();

      printed.mockReturnValue(false);
      view = await mount();
      click();
      await waitFor(() => expect(printed).toHaveBeenCalledTimes(3));
      expect(requests[2]).not.toBe(requests[0]);
      view.unmount();

      mockRootState.ordersOwner = "second@example.com";
      document.cookie = "user_id=second%40example.com; path=/";
      view = await mount();
      click();
      await waitFor(() => expect(printed).toHaveBeenCalledTimes(4));
      expect(requests[3]).not.toBe(requests[2]);
      view.unmount();

      clearAppStorage();
      view = await mount();
      click();
      await waitFor(() => expect(printed).toHaveBeenCalledTimes(5));
      expect(requests[4]).not.toBe(requests[3]);
      view.unmount();

      // A fresh login in another tab need not run this tab's logout cleanup.
      liveLogin = "login-two";
      view = await mount();
      click();
      await waitFor(() => expect(printed).toHaveBeenCalledTimes(6));
      expect(requests[5]).not.toBe(requests[4]);
      expect(window.csrf_token).toBe("csrf-login-two");
      view.unmount();
    });
    open.mockRestore();
    printed.mockRestore();
    vi.unstubAllGlobals();
  });

  it.each(["Orders", "Settled Bills"])("%s retries the same pending copy when session storage is full", async page => {
    const invoice = mockRootState.selectedOrder.name;
    const bill = {...mockRootState.selectedOrder, pos_profile: "Till", items: [], taxes: [], payments: []};
    vi.stubGlobal("frappe", {boot: {receipt_copy_session: "quota-login"}});
    vi.mocked(call.get).mockImplementation(async method => method === "ury.www.pos.get_receipt_copy_session"
      ? {message: {user: mockRootState.ordersOwner, receipt_copy_session: "quota-login", csrf_token: "new-csrf"}}
      : {message: [bill]});
    const setItem = Storage.prototype.setItem;
    const storage = vi.spyOn(Storage.prototype, "setItem").mockImplementation(function (this: Storage, key, value) {
      if (this === sessionStorage) throw new DOMException("Storage full", "QuotaExceededError");
      setItem.call(this, key, value);
    });
    const printed = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValue(true);
    const open = vi.spyOn(window, "open").mockReturnValue({closed: true, location: {replace: vi.fn()}} as unknown as Window);
    const keys: string[] = [];
    const confirms: string[] = [];
    try {
      await vi.mocked(call.post).withImplementation(async (method, args) => {
        if (method.endsWith("confirm_bill_copy")) {
          confirms.push(args.idempotency_key);
          return {message: {status: "COMPLETED"}};
        }
        if (method.endsWith("begin_bill_copy_dispatch")) return {message: {status: "DISPATCHING", render_grant: "once"}};
        keys.push(args.idempotency_key);
        return {message: keys.length === 1
          ? {status: "RESERVED", delivery: "browser", dispatch_token: "lease", copy_number: 1}
          : {status: "BROWSER_DISPATCHING", recovery_token: "ack", copy_number: 1}};
      }, async () => {
        for (const attempt of [1, 2]) {
          const view = render(page === "Orders" ? <Orders /> : <SettledBills />);
          if (page === "Settled Bills") {
            fireEvent.click(screen.getByRole("button", {name: "Search"}));
            fireEvent.click(await screen.findByRole("button", {name: new RegExp(invoice)}));
          }
          fireEvent.click(screen.getByRole("button", {name: /print receipt|print copy|retry copy/i}));
          await waitFor(() => expect(printed).toHaveBeenCalledTimes(attempt));
          view.unmount();
        }
        expect(keys).toEqual([expect.any(String), keys[0]]);
        expect(confirms).toEqual([keys[0]]);
        expect(open).toHaveBeenCalledOnce();
      });
    } finally {
      storage.mockRestore();
      open.mockRestore();
      printed.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it.each(["Orders", "Settled Bills"])("%s refuses a mounted retry from a previous login until Print copy is clicked", async page => {
    const bill = {...mockRootState.selectedOrder, items: [], payments: [], taxes: [], pos_profile: "Till"};
    let login = "original-login";
    vi.stubGlobal("frappe", {boot: {receipt_copy_session: login}});
    vi.mocked(call.get).mockImplementation(async method => method.endsWith("get_receipt_copy_session")
      ? {message: {user: mockRootState.ordersOwner, receipt_copy_session: login, csrf_token: "csrf-" + login}}
      : {message: [bill]});
    const printed = vi.spyOn(window, "confirm").mockReturnValue(false);
    const open = vi.spyOn(window, "open").mockReturnValue({closed: true, location: {replace: vi.fn()}} as unknown as Window);
    const keys: string[] = [];
    try {
      await vi.mocked(call.post).withImplementation(async (method, args) => {
        if (method.endsWith("print_bill_copy")) {
          keys.push(args.idempotency_key);
          return {message: {status: "RESERVED", delivery: "browser", copy_number: keys.length, dispatch_token: "lease"}};
        }
        return {message: {status: "DISPATCHING", render_grant: "single-use"}};
      }, async () => {
        render(page === "Orders" ? <Orders /> : <SettledBills />);
        if (page === "Settled Bills") {
          fireEvent.click(screen.getByRole("button", {name: "Search"}));
          fireEvent.click(await screen.findByRole("button", {name: new RegExp(bill.name)}));
        }
        const clickCopy = () => act(async () => {
          fireEvent.click(screen.getByRole("button", {name: /print receipt|print copy|retry copy/i}));
        });
        await clickCopy();
        expect(printed).toHaveBeenCalledOnce();
        expect(keys).toHaveLength(1);
        const postsBeforeRetry = vi.mocked(call.post).mock.calls.length;
        const retryLabel = screen.getByRole("button", {name: /print receipt|print copy|retry copy/i}).textContent;
        login = "new-login";
        await clickCopy();
        expect(call.post).toHaveBeenCalledTimes(postsBeforeRetry);
        expect(retryLabel).toMatch(/retry copy/i);
        expect(open).toHaveBeenCalledOnce();
        const notice = /session.*changed|previous.*(session|login)/i;
        if (page === "Orders") expect(showToast.error).toHaveBeenCalledWith(expect.stringMatching(notice));
        else expect(screen.getByText(notice)).toBeTruthy();
        expect(screen.queryByRole("button", {name: /retry copy/i})).toBeNull();
        fireEvent.click(screen.getByRole("button", {name: /^Print copy$/i}));
        await waitFor(() => expect(keys).toHaveLength(2));
        expect(keys[1]).not.toBe(keys[0]);
      });
    } finally {
      open.mockRestore();
      printed.mockRestore();
      vi.unstubAllGlobals();
    }
  });

  it("still uses printOrder for a draft guest check (not print_bill_copy)", async () => {
    mockRootState.selectedStatus = "Draft";
    mockRootState.selectedOrder.status = "Draft";
    mockRootState.selectedOrder.docstatus = 0;
    const printOrderSpy = vi.spyOn(printLib, "printOrder").mockResolvedValue("socket");

    render(<Orders />);
    const btn = screen.getByRole("button", { name: /print bill/i });
    fireEvent.click(btn);

    await waitFor(() => expect(printOrderSpy).toHaveBeenCalledWith(expect.objectContaining({ orderId: "POS-INV-00024" })));
    expect(call.post).not.toHaveBeenCalledWith("ury.ury_pos.api.print_bill_copy", expect.anything());
  });
});
