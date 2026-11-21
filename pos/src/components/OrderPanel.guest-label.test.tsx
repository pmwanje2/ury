import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import OrderPanel from "./OrderPanel";
import { syncOrder } from "../lib/order-api";

// Ported from the fork (order-guest-label.test.tsx, M03): a short guest label
// (Susan, Seat 2) on the cart, sent with sync_order so it reaches the kitchen
// ticket, with no Customer master. An existing order always sends its label,
// so clearing the box clears it on the server.

const state = vi.hoisted(() => ({
  activeOrders: [{ id: "SOUP", item: "SOUP", uniqueId: "ROW-1", name: "Soup", price: 18000, quantity: 1 }],
  posProfile: { name: "Till A", cashier: "Cashier", owner: "Owner" },
  selectedOrderType: "Take Away", selectedTable: null, selectedRoom: "Dining",
  selectedCustomer: { id: "Walk-in", name: "Walk-in" }, selectedAggregator: null,
  isUpdatingOrder: false, orderId: null as string | null, lastModifiedTime: null,
  orderComment: "", guestLabel: "", noOfPax: 1, paymentModes: ["Cash"], orderLoading: false,
  resetOrderState: vi.fn(), clearOrder: vi.fn(), setNoOfPax: vi.fn(), removeFromOrder: vi.fn(),
  updateQuantity: vi.fn(), setSelectedItem: vi.fn(), setOrderComment: vi.fn(), setGuestLabel: vi.fn(),
}));

vi.mock("../store/pos-store", () => ({
  usePOSStore: () => ({ ...state, isOrderInteractionDisabled: () => false }),
}));
vi.mock("../store/root-store", () => ({
  useRootStore: (select: (value: { user: { name: string } }) => unknown) => select({ user: { name: "waiter@example.com" } }),
}));
vi.mock("../lib/order-api", () => ({ syncOrder: vi.fn() }));
vi.mock("../lib/kot-status-api", () => ({ getOrderKotStatus: vi.fn(async () => ({ items: {} })) }));
vi.mock("../lib/cart-capacity", () => ({
  useCartAvailability: () => ({ availabilityByItem: {} }),
  cartQtyForItem: () => 0,
  remainingHeadroom: () => undefined,
}));
vi.mock("@ury/ui", () => ({
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
  Badge: ({ children }: any) => <span>{children}</span>,
  Spinner: ({ message }: any) => <div>{message}</div>,
  cn: (...args: any[]) => args.filter(Boolean).join(" "),
  showToast: { success: vi.fn(), error: vi.fn() },
}));
vi.mock("@ury/core", () => ({
  formatCurrency: (amount: number) => `UGX ${amount}`,
  parseFrappeError: (_error: unknown, fallback: string) => fallback,
}));
vi.mock("./CustomerSelect", () => ({ CustomerSelect: () => null }));
vi.mock("./ProductDialog", () => ({ default: () => null }));
vi.mock("./OrderTypeSelect", () => ({ default: () => null }));
vi.mock("./CommentDialog", () => ({ default: () => null }));

beforeEach(() => {
  vi.clearAllMocks();
  Object.assign(state, { guestLabel: "", isUpdatingOrder: false, orderId: null });
  vi.mocked(syncOrder).mockResolvedValue({ message: { name: "INV-1" } } as never);
});

const box = () => document.querySelector<HTMLInputElement>('input[name="guest_label"]');

async function submit() {
  render(<OrderPanel />);
  const button = screen.getAllByRole("button").find((b) => /Add New Order|Update Order/.test(b.textContent || ""))!;
  fireEvent.click(button);
  await waitFor(() => expect(syncOrder).toHaveBeenCalled());
  return vi.mocked(syncOrder).mock.calls[0][0] as unknown as Record<string, unknown>;
}

it("the cart has a short guest label box bound to the store", () => {
  render(<OrderPanel />);
  expect(box()).not.toBeNull();
  expect(box()!.maxLength).toBe(40);
  fireEvent.change(box()!, { target: { value: "David" } });
  expect(state.setGuestLabel).toHaveBeenCalledWith("David");
});

it("sends the trimmed guest label with a new order, keeping the customer", async () => {
  state.guestLabel = "  Susan ";
  const payload = await submit();
  expect(payload).toEqual(expect.objectContaining({ guest_label: "Susan", customer: "Walk-in" }));
});

it("a new order without a label sends none", async () => {
  const payload = await submit();
  expect(payload).not.toHaveProperty("guest_label");
});

it("clearing the label on an existing order sends it blank so the server clears it", async () => {
  Object.assign(state, { guestLabel: "", isUpdatingOrder: true, orderId: "INV-1" });
  const payload = await submit();
  expect(payload).toEqual(expect.objectContaining({ invoice: "INV-1", guest_label: "" }));
});
