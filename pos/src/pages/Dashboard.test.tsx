import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

vi.mock("../i18n", () => ({
  t: (key) => key,
  tData: (s) => s,
  tUser: (s) => s ?? '',
  tNotification: (s) => s,
  ltrFraction: (a, b) => `${a} / ${b}`,
}));

vi.mock("@ury/ui", () => ({
  Card: ({ children, padding }) => <div>{children}</div>,
  CardContent: ({ children }) => <div>{children}</div>,
  KpiStrip: ({ data }) => <div>{data?.length ? "KpiStrip loaded" : "KpiStrip empty"}</div>,
  AttentionFeed: () => <div>AttentionFeed</div>,
  cn: (...args) => args.filter(Boolean).join(" "),
}));

vi.mock("../store/pos-store", () => ({
  usePOSStore: () => ({
    posProfile: { branch: "Kozhikode" },
  }),
}));

vi.mock("../lib/pos-closing-api", () => ({
  getOpenPosOpeningEntries: vi.fn().mockResolvedValue([]),
}));

vi.mock("../components/dashboard/InsightFeed", () => ({
  default: () => <div>InsightFeed</div>,
}));

vi.mock("../components/chat/AskBar", () => ({
  default: () => <div>AskBar</div>,
}));

vi.mock("@ury/core", () => ({
  formatCurrency: (amount) => `Rs. ${amount}`,
  call: {
    get: vi.fn().mockResolvedValue({ message: {} }),
  },
}));

import Dashboard from "./Dashboard";

describe("Dashboard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders dashboard title components", async () => {
    render(<Dashboard />);

    await waitFor(() => {
      // The dashboard renders "Shift Overview" as the title
      expect(screen.getByText("Shift Overview")).toBeInTheDocument();
    });
  });

  it("discloses the full native notification behind a compact headline", async () => {
    const subject = 'Long notification '.repeat(9).trim();
    vi.stubGlobal('fetch', vi.fn(async url => ({ ok: true, json: async () => ({
      message: String(url).includes('Notification+Log') ? [{ name: 'N1', subject, creation: '2026-01-01' }] : [],
    }) })));
    try {
      render(<Dashboard />);
      const full = await screen.findByText(subject);
      const details = full.closest('details');
      expect(details).not.toBeNull();
      expect(details).not.toHaveAttribute('open');
      expect(details!.querySelector('summary')!.textContent).toContain(subject.slice(0, 96).trimEnd() + '…');
    } finally { vi.unstubAllGlobals(); }
  });

  it("renders main panels", async () => {
    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText("InsightFeed")).toBeInTheDocument();
      expect(screen.getByText("AskBar")).toBeInTheDocument();
    });
  });

  it("loads with posProfile from store", async () => {
    render(<Dashboard />);

    await waitFor(() => {
      expect(screen.getByText("InsightFeed")).toBeInTheDocument();
    });
  });

  it("renders dashboard structure", () => {
    const { container } = render(<Dashboard />);
    expect(container.querySelector("div")).toBeTruthy();
  });
});
