import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { call } from "@ury/core";
import PaymentDialog from "./PaymentDialog";

// Ported from the fork (#65/#68 PaymentDialog room charge, M02 room-charge-lookup):
// a Room Charge tender asks for the room or folio and sends it as room_number.
// With a room lookup method on the POS Profile the cashier checks the room
// first; Pay waits for a verified match of the room as typed now.

const METHOD = "pms_bridge.room_charge_lookup";
const store = vi.hoisted(() => ({
  state: {} as Record<string, unknown>,
}));

vi.mock("../store/pos-store", () => ({
  usePOSStore: () => store.state,
}));

vi.mock("../lib/print", () => ({
  printOrder: vi.fn().mockResolvedValue("qz"),
}));

vi.mock("@ury/core", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@ury/core")>();
  return {
    ...actual,
    formatCurrency: (amount: number) => `UGX ${Number(amount).toFixed(2)}`,
    call: { get: vi.fn(), post: vi.fn().mockResolvedValue({ message: { name: "BILL-1" } }) },
  };
});

vi.mock("@ury/ui", () => ({
  Button: ({ children, ...props }: any) => <button {...props}>{children}</button>,
  Input: ({ size: _size, ...props }: any) => <input {...props} />,
  Dialog: ({ children, open }: any) => (open ? <div>{children}</div> : null),
  DialogContent: ({ children }: any) => <div>{children}</div>,
  showToast: { success: vi.fn(), error: vi.fn() },
}));

function setProfile(extra: Record<string, unknown> = {}) {
  store.state = {
    paymentModes: ["Room Charge"],
    fetchPaymentModes: vi.fn(),
    posProfile: { name: "Till A", enable_discount: 0, enable_payment_without_printing: 0, room_charge_modes: ["Room Charge"], ...extra },
  };
}

function renderDialog() {
  return render(
    <PaymentDialog
      order={{ invoice_printed: 1 }}
      onClose={vi.fn()}
      grandTotal={100}
      roundedTotal={100}
      invoice="BILL-1"
      customer="Walk-in Customer"
      posProfile="Till A"
      table={null}
      cashier="cashier@example.com"
      owner="waiter@example.com"
      fetchOrders={vi.fn(async () => {})}
      clearSelectedOrder={vi.fn()}
    />,
  );
}

const amount = () => document.querySelector<HTMLInputElement>('input[type="number"]')!;
const roomBox = () => document.querySelector<HTMLInputElement>("#room-charge-reference");

function enterRoom(value: string) {
  const box = roomBox();
  expect(box, "no room/folio box for the Room Charge tender").not.toBeNull();
  fireEvent.change(box!, { target: { value } });
}
const payButton = () => screen.getByRole("button", { name: /^Pay / }) as HTMLButtonElement;
const checkRoomButton = () => screen.queryByRole("button", { name: "Check room" });

async function checkRoom() {
  fireEvent.click(checkRoomButton()!);
  await waitFor(() => expect(vi.mocked(call.get)).toHaveBeenCalled());
  await waitFor(() => expect(screen.queryByText("Checking room...")).toBeNull());
}

beforeEach(() => {
  vi.mocked(call.get).mockReset();
  vi.mocked(call.post).mockClear();
  setProfile();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Room Charge without a room lookup method", () => {
  it("asks for a room or folio only once Room Charge has an amount", () => {
    renderDialog();
    expect(roomBox()).toBeNull();
    fireEvent.change(amount(), { target: { value: "100" } });
    expect(screen.getByText("Room / Folio")).toBeInTheDocument();
    expect(roomBox()).not.toBeNull();
    expect(checkRoomButton()).toBeNull();
  });

  it("sends the cashier's room/folio text as room_number with settlement", async () => {
    renderDialog();
    fireEvent.change(amount(), { target: { value: "100" } });
    enterRoom("  ROOM-101  ");
    expect(payButton().disabled).toBe(false);
    fireEvent.click(payButton());
    await waitFor(() => expect(call.post).toHaveBeenCalledWith(
      "ury.ury.doctype.ury_order.ury_order.make_invoice",
      expect.objectContaining({
        invoice: "BILL-1", room_number: "  ROOM-101  ", submit_invoice: 0,
        payments: [{ mode_of_payment: "Room Charge", amount: 100 }],
      }),
    ));
    await waitFor(() => expect(call.post).toHaveBeenCalledWith(
      "ury.ury.doctype.ury_order.ury_order.submit_order_invoice",
      { invoice: "BILL-1" },
    ));
    expect(call.get).not.toHaveBeenCalledWith(METHOD, expect.anything());
  });

  it("a settlement without Room Charge sends no room_number", async () => {
    store.state = { ...store.state, paymentModes: ["Cash"] };
    renderDialog();
    fireEvent.change(amount(), { target: { value: "100" } });
    fireEvent.click(payButton());
    await waitFor(() => expect(call.post).toHaveBeenCalled());
    expect(vi.mocked(call.post).mock.calls[0][1]).not.toHaveProperty("room_number");
  });
});

describe("Room Charge with a room lookup method on the POS Profile", () => {
  beforeEach(() => setProfile({ room_lookup_method: METHOD }));

  it("labels the section, checks the room and enables Pay on a match", async () => {
    vi.mocked(call.get).mockResolvedValue({ message: {
      room_number: "108", guest_name: "Susan K.", departure: "2026-11-16", no_post: 0,
    } });
    renderDialog();
    fireEvent.change(amount(), { target: { value: "100" } });
    expect(screen.getByText("Room posting")).toBeInTheDocument();
    enterRoom("108");
    expect(payButton().disabled).toBe(true);
    expect(screen.getByText("Check the room before taking Room Charge")).toBeInTheDocument();

    await checkRoom();
    expect(call.get).toHaveBeenCalledExactlyOnceWith(METHOD, { pos_profile: "Till A", room: "108" });
    expect(screen.getByText("Room 108 · Susan K. · departs 2026-11-16")).toBeInTheDocument();
    expect(payButton().disabled).toBe(false);

    fireEvent.click(payButton());
    await waitFor(() => expect(call.post).toHaveBeenCalledWith(
      "ury.ury.doctype.ury_order.ury_order.make_invoice",
      expect.objectContaining({ room_number: "108" }),
    ));
  });

  it("editing the room after a match disables Pay until it is checked again", async () => {
    vi.mocked(call.get).mockResolvedValue({ message: {
      room_number: "108", guest_name: "Susan K.", departure: "2026-11-16", no_post: 0,
    } });
    renderDialog();
    fireEvent.change(amount(), { target: { value: "100" } });
    enterRoom("108");
    await checkRoom();
    expect(payButton().disabled).toBe(false);
    enterRoom("109");
    expect(payButton().disabled).toBe(true);
    expect(screen.queryByText(/Susan K\./)).toBeNull();
  });

  it("a No Post guest is shown as refused and Pay stays disabled", async () => {
    vi.mocked(call.get).mockResolvedValue({ message: {
      room_number: "108", guest_name: "Susan K.", departure: "2026-11-16", no_post: 1,
    } });
    renderDialog();
    fireEvent.change(amount(), { target: { value: "100" } });
    enterRoom("108");
    await checkRoom();
    expect(screen.getByText("This guest's bill is closed to room charges")).toBeInTheDocument();
    expect(payButton().disabled).toBe(true);
  });

  it("a refused lookup shows the server message and keeps Pay disabled", async () => {
    vi.mocked(call.get).mockRejectedValue({ message: "KMR-RC-004: no guest is checked in to room 999" });
    renderDialog();
    fireEvent.change(amount(), { target: { value: "100" } });
    enterRoom("999");
    await checkRoom();
    expect(screen.getByText("KMR-RC-004: no guest is checked in to room 999")).toBeInTheDocument();
    expect(payButton().disabled).toBe(true);
    expect(call.post).not.toHaveBeenCalled();
  });

  it("an empty lookup answer is a failed check, not a match", async () => {
    vi.mocked(call.get).mockResolvedValue({ message: null });
    renderDialog();
    fireEvent.change(amount(), { target: { value: "100" } });
    enterRoom("108");
    await checkRoom();
    expect(screen.getByText("Could not check this room")).toBeInTheDocument();
    expect(payButton().disabled).toBe(true);
  });

  it("a lookup configured for another till is not used", () => {
    setProfile({ name: "Till B", room_lookup_method: METHOD });
    renderDialog();
    fireEvent.change(amount(), { target: { value: "100" } });
    expect(screen.getByText("Room / Folio")).toBeInTheDocument();
    expect(checkRoomButton()).toBeNull();
  });
});
