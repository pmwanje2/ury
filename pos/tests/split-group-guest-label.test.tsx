import { expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import SplitGroupPanel from "../src/components/SplitGroupPanel";
import { getSplitGroup, type SplitGroupInvoice } from "../src/lib/invoice-api";

// Ported from the fork (guest-label-carry-through.test.tsx, M03): split checks
// on one walk-in customer are told apart by their guest label.

vi.mock("../src/lib/invoice-api", () => ({ getSplitGroup: vi.fn() }));
vi.mock("@ury/ui", () => ({
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
  Spinner: ({ message }: any) => <div>{message}</div>,
  cn: (...args: any[]) => args.filter(Boolean).join(" "),
}));
vi.mock("@ury/core", () => ({ formatCurrency: (n: number) => String(n) }));

const check = (name: string, label: string | null, index: number) => ({
  name, invoice_printed: 1, rounded_total: 100, grand_total: 100, customer: "Walk In",
  customer_name: "Walk In", status: "Draft", custom_split_group: "SG-1",
  custom_split_from: index === 1 ? null : "INV-A", split_index: index, split_total: 2,
  is_original: index === 1, docstatus: 0, custom_guest_label: label,
}) as unknown as SplitGroupInvoice;

async function rows(invoices: SplitGroupInvoice[]) {
  vi.mocked(getSplitGroup).mockResolvedValue({ invoices, current: "INV-A", group: "SG-1" });
  render(<SplitGroupPanel invoiceName="INV-A" onOpenInvoice={vi.fn()} />);
  await waitFor(() => expect(screen.getAllByRole("button").length).toBeGreaterThanOrEqual(2));
  return screen.getAllByRole("button").filter((el) => el.getAttribute("tabindex") !== null).map((row) => row.textContent || "");
}

it("names each split check by its label, ahead of the shared walk-in customer", async () => {
  const [first, second] = await rows([check("INV-A", "Susan", 1), check("INV-B", "David", 2)]);
  expect(first).toContain("Susan · Walk In");
  expect(second).toContain("David · Walk In");
});

it("without labels the rows still show the customer", async () => {
  const found = await rows([check("INV-A", null, 1), check("INV-B", "", 2)]);
  expect(found.every((row) => row.includes("Walk In") && !row.includes(" · Walk In"))).toBe(true);
});

it("does not repeat a customer the guest label already names", async () => {
  const named = {
    ...check("INV-A", "Dr. Elena Rostova (Rm 408)", 1),
    customer: "Dr. Elena Rostova", customer_name: "Dr. Elena Rostova",
  } as SplitGroupInvoice;
  const [first, second] = await rows([named, check("INV-B", "David", 2)]);
  expect(first).toContain("Dr. Elena Rostova (Rm 408)");
  expect(first).not.toContain("(Rm 408) · Dr. Elena Rostova");
  expect(second).toContain("David · Walk In");
});

it("tells apart two splits whose labels both sit inside the customer name", async () => {
  const family = (name: string, label: string, index: number) => ({
    ...check(name, label, index), customer: "CUST-0107", customer_name: "Smith Family",
  }) as SplitGroupInvoice;
  const [first, second] = await rows([family("INV-A", "Smith", 1), family("INV-B", "Family", 2)]);
  expect(first).toContain("Smith · Smith Family");
  expect(second).toContain("Family · Smith Family");
});

it("names a customer kept under a naming-series id by its name, never the id", async () => {
  const series = { ...check("INV-A", "CUST-0001", 1), customer: "CUST-0001", customer_name: "Dr. Elena Rostova" } as SplitGroupInvoice;
  const [first] = await rows([series, check("INV-B", "David", 2)]);
  expect(first).toContain("Dr. Elena Rostova");
  expect(first).not.toContain("CUST-0001");
});
