import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Header from "./Header";
import { BrowserRouter } from "react-router-dom";

const { mockIsPrintStatusDisabled } = vi.hoisted(() => ({
  mockIsPrintStatusDisabled: vi.fn(),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return {
    ...actual,
    useLocation: () => ({ pathname: "/register" }),
    useNavigate: () => vi.fn(),
  };
});

vi.mock("@ury/core", async (importOriginal) => ({
  ...await importOriginal<typeof import("@ury/core")>(),
  logout: vi.fn().mockResolvedValue(undefined),
  withReturnContext: (path: string) => path,
  formatCurrency: (amount: number) => `Rs. ${amount}`,
  isPrintStatusDisabled: mockIsPrintStatusDisabled,
  DESK_RETURN_STORAGE_KEY: "ury:desk_return_to",
}));

vi.mock("./PrinterWatch", () => ({
  PrinterStatusButton: () => (
    <button type="button" data-testid="printer-status-button">
      Printer Status
    </button>
  ),
}));

const mockUser = vi.hoisted(() => ({ roles: [] as string[] }));

vi.mock("../store/root-store", () => ({
  useRootStore: (selector: any) => {
    const state = {
      user: {
        name: "test_user",
        full_name: "Test User",
        roles: mockUser.roles,
      },
      orderSearchQuery: "",
      setOrderSearchQuery: vi.fn(),
    };
    return typeof selector === "function" ? selector(state) : state;
  },
}));

const mockPosProfile = vi.hoisted(() => ({ value: null as Record<string, unknown> | null }));

vi.mock("../store/pos-store", () => ({
  usePOSStore: (selector?: (state: any) => unknown) => {
    const state = {
      searchQuery: "",
      setSearchQuery: vi.fn(),
      setShowVoluntaryClosing: vi.fn(),
      posProfile: mockPosProfile.value,
      availableOutlets: [],
      requiresOutletSelection: false,
      activeOrders: [],
      switchOutlet: vi.fn(),
    };
    return typeof selector === "function" ? selector(state) : state;
  },
}));

vi.mock("@ury/ui", async () => {
  const actual = await vi.importActual("@ury/ui");
  return {
    ...actual,
    showToast: {
      error: vi.fn(),
    },
  };
});

vi.mock("../i18n", () => ({
  t: (key: string) => key,
  tUser: (name: string | null | undefined) => name ?? "",
}));

describe("Header", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    vi.clearAllMocks();
    mockPosProfile.value = null;
    mockIsPrintStatusDisabled.mockReturnValue(false);
  });

  it("renders the header", () => {
    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );

    expect(screen.getByAltText("Maximus POS")).toBeInTheDocument();
  });

  it("renders the search bar with appropriate placeholder", () => {
    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );

    const searchInput = screen.getByPlaceholderText("header.search_placeholder_menu");
    expect(searchInput).toBeInTheDocument();
  });

  it("opens user menu when user button is clicked", async () => {
    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );

    const userButton = screen.getByRole("button", { name: /Test User/i });
    await userEvent.click(userButton);

    expect(screen.getByText("header.close_shift")).toBeInTheDocument();
  });

  it("displays user menu options when menu is open", async () => {
    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );

    const userButton = screen.getByRole("button", { name: /Test User/i });
    await userEvent.click(userButton);

    expect(screen.getByText("header.logout")).toBeInTheDocument();
    expect(screen.getByText("header.clear_cache")).toBeInTheDocument();
    expect(screen.queryByText("header.switch_to_desk")).toBeNull();
  });


  it.each([
    [["URY Cashier"]],
    [["URY Cashier", "URY Manager"]],
    [["URY Cashier", "URY Admin"]],
    [["URY Cashier", "System Manager"]],
  ])("omits the Desk switch for roles %j", async (roles) => {
    mockUser.roles = roles;
    const originalLocation = window.location;
    delete (window as any).location;
    window.location = { ...originalLocation, href: "" } as any;

    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );
    await userEvent.click(screen.getByRole("button", { name: /Test User/i }));
    expect(screen.queryByText("header.switch_to_desk")).toBeNull();

    window.location = originalLocation as any;
    mockUser.roles = [];
  });

  it("keeps the logo outside Desk navigation", () => {
    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );

    const logoLink = screen.getByAltText("Maximus POS").closest("a");
    expect(logoLink).toBeNull();
  });


  it("renders the printer status button next to the user menu when enabled", () => {
    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );

    const printerButton = screen.getByTestId("printer-status-button");
    expect(printerButton).toBeInTheDocument();

    const userButton = screen.getByRole("button", { name: /Test User/i });
    // The printer button must sit immediately before the user menu in the
    // right-side actions group.
    expect(
      printerButton.compareDocumentPosition(userButton) &
        Node.DOCUMENT_POSITION_FOLLOWING
    ).toBeTruthy();
  });

  it("hides the printer status button when print status is disabled", () => {
    mockIsPrintStatusDisabled.mockReturnValue(true);

    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );

    expect(screen.queryByTestId("printer-status-button")).not.toBeInTheDocument();
  });

  it("renders only one user menu and one printer button", () => {
    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );

    expect(screen.getAllByRole("button", { name: /Test User/i })).toHaveLength(1);
    expect(screen.getAllByTestId("printer-status-button")).toHaveLength(1);
  });

  it("renders G44 business date badge and Shift X/Z Report action when installed, hides when absent", async () => {
    // Absent: neither badge nor Shift Report menu item appears
    mockPosProfile.value = { name: "Main Grill POS", currency: "UGX" };
    const { rerender } = render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );
    expect(screen.queryByTestId("business-date-badge")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Test User/i }));
    expect(screen.queryByText("header.shift_report")).toBeNull();

    // Present (siteops#8609 installed): badge displays business date & menu shows Shift Report
    mockPosProfile.value = {
      name: "Main Grill POS",
      currency: "UGX",
      active_business_date: "2026-11-24",
      active_opening_entry: "POS-OPE-001",
      shift_report_available: true,
    };
    rerender(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );
    const badge = screen.getByTestId("business-date-badge");
    expect(badge).toBeInTheDocument();
    expect(badge.textContent).toContain("2026-11-24");
    expect(screen.getByText("header.shift_report")).toBeInTheDocument();
  });
});


it("renders the site logo with its configured accessible name", () => {
  vi.stubGlobal("frappe", { boot: { ury_brand: { name: "Example site", logo: "/files/site.png" } } });
  try {
    render(<BrowserRouter><Header /></BrowserRouter>);
    expect(screen.getByRole("img")).toHaveAttribute("src", "/files/site.png");
    expect(screen.getByRole("img")).toHaveAccessibleName("Example site");
  } finally { vi.unstubAllGlobals(); }
});
