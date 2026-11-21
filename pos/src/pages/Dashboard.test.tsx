import userEvent from '@testing-library/user-event';
import { render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";

vi.mock("../i18n", () => ({
  t: (key) => key,
  tData: (s) => s,
  tUser: (s) => s ?? '',
  tNotification: (s) => s,
  ltrFraction: (a, b) => `${a} / ${b}`,
}));

vi.mock("@ury/ui", async (importOriginal) => ({
  ...await importOriginal<typeof import("@ury/ui")>(),
  Card: ({ children, padding }) => <div>{children}</div>,
  CardContent: ({ children }) => <div>{children}</div>,
  KpiStrip: ({ data }) => <div>{data?.length ? "KpiStrip loaded" : "KpiStrip empty"}</div>,
  AttentionFeed: () => <div>AttentionFeed</div>,
  cn: (...args) => args.filter(Boolean).join(" "),
}));

vi.mock('../store/root-store', () => ({
  useRootStore: (select: (state: { user: { name: string } }) => unknown) => select({ user: { name: 'cashier@example.test' } }),
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

  it.each(['Short notice', 'Long notification '.repeat(9).trim()])(
    'discloses the full subject and distinct safe notification body for %s', async (subject) => {
      const row: Record<string, string> = { name: 'N1', for_user: 'cashier@example.test', subject: `<b>${subject}</b>`,
        email_content: '<strong>Collect &amp; verify</strong><script>bad()</script>', creation: '2026-01-01' };
      const getList = vi.fn(async (url: string) => {
        const params = new URL(url, location.origin).searchParams;
        const fields: string[] = JSON.parse(params.get('fields') || '[]');
        const message = params.get('doctype') === 'Notification Log'
          ? [Object.fromEntries(fields.map(key => [key, row[key]]))] : [];
        return { ok: true, json: async () => ({ message }) };
      });
      vi.stubGlobal('fetch', getList);
      try {
        render(<Dashboard />);
        const body = await screen.findByText('Collect & verify');
        const details = body.closest('details')!;
        const summary = details.querySelector('summary')!;
        expect(summary.textContent).toBe(subject.length > 96 ? subject.slice(0, 96).trimEnd() + '…' : subject);
        expect(details.querySelector('p')?.textContent).toBe(subject);
        expect(details.querySelector('b, strong, script')).toBeNull();
        expect(details.textContent).not.toContain('bad()');
        expect(details).not.toHaveAttribute('open');
        summary.focus();
        expect(summary).toHaveFocus();
        await userEvent.click(summary);
        expect(details).toHaveAttribute('open');
        const requests = getList.mock.calls.map(([url]) => new URL(url, location.origin).searchParams)
          .filter(params => params.get('doctype') === 'Notification Log');
        expect(requests).toHaveLength(1);
        expect(JSON.parse(requests[0].get('filters') || '{}')).toEqual({ for_user: 'cashier@example.test' });
      } finally { vi.unstubAllGlobals(); }
    },
  );

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
