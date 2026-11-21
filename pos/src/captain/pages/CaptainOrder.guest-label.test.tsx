import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import CaptainOrder from "./CaptainOrder";
import { syncOrder } from "../../lib/order-api";

// Ported from the fork (guest-label-carry-through.test.tsx, Captain order pad,
// M03): the waiter's pad has the same 40-character guest label box as the
// cashier cart, and clearing it on an existing order clears it on the server.

const state = vi.hoisted(() => ({
  activeOrders: [{ id: "TEA", item: "TEA", uniqueId: "ROW-1", name: "Tea", price: 100, quantity: 1 }],
  posProfile: { name: "Till A", cashier: "Cashier", owner: "Owner" },
  selectedCustomer: { id: "Walk In", name: "Walk In" }, selectedRoom: "Dining", paymentModes: ["Cash"],
  orderComment: "", guestLabel: "", noOfPax: 2, lastModifiedTime: "2026-10-22 10:00:00",
  orderId: null as string | null, isUpdatingOrder: false, selectedOrderType: "Dine In",
  clearTableOrder: vi.fn(), setNoOfPax: vi.fn(), setSelectedItem: vi.fn(), removeFromOrder: vi.fn(),
  updateQuantity: vi.fn(), updateItemComment: vi.fn(), setGuestLabel: vi.fn(),
  setSelectedOrderType: vi.fn(), loadTableOrder: vi.fn(),
}));

vi.mock("../../store/pos-store", () => ({
  usePOSStore: () => ({ ...state, isOrderInteractionDisabled: () => false }),
}));
vi.mock("../../store/root-store", () => ({
  useRootStore: (select: (value: { user: { name: string } }) => unknown) => select({ user: { name: "waiter@example.com" } }),
}));
vi.mock("../hooks/useTableOrderContext", () => ({
  useTableOrderContext: () => ({
    context: { order: null, assignment: null, table: { is_take_away: 0 } },
    permissions: { view: true, modify: true, reduce_items: false, remove_items: false, reprint_kot: false,
      transfer_table: false, transfer_captain: false, print_bill: false, split_bill: false },
    isContextLoading: false, contextError: null, isOrderReady: true,
    alreadyOrderedLines: [], newOrChangedLines: [], reductionPendingLines: [],
  }),
}));
vi.mock("react-router-dom", () => ({ useNavigate: () => vi.fn(), useParams: () => ({ table: "T-1" }) }));
vi.mock("../../lib/order-api", () => ({
  syncOrder: vi.fn(), reprintKot: vi.fn(), tableTransfer: vi.fn(), captainTransfer: vi.fn(),
  reduceOrderItemQty: vi.fn(), isOrderTypeNotAllowedError: () => false, isLastItemCannotBeRemovedError: () => false,
}));
vi.mock("../../lib/pos-opening-api", () => ({ parseFrappeError: (_e: unknown, fallback: string) => fallback }));
vi.mock("../../lib/print", () => ({ printOrder: vi.fn() }));
vi.mock("../../lib/invoice-api", () => ({ resolvePrintFormat: vi.fn(() => "default") }));
vi.mock("../../lib/table-api", () => ({ getVacantTablesForBranch: vi.fn() }));
vi.mock("@ury/ui", () => ({
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
  Spinner: ({ message }: any) => <div>{message}</div>,
  ConfirmDialog: () => null,
  cn: (...args: any[]) => args.filter(Boolean).join(" "),
  showToast: { error: vi.fn(), success: vi.fn() },
}));
vi.mock("@ury/core", () => ({ formatCurrency: (amount: number) => `UGX ${amount}` }));
vi.mock("../components/CaptainMenu", () => ({ default: () => null }));
vi.mock("../components/CaptainOrderLine", () => ({ default: () => null }));
vi.mock("../components/CaptainActionsMenu", () => ({ default: () => null }));
vi.mock("../../components/ProductDialog", () => ({ default: () => null }));
vi.mock("../../components/CommentDialog", () => ({ default: () => null }));
vi.mock("../../components/TableTransferDialog", () => ({ default: () => null }));
vi.mock("../../components/CaptainTransferDialog", () => ({ default: () => null }));
vi.mock("../../components/CustomerSelect", () => ({ CustomerSelect: () => null }));

beforeEach(() => {
  vi.clearAllMocks();
  Object.assign(state, { guestLabel: "", isUpdatingOrder: false, orderId: null });
  vi.mocked(syncOrder).mockResolvedValue({ message: { name: "INV-1" } } as never);
});

const box = () => document.querySelector<HTMLInputElement>('input[name="guest_label"]');

async function send() {
  render(<CaptainOrder />);
  const button = screen.getAllByRole("button").find((b) => /Send Order|Update Order/.test(b.textContent || ""));
  expect(button).toBeDefined();
  fireEvent.click(button!);
  await waitFor(() => expect(syncOrder).toHaveBeenCalled());
  return vi.mocked(syncOrder).mock.calls[0][0] as unknown as Record<string, unknown>;
}

describe("Captain order pad guest label", () => {
  it("has a 40-character guest label box bound to the store", () => {
    render(<CaptainOrder />);
    expect(box()).not.toBeNull();
    expect(box()!.maxLength).toBe(40);
    fireEvent.change(box()!, { target: { value: "Susan" } });
    expect(state.setGuestLabel).toHaveBeenCalledWith("Susan");
  });

  it("sends the label with the order", async () => {
    state.guestLabel = "Susan";
    expect(await send()).toEqual(expect.objectContaining({ guest_label: "Susan" }));
  });

  it("clearing the label on an existing order sends it blank so the server clears it", async () => {
    Object.assign(state, { guestLabel: "", isUpdatingOrder: true, orderId: "INV-1" });
    expect(await send()).toEqual(expect.objectContaining({ invoice: "INV-1", guest_label: "" }));
  });

  it("a new order without a label sends none", async () => {
    expect(await send()).not.toHaveProperty("guest_label");
  });
});
