import { describe, expect, it } from "vitest";
import { canRecallKot } from "./kitchen-actions.js";

describe("ticket-specific recall policy", () => {
  it("uses the ticket's configured window when an expo board spans profiles", () => {
    const now = new Date("2026-11-25T10:20:00");
    const ticket = { order_status: "Served", creation: "2026-11-25 10:00:00", production_time: 0 };
    expect(canRecallKot({ ...ticket, recall_window_minutes: 25 }, now, 15)).toBe(true);
    expect(canRecallKot({ ...ticket, recall_window_minutes: 5 }, now, 25)).toBe(false);
  });
});
