import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { BrowserRouter } from "react-router-dom";
import { Header } from "./Header";

vi.mock("../../context/BranchContext", () => ({
  useBranchContext: () => ({
    activeBranchId: "branch-1",
    setActiveBranchId: vi.fn(),
    branches: [{ id: "branch-1", name: "Main Branch" }],
    activeBranch: { id: "branch-1", name: "Main Branch" },
  }),
}));

vi.mock("../../context/CompanyContext", () => ({
  useCompanyContext: () => ({
    activeCompanyId: "all",
    setActiveCompanyId: vi.fn(),
    companies: [],
    activeCompany: null,
  }),
}));

vi.mock("../DeskLink", () => ({
  useDeskPermission: () => ({ read: true }),
}));

vi.mock("./Breadcrumbs", () => ({
  Breadcrumbs: () => <div data-testid="breadcrumbs">Breadcrumbs</div>,
}));

vi.mock("../chat/AskBar", () => ({
  default: () => <div data-testid="ask-bar">AskBar</div>,
}));

vi.mock("@ury/core", async (original) => ({
  ...await original<typeof import("@ury/core")>(),
  logout: vi.fn().mockResolvedValue(undefined),
  call: Object.assign(vi.fn().mockResolvedValue({ message: [] }), {
    get: vi.fn().mockResolvedValue({ message: [] }),
    post: vi.fn().mockResolvedValue({ message: [] }),
  }),
  getLoggedUser: () => "admin@urypos.com",
  getUserRoles: () => ["System Manager"],
  withReturnContext: (path: string) => path,
  appLabelForPath: () => "URY",
  DESK_RETURN_STORAGE_KEY: "ury:desk_return_to",
}));

vi.mock("@ury/ui", async () => {
  const actual = await vi.importActual("@ury/ui");
  return {
    ...actual,
    messageToPlainText: (msg: string) => msg,
  };
});

vi.mock("../../i18n", () => ({
  __: (str: string) => str,
  uiLocale: () => "en",
}));

describe("Header (/ury)", () => {
  beforeEach(() => {
    cleanup();
    localStorage.clear();
    sessionStorage.clear();
    vi.clearAllMocks();
  });

  it("renders the Maximus logo without a Desk link", () => {
    render(
      <BrowserRouter>
        <Header />
      </BrowserRouter>
    );

    const logo = screen.getByAltText("Maximus Logo");
    expect(logo).toBeInTheDocument();

    const logoLink = logo.closest("a");
    expect(logoLink).toBeNull();
  });

});

it("clears the POS worker caches as well as web storage before reloading", async () => {
  localStorage.setItem('deskPreference', 'keep');
  sessionStorage.setItem('deskSession', 'keep');
  const remove = vi.fn().mockResolvedValue(true);
  vi.stubGlobal('caches', {keys: async () => ['ury-pos-shell', 'other'], delete: remove});
  try {
    render(<BrowserRouter><Header /></BrowserRouter>);
    await userEvent.click(screen.getByRole('button', {name: 'Admin User'}));
    await userEvent.click(screen.getByText('Clear Cache'));
    expect(localStorage.getItem('deskPreference')).toBe('keep');
    expect(sessionStorage.getItem('deskSession')).toBe('keep');
    expect(remove.mock.calls).toEqual([['ury-pos-shell']]);
  } finally { vi.unstubAllGlobals(); }
});

it('keeps the management session visible and allows retry after a refused logout', async () => {
  cleanup();
  const { logout } = await import('@ury/core');
  vi.mocked(logout).mockResolvedValueOnce(false);
  render(<BrowserRouter><Header /></BrowserRouter>);
  await userEvent.click(screen.getByRole('button', {name: 'Admin User'}));
  await userEvent.click(screen.getByRole('button', {name: 'Logout'}));
  expect(await screen.findByRole('alert')).toHaveTextContent('Failed to logout');
  expect(screen.getByRole('button', {name: 'Logout'})).toBeEnabled();
});

it("renders the site logo with its configured accessible name", () => {
  vi.stubGlobal("frappe", { boot: { ury_brand: { name: "Example site", logo: "/files/site.png" } } });
  try {
    render(<BrowserRouter><Header /></BrowserRouter>);
    expect(screen.getByRole("img")).toHaveAttribute("src", "/files/site.png");
    expect(screen.getByRole("img")).toHaveAccessibleName("Example site");
  } finally { vi.unstubAllGlobals(); }
});
