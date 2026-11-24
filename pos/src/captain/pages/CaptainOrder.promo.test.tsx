import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import CaptainOrder from "./CaptainOrder";

const storeState = vi.hoisted(() => ({
  activeOrders: [
    {
      id: "KH-BURGER-18",
      item: "KH-BURGER-18",
      uniqueId: "KH-BURGER-18-default",
      name: "Classic Beef Burger",
      price: 20000,
      promo_rate: 16000,
      promo_label: "PRLE-0001 Happy Hour 20%",
      quantity: 1,
    },
  ],
  posProfile: { name: "Till A", currency: "UGX", cashier: "Cashier", owner: "Owner" },
  currency: "UGX",
  selectedCustomer: { id: "Walk In", name: "Walk In" },
  selectedRoom: "Main Hall",
  paymentModes: ["Cash"],
  orderComment: "",
  guestLabel: "",
  noOfPax: 2,
  lastModifiedTime: null as string | null,
  orderId: null as string | null,
  isUpdatingOrder: false,
  selectedOrderType: "Dine In",
  clearTableOrder: vi.fn(),
  setNoOfPax: vi.fn(),
  setSelectedItem: vi.fn(),
  removeFromOrder: vi.fn(),
  updateQuantity: vi.fn(),
  updateItemComment: vi.fn(),
  setGuestLabel: vi.fn(),
  setSelectedOrderType: vi.fn(),
  loadTableOrder: vi.fn(),
  isOrderInteractionDisabled: () => false,
}));

vi.mock("../../store/pos-store", () => ({
  usePOSStore: (selector?: (s: typeof storeState) => unknown) =>
    typeof selector === "function" ? selector(storeState) : storeState,
}));
vi.mock("../../store/root-store", () => ({
  useRootStore: (select: (value: { user: { name: string } }) => unknown) =>
    select({ user: { name: "captain@example.com" } }),
}));
vi.mock("../hooks/useTableOrderContext", () => ({
  useTableOrderContext: () => ({
    context: { order: null, assignment: null, table: { is_take_away: 0, restaurant_room: "Main Hall" } },
    permissions: {
      view: true,
      modify: true,
      reduce_items: false,
      remove_items: false,
      reprint_kot: false,
      transfer_table: false,
      transfer_captain: false,
      print_bill: false,
      split_bill: false,
    },
    isContextLoading: false,
    contextError: null,
    isOrderReady: true,
    alreadyOrderedLines: [],
    newOrChangedLines: [],
    reductionPendingLines: [],
    refetchContext: vi.fn(),
    reloadOrder: vi.fn(),
  }),
}));
vi.mock("react-router-dom", () => ({ useNavigate: () => vi.fn(), useParams: () => ({ table: "T-04" }) }));
vi.mock("../../lib/order-api", () => ({
  syncOrder: vi.fn(),
  reprintKot: vi.fn(),
  tableTransfer: vi.fn(),
  captainTransfer: vi.fn(),
  reduceOrderItemQty: vi.fn(),
  isOrderTypeNotAllowedError: () => false,
  isLastItemCannotBeRemovedError: () => false,
}));
vi.mock("../../lib/pos-opening-api", () => ({ parseFrappeError: (_e: unknown, fallback: string) => fallback }));
vi.mock("../../lib/print", () => ({ printOrder: vi.fn() }));
vi.mock("../../lib/invoice-api", () => ({ resolvePrintFormat: vi.fn(() => "default") }));
vi.mock("../../lib/table-api", () => ({ getVacantTablesForBranch: vi.fn() }));
vi.mock("@ury/ui", async (original) => ({
  ...(await original<typeof import("@ury/ui")>()),
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
  Spinner: ({ message }: any) => <div>{message}</div>,
  ConfirmDialog: () => null,
  cn: (...args: any[]) => args.filter(Boolean).join(" "),
  showToast: { error: vi.fn(), success: vi.fn() },
}));
vi.mock("@ury/core", () => ({
  formatCurrency: (amount: number, cur?: string) => `${cur || "UGX"} ${amount}`,
  parseFrappeError: (_e: unknown, fallback = "Error") => fallback,
}));
vi.mock("../components/CaptainMenu", () => ({ default: () => <div data-testid="captain-menu" /> }));
vi.mock("../components/CaptainOrderLine", () => ({ default: () => null }));
vi.mock("../components/CaptainActionsMenu", () => ({ default: () => null }));
vi.mock("../../components/ProductDialog", () => ({ default: () => null }));
vi.mock("../../components/CommentDialog", () => ({ default: () => null }));
vi.mock("../../components/TableTransferDialog", () => ({ default: () => null }));
vi.mock("../../components/CaptainTransferDialog", () => ({ default: () => null }));
vi.mock("../../components/CustomerSelect", () => ({ CustomerSelect: () => null }));
vi.mock("../../components/HoldFireControls", () => ({ HoldFireControls: () => null }));

describe("CaptainOrder Happy Hour / daypart promo totals", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("displays discounted promo_rate subtotal in the sticky cart bar instead of undiscounted list price", () => {
    render(<CaptainOrder />);

    const totalLabels = screen.getAllByText("Total");
    expect(totalLabels.length).toBeGreaterThan(0);
    for (const label of totalLabels) {
      const bar = label.parentElement;
      expect(bar).not.toBeNull();
      expect(bar).toHaveTextContent("UGX 16000");
      expect(bar).not.toHaveTextContent("UGX 20000");
    }
  });
});
