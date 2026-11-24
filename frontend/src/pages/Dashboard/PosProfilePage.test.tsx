import React from "react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { call } from "@ury/core";
import { PosProfilePage } from "./PosProfilePage";

vi.mock("../../context/BranchContext", () => ({
  useBranchContext: () => ({ activeBranchId: "all", branches: [] }),
}));

vi.mock("@ury/core", () => ({
  call: vi.fn(),
}));

vi.mock("@ury/ui", () => ({
  Card: ({ children }: any) => <div>{children}</div>,
  Button: ({ children, onClick, ...props }: any) => (
    <button type="button" onClick={onClick} {...props}>
      {children}
    </button>
  ),
  Badge: ({ children }: any) => <span>{children}</span>,
  Input: (props: any) => <input {...props} />,
  Spinner: () => <span>Loading</span>,
  showToast: { success: vi.fn(), error: vi.fn(), warning: vi.fn() },
  DataTable: ({ rows, columns }: any) => (
    <div>
      {(rows || []).map((row: any) => (
        <div key={row.name}>
          {columns.map((c: any, i: number) => (
            <span key={i}>{c.render ? c.render(row) : row[c.key]}</span>
          ))}
        </div>
      ))}
    </div>
  ),
}));

vi.mock("../../components/ui/switch", () => ({
  Switch: ({ id, checked, onCheckedChange, disabled }: any) => (
    <input
      id={id}
      type="checkbox"
      checked={Boolean(checked)}
      disabled={disabled}
      onChange={(e) => onCheckedChange?.(e.target.checked)}
    />
  ),
}));

vi.mock("../../components/layout/SideDrawer", () => ({
  default: ({ children, open }: any) => (open ? <div>{children}</div> : null),
}));

vi.mock("../../components/common/SearchableSelect", () => ({
  SearchableSelect: ({ value, onChange, id }: any) => (
    <input id={id} value={value} onChange={(e) => onChange?.(id, e.target.value)} />
  ),
}));

vi.mock("../../i18n", () => ({
  __: (text: string) => text,
}));

describe("PosProfilePage P1-7b: uses native allow_discount_change instead of legacy custom_enable_discount", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(call).mockImplementation(async (method: string, params?: any) => {
      if (method === "frappe.client.get_list" && params?.doctype === "POS Profile") {
        return {
          message: [
            {
              name: "Lakeside Till",
              branch: "Lakeside",
              company: "Lakeside Hotel",
              warehouse: "Stores - LH",
              allow_discount_change: 0,
              disabled: 0,
            },
          ],
        };
      }
      if (method === "frappe.client.get_list") {
        return { message: [] };
      }
      if (method === "frappe.client.get" && params?.doctype === "POS Profile") {
        return {
          message: {
            name: "Lakeside Till",
            branch: "Lakeside",
            company: "Lakeside Hotel",
            warehouse: "Stores - LH",
            allow_discount_change: 0,
            payments: [{ mode_of_payment: "Cash", default: 1 }],
            applicable_for_users: [],
          },
        };
      }
      if (method === "frappe.client.get" && params?.doctype === "Mode of Payment") {
        return { message: { accounts: [{ company: "Lakeside Hotel", default_account: "Cash - LH" }] } };
      }
      if (method === "frappe.client.get" && params?.doctype === "Company") {
        return { message: { abbr: "LH", default_cost_center: "Main - LH", write_off_account: "Write Off - LH" } };
      }
      return { message: {} };
    });
  });

  it("loads, toggles, and saves native allow_discount_change on POS Profile", async () => {
    render(<PosProfilePage />);

    await waitFor(() => expect(screen.getByText("Lakeside Till")).toBeInTheDocument());

    // Open Edit mode for Lakeside Till
    const editBtn = screen.getByTitle("Edit Profile");
    fireEvent.click(editBtn);

    // Native allow_discount_change toggle must exist (NOT legacy custom_enable_discount)
    await waitFor(() => expect(screen.getByText(/POS Profile:/i)).toBeInTheDocument());
    const toggle = document.getElementById("allow_discount_change") as HTMLInputElement | null;
    expect(toggle).not.toBeNull();
    expect(document.getElementById("custom_enable_discount")).toBeNull();

    fireEvent.click(toggle!);
    expect(toggle!.checked).toBe(true);

    const saveBtn = screen.getByRole("button", { name: /^Save$/i });
    fireEvent.click(saveBtn);

    await waitFor(() =>
      expect(call).toHaveBeenCalledWith(
        "frappe.client.set_value",
        expect.objectContaining({
          doctype: "POS Profile",
          name: "Lakeside Till",
          fieldname: expect.objectContaining({ allow_discount_change: 1 }),
        }),
      ),
    );
  });
});
