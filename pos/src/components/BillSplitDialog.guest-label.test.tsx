import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import BillSplitDialog from "./BillSplitDialog";

// Ported from the fork (split-bill-guest-label.test.tsx, M03): two people at
// one table stay identifiable on separately named split bills without a
// Customer master for each.

vi.mock("@ury/ui", () => ({
  Dialog: ({ open, children }: any) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: any) => <div>{children}</div>,
  DialogDescription: ({ children }: any) => <div>{children}</div>,
  DialogFooter: ({ children }: any) => <div>{children}</div>,
  DialogHeader: ({ children }: any) => <div>{children}</div>,
  DialogTitle: ({ children }: any) => <div>{children}</div>,
  Button: ({ children, onClick, disabled }: any) => <button onClick={onClick} disabled={disabled}>{children}</button>,
  cn: (...args: any[]) => args.filter(Boolean).join(" "),
}));
vi.mock("@ury/core", () => ({ formatCurrency: (amount: number) => `UGX ${amount}` }));
vi.mock("./CustomerPicker", () => ({ CustomerPicker: () => null }));

const items = [
  { name: "line-1", item_name: "Fish", qty: 1, rate: 100, amount: 100 },
  { name: "line-2", item_name: "Tea", qty: 1, rate: 20, amount: 20 },
];

let onConfirm: ReturnType<typeof vi.fn>;

beforeEach(() => {
  onConfirm = vi.fn(async () => {});
  render(
    <BillSplitDialog open onOpenChange={vi.fn()} invoiceName="BILL-1" items={items}
      sourceCustomer={{ id: "Walk-in", name: "Walk-in", phone: "" }} onConfirm={onConfirm} />,
  );
  fireEvent.click(screen.getByText("Fish"));
});

const confirm = () => screen.getAllByRole("button").filter((b) => b.textContent === "Split bill").pop()!;

it("sends a short guest label for the new bill, keeping the same customer", () => {
  const label = document.querySelector<HTMLInputElement>('input[name="guest_label"]');
  expect(label).not.toBeNull();
  expect(label!.maxLength).toBe(40);
  fireEvent.change(label!, { target: { value: "  David " } });
  fireEvent.click(confirm());
  expect(onConfirm).toHaveBeenCalledExactlyOnceWith({
    itemsToMove: [{ name: "line-1", qty: 1 }], customer: undefined, guestLabel: "David",
  });
});

it("a blank label sends none", () => {
  fireEvent.click(confirm());
  expect(onConfirm).toHaveBeenCalledExactlyOnceWith({
    itemsToMove: [{ name: "line-1", qty: 1 }], customer: undefined, guestLabel: undefined,
  });
});
