import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { showToast } from "@ury/ui";
import Orders from "./Orders";

// Mock UI components first
vi.mock("@ury/ui", () => ({
  Spinner: () => <div data-testid="spinner">Loading...</div>,
  Button: ({ children, onClick, ...props }: any) => (
    <button onClick={onClick} {...props}>
      {children}
    </button>
  ),
  Badge: ({ children }: any) => (
    <span data-testid="badge">{children}</span>
  ),
  Card: ({ children }: any) => <div data-testid="card">{children}</div>,
  CardContent: ({ children }: any) => <div>{children}</div>,
  Dialog: ({ children, open }: any) => open ? <div data-testid="dialog">{children}</div> : null,
  DialogContent: ({ children }: any) => <div>{children}</div>,
  DialogHeader: ({ children }: any) => <div>{children}</div>,
  DialogTitle: ({ children }: any) => <h2>{children}</h2>,
  DialogDescription: ({ children }: any) => <p>{children}</p>,
  DialogFooter: ({ children }: any) => <div>{children}</div>,
  showToast: {
    success: vi.fn(),
    error: vi.fn(),
  },
  Textarea: ({ value, onChange, ...props }: any) => (
    <textarea value={value} onChange={onChange} {...props} />
  ),
}));

// Create a mock store object with correct pagination structure
const mockRootStoreState = {
  ordersOwner: "cashier@example.com",
  orders: [],
  ordersOffline: false,
  orderLoading: false,
  error: null,
  selectedStatus: "Unbilled",
  pagination: { currentPage: 1, hasNextPage: false },
  selectedOrder: null,
  selectedOrderItems: [],
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
  useRootStore: vi.fn(() => mockRootStoreState),
}));

const mockPosStore: {
  posProfile: { name: string; enable_payment_without_printing?: number } | null;
  [key: string]: unknown;
} = {
  posProfile: { name: "POS-001" },
};

vi.mock("../store/pos-store", () => ({
  usePOSStore: () => mockPosStore,
}));

// Mock react-router
const navigateMock = vi.fn();
vi.mock("react-router-dom", () => ({
  useNavigate: () => navigateMock,
  useLocation: () => ({ pathname: "/orders", search: "", hash: "", state: null, key: "default" }),
  Link: ({ children, to, ...props }: any) => (
    <a href={typeof to === "string" ? to : "#"} {...props}>
      {children}
    </a>
  ),
}));

// Mock core functions
vi.mock("@ury/core", () => ({
  getSessionUser: () => "cashier@example.com",
  formatCurrency: (amount: number) => `Rs. ${amount}`,
  normalizeFrappeTime: (time: any) => time || "2026-11-03 12:00:00",
  parseFrappeError: (err: any) => (err instanceof Error ? err.message : String(err || "")),
  call: {
    get: vi.fn().mockResolvedValue({ message: { has_permission: true } }),
  },
}));

// Mock order API
vi.mock("../lib/order-api", () => ({
  splitBill: vi.fn().mockResolvedValue(undefined),
  reduceOrderItemQty: vi.fn().mockResolvedValue({cancel_kot_names: []}),
}));

// Mock invoice API
vi.mock("../lib/invoice-api", () => ({
  getOrdersTabForInvoice: vi.fn(),
  getSplitGroup: vi.fn(),
  getOpenCheckCounts: vi.fn().mockResolvedValue({}),
  getCombinedOrderTotals: vi.fn(() => ({
    grandTotal: 1000,
    roundedTotal: 1000,
  })),
  isMergedBill: vi.fn(() => false),
  resolvePrintFormat: vi.fn(() => "Thermal"),
  mapSplitGroupInvoiceToPOSInvoice: vi.fn(),
}));

// Mock print
vi.mock("../lib/print", () => ({
  printOrder: vi.fn().mockResolvedValue(undefined),
}));

// Mock table utils
vi.mock("../lib/table-utils", () => ({
  formatMergedTableLabel: vi.fn((table) => table),
}));

// Mock i18n with proper number formatting support
vi.mock("../i18n", () => ({
  t: (key: string, opts?: any) => {
    if (key === 'orders.pagination.page' && opts?.number) {
      return `Page ${opts.number}`;
    }
    if (key === 'orders.reduce_item_qty' && opts?.item) {
      return `Reduce quantity of ${opts.item}`;
    }
    return key;
  },
}));

// Mock components
vi.mock("../components/OrderStatusSidebar", () => ({
  default: () => <div data-testid="order-status-sidebar" />,
}));

vi.mock("../components/PaymentDialog", () => ({
  default: () => <div data-testid="payment-dialog" />,
}));

vi.mock("../components/BillSplitDialog", () => ({
  default: () => <div data-testid="split-dialog" />,
}));

vi.mock("../components/BillMergeDialog", () => ({
  default: () => <div data-testid="merge-dialog" />,
}));

vi.mock("../components/OrderActionsMenu", () => ({
  default: () => <div data-testid="actions-menu" />,
}));

vi.mock("../components/SplitGroupPanel", () => ({
  default: () => <div data-testid="split-group-panel" />,
}));

vi.mock("../components/MergedBillPanel", () => ({
  default: () => <div data-testid="merged-bill-panel" />,
}));

vi.mock("../components/ManagerApproval", () => ({
  default: ({ open, amount, claim }: any) =>
    open ? (
      <div
        data-testid="manager-approval-dialog"
        data-amount={String(amount)}
        data-claim={JSON.stringify(claim)}
      />
    ) : null,
}));

describe("Orders Page", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRootStoreState.ordersOffline = false;
    mockRootStoreState.fetchOrders.mockClear();
  });

  it("renders the orders page", () => {
    render(<Orders />);

    expect(screen.getByTestId("order-status-sidebar")).toBeInTheDocument();
  });

  it("fetches orders on mount", async () => {
    render(<Orders />);

    await waitFor(() => {
      expect(mockRootStoreState.fetchOrders).toHaveBeenCalled();
    });
  });

  it("shows loading spinner when orders are loading", () => {
    mockRootStoreState.orderLoading = true;

    render(<Orders />);

    expect(screen.getByTestId("spinner")).toBeInTheDocument();

    mockRootStoreState.orderLoading = false;
  });

  it("renders with empty orders list initially", async () => {
    mockRootStoreState.orders = [];

    render(<Orders />);

    await waitFor(() => {
      expect(mockRootStoreState.fetchOrders).toHaveBeenCalled();
    });
  });

  it("has OrderStatusSidebar component", () => {
    render(<Orders />);

    expect(screen.getByTestId("order-status-sidebar")).toBeInTheDocument();
  });

  it("displays no order message when no order is selected", () => {
    mockRootStoreState.selectedOrder = null;
    render(<Orders />);

    expect(screen.getByText(/order.select_to_view/i).closest(".fixed")).toBeNull();
  });

  it("shows only the outage notice instead of the detail pane that covers phone navigation", () => {
    mockRootStoreState.ordersOffline = true;
    const { rerender } = render(<Orders />);

    expect(screen.getByRole("status")).toHaveTextContent("offline.orders_unavailable");
    expect(screen.queryByText("order.select_to_view")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "orders.pagination.next" })).not.toBeInTheDocument();

    mockRootStoreState.ordersOffline = false;
    rerender(<Orders />);
    expect(screen.getByText("order.select_to_view")).toBeInTheDocument();
  });

  it("displays pagination controls", () => {
    mockRootStoreState.orders = [];
    render(<Orders />);

    expect(screen.getByText(/orders.pagination.previous/i)).toBeInTheDocument();
    expect(screen.getByText(/orders.pagination.next/i)).toBeInTheDocument();
  });

  it("shows please_print_first error when invoice is not printed and enable_payment_without_printing is not enabled", () => {
    mockPosStore.posProfile = { name: "POS-001", enable_payment_without_printing: 0 };
    mockRootStoreState.selectedOrder = {
      name: "ACC-PSINV-0001",
      status: "Unbilled",
      order_type: "Dine In",
      invoice_printed: 0,
      rounded_total: 100,
    } as any;

    render(<Orders />);

    const paymentBtn = screen.getByRole("button", { name: "order.settle_bill" });
    fireEvent.click(paymentBtn);

    expect(showToast.error).toHaveBeenCalledWith("errors.please_print_first");
    expect(screen.queryByTestId("payment-dialog")).not.toBeInTheDocument();
  });

  it("opens payment dialog when invoice is not printed but enable_payment_without_printing is enabled", () => {
    mockPosStore.posProfile = { name: "POS-001", enable_payment_without_printing: 1 };
    mockRootStoreState.selectedOrder = {
      name: "ACC-PSINV-0001",
      status: "Unbilled",
      order_type: "Dine In",
      invoice_printed: 0,
      rounded_total: 100,
    } as any;

    render(<Orders />);

    const paymentBtn = screen.getByRole("button", { name: "order.settle_bill" });
    fireEvent.click(paymentBtn);

    expect(showToast.error).not.toHaveBeenCalled();
    expect(screen.getByTestId("payment-dialog")).toBeInTheDocument();
  });
});

// Ported from the fork (Orders.test.tsx M03 + guest-label edit): the check's
// guest label leads its card and bill detail, so two splits on one walk-in
// customer are told apart; editing the order keeps its saved label.
describe("Orders guest label", () => {
  const order = {
    name: "ACC-PSINV-0002", status: "Draft", invoice_printed: 1, rounded_total: 100,
    customer: "Walk In", custom_guest_label: "Susan", posting_date: "2026-11-03",
    posting_time: "12:00:00", order_type: "Take Away", restaurant_table: null,
  } as any;

  beforeEach(() => {
    vi.clearAllMocks();
    mockRootStoreState.orders = [order] as any;
    mockRootStoreState.selectedOrder = order;
  });

  it("names the check on its card and in the bill detail, ahead of the walk-in customer", () => {
    render(<Orders />);
    expect(screen.getAllByText("Susan · Walk In")).toHaveLength(2);
  });

  it("reports a reduction without a kitchen dispatch when no cancel ticket was created", async () => {
    mockRootStoreState.selectedOrderItems = [{ name: 'ROW', item_name: 'Soup', qty: 2, rate: 50, amount: 100 }] as never;
    try {
      render(<Orders />);
      fireEvent.click(screen.getByRole('button', {name: 'Reduce quantity of Soup'}));
      await waitFor(() => expect(showToast.success).toHaveBeenCalledWith('Soup reduced to qty 1.'));
    } finally { mockRootStoreState.selectedOrderItems = []; }
  });

  it("without a label the card and detail show the customer as before", () => {
    const plain = { ...order, custom_guest_label: "" };
    mockRootStoreState.orders = [plain] as any;
    mockRootStoreState.selectedOrder = plain;
    render(<Orders />);
    expect(screen.getAllByText("Walk In")).toHaveLength(2);
    expect(screen.queryByText(/ · Walk In/)).not.toBeInTheDocument();
  });

  it("editing the order loads its saved label into the cart", async () => {
    const setGuestLabel = vi.fn();
    Object.assign(mockPosStore, {
      resetOrderState: vi.fn(), setSelectedOrderType: vi.fn(), setOrderForUpdate: vi.fn(),
      setSelectedTable: vi.fn(), setSelectedCustomer: vi.fn(), addToOrder: vi.fn(), setGuestLabel,
    });
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => ({ message: { ...order, items: [] } }),
    })));
    render(<Orders />);
    fireEvent.click(screen.getByRole("button", { name: "orders.edit_order" }));
    await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/register"));
    expect(mockPosStore.setOrderForUpdate).toHaveBeenCalledWith(order.name, order.customer, order.modified, []);
    expect(setGuestLabel).toHaveBeenCalledWith("Susan");
    vi.unstubAllGlobals();
  });
});

describe("Orders modifiers edit and void parity (R1 M2)", () => {
  const order = {
    name: "ACC-PSINV-MOD-1",
    status: "Draft",
    invoice_printed: 1,
    rounded_total: 133000,
    customer: "Walk In",
    custom_guest_label: "",
    posting_date: "2026-11-24",
    posting_time: "14:00:00",
    order_type: "Take Away",
    restaurant_table: null,
  } as any;

  beforeEach(() => {
    vi.clearAllMocks();
    mockRootStoreState.orders = [order] as any;
    mockRootStoreState.selectedOrder = order;
    mockRootStoreState.selectedOrderItems = [];
  });

  it("handleEditOrder preserves modifier and bundle linkage/override fields into the cart", async () => {
    const addToOrder = vi.fn().mockResolvedValue(undefined);
    Object.assign(mockPosStore, {
      resetOrderState: vi.fn(),
      setSelectedOrderType: vi.fn(),
      setOrderForUpdate: vi.fn(),
      setSelectedTable: vi.fn(),
      setSelectedCustomer: vi.fn(),
      addToOrder,
      setGuestLabel: vi.fn(),
    });
    const serverItems = [
      {
        name: "row-rib",
        item_code: "RIBEYE",
        item_name: "Ribeye Steak",
        qty: 1,
        rate: 45000,
        amount: 45000,
        reservation_line_key: "key-rib",
        custom_line_role: "Standard",
        custom_is_modifier: 0,
        custom_modifiers_json: JSON.stringify([
          { group: "Cooking Temperature", option_code: "MED-RARE", label: "Medium Rare", price_mode: "Unpriced Prep", hide_on_receipt: 1 },
        ]),
        custom_hide_unpriced_on_receipt: 1,
      },
      {
        name: "row-ch",
        item_code: "EXTRA-CHEESE",
        item_name: "Aged Cheddar",
        qty: 1,
        rate: 4000,
        amount: 4000,
        reservation_line_key: "key-ch",
        custom_parent_line_key: "key-rib",
        custom_line_role: "Modifier",
        custom_is_modifier: 1,
        custom_modifier_prefix: "Extra",
        custom_modifier_group: "Steak Toppings",
        custom_modifier_option: "EXTRA-CHEESE",
        custom_inherit_parent_routing: 1,
      },
      {
        name: "row-combo",
        item_code: "BURGER-COMBO",
        item_name: "Burger Meal Combo",
        qty: 1,
        rate: 35000,
        amount: 35000,
        reservation_line_key: "key-combo",
        custom_line_role: "Combo Header",
        custom_is_modifier: 0,
        custom_bundle_overrides_json: JSON.stringify([
          { group: "Combo Side", slot_item: "FRIES", chosen_item: "ONION-RINGS", option_code: "ONION-RINGS", label: "Onion Rings", qty: 1 },
        ]),
      },
    ];
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: true,
        json: async () => ({ message: { ...order, items: serverItems } }),
      })),
    );
    try {
      render(<Orders />);
      fireEvent.click(screen.getByRole("button", { name: "orders.edit_order" }));
      await waitFor(() => expect(navigateMock).toHaveBeenCalledWith("/register"));

      expect(addToOrder).toHaveBeenCalledTimes(3);
      expect(addToOrder.mock.calls[0][0]).toMatchObject({
        id: "RIBEYE",
        reservationLineKey: "key-rib",
        isModifier: false,
        hideUnpricedOnReceipt: true,
        selectedModifiers: [expect.objectContaining({ option_code: "MED-RARE", label: "Medium Rare" })],
      });
      expect(addToOrder.mock.calls[1][0]).toMatchObject({
        id: "EXTRA-CHEESE",
        reservationLineKey: "key-ch",
        parentLineKey: "key-rib",
        isModifier: true,
        lineRole: "Modifier",
        modifierPrefix: "Extra",
        modifierGroup: "Steak Toppings",
        modifierOption: "EXTRA-CHEESE",
        inheritParentRouting: true,
      });
      expect(addToOrder.mock.calls[2][0]).toMatchObject({
        id: "BURGER-COMBO",
        lineRole: "Combo Header",
        bundleOverrides: [expect.objectContaining({ slot_item: "FRIES", chosen_item: "ONION-RINGS" })],
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("void approval amount includes linked child modifier rows proportionally", async () => {
    const orderApi = await import("../lib/order-api");
    vi.mocked(orderApi.reduceOrderItemQty).mockRejectedValueOnce(
      new Error("Removing or reducing a sent item requires manager approval. [POS-AUTH-REQUIRED]"),
    );

    mockRootStoreState.selectedOrderItems = [
      {
        name: "row-rib",
        item_code: "RIBEYE",
        item_name: "Ribeye Steak",
        qty: 2,
        rate: 45000,
        price_list_rate: 45000,
        amount: 90000,
        reservation_line_key: "key-rib",
        custom_is_modifier: 0,
      },
      {
        name: "row-ch",
        item_code: "EXTRA-CHEESE",
        item_name: "Aged Cheddar",
        qty: 2,
        rate: 4000,
        price_list_rate: 4000,
        amount: 8000,
        reservation_line_key: "key-ch",
        custom_parent_line_key: "key-rib",
        custom_is_modifier: 1,
        custom_modifier_prefix: "Extra",
      },
    ] as never;

    try {
      render(<Orders />);
      fireEvent.click(screen.getByRole("button", { name: "Reduce quantity of Ribeye Steak" }));
      const approvalDialog = await screen.findByTestId("manager-approval-dialog");
      // 1 × Ribeye (45,000) + proportionally scaled 1 × Aged Cheddar (4,000) = 49,000
      expect(approvalDialog).toHaveAttribute("data-amount", "49000");
      const claim = JSON.parse(approvalDialog.getAttribute("data-claim") || "{}");
      expect(claim.amount).toBe(49000);
    } finally {
      mockRootStoreState.selectedOrderItems = [];
    }
  });
});
