import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

vi.mock("./ScreenSizeDialog", () => ({
  default: () => <div>Screen Size Dialog</div>,
}));

import ScreenSizeProvider from "./ScreenSizeProvider";

describe("ScreenSizeProvider", () => {
  const originalLocation = window.location;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders children when screen is large enough", () => {
    const originalInnerWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", {
      value: 1200,
      writable: true,
      configurable: true,
    });

    render(
      <ScreenSizeProvider>
        <div>Test Content</div>
      </ScreenSizeProvider>
    );

    expect(screen.getByText("Test Content")).toBeInTheDocument();

    Object.defineProperty(window, "innerWidth", {
      value: originalInnerWidth,
      writable: true,
      configurable: true,
    });
  });

  it("keeps the desktop gate for settings", () => {
    const originalInnerWidth = window.innerWidth;
    Object.defineProperty(window, "innerWidth", {
      value: 500,
      writable: true,
      configurable: true,
    });

    render(
      <ScreenSizeProvider pathname="/settings">
        <div>Test Content</div>
      </ScreenSizeProvider>
    );

    expect(screen.getByText("Screen Size Dialog")).toBeInTheDocument();

    Object.defineProperty(window, "innerWidth", {
      value: originalInnerWidth,
      writable: true,
      configurable: true,
    });
  });

  it("exempts /order routes from size check", () => {
    const originalInnerWidth = window.innerWidth;

    Object.defineProperty(window, "innerWidth", {
      value: 500,
      writable: true,
      configurable: true,
    });

    // Mock window.location with /order path
    const mockLocation = { ...originalLocation, pathname: "/order/123" };
    Object.defineProperty(window, "location", {
      value: mockLocation,
      writable: true,
      configurable: true,
    });

    render(
      <ScreenSizeProvider>
        <div>Test Content</div>
      </ScreenSizeProvider>
    );

    expect(screen.getByText("Test Content")).toBeInTheDocument();

    // Restore
    Object.defineProperty(window, "innerWidth", {
      value: originalInnerWidth,
      writable: true,
      configurable: true,
    });

    Object.defineProperty(window, "location", {
      value: originalLocation,
      writable: true,
      configurable: true,
    });
  });

  it("responds to window resize events", () => {
    const originalInnerWidth = window.innerWidth;
    
    Object.defineProperty(window, "innerWidth", {
      value: 1200,
      writable: true,
      configurable: true,
    });

    const { rerender } = render(
      <ScreenSizeProvider>
        <div>Test Content</div>
      </ScreenSizeProvider>
    );

    expect(screen.getByText("Test Content")).toBeInTheDocument();

    Object.defineProperty(window, "innerWidth", {
      value: originalInnerWidth,
      writable: true,
      configurable: true,
    });
  });
});

it('opens the online register on a 390px handheld', () => {
  vi.stubGlobal('innerWidth', 390);
  const previous = window.location.pathname;
  window.history.replaceState(null, '', '/pos/register');
  try {
    render(<ScreenSizeProvider><div>Handheld register</div></ScreenSizeProvider>);
    expect(screen.getByText('Handheld register')).toBeVisible();
  } finally {
    window.history.replaceState(null, '', previous);
    vi.unstubAllGlobals();
  }
});

it.each([[390, 844], [768, 1024]])('allows online till entry and the floor plan at %i × %i', (width, height) => {
  vi.stubGlobal('innerWidth', width);
  vi.stubGlobal('innerHeight', height);
  try {
    for (const pathname of ['/pos', '/', '/tables', '/pos/tables', '/register']) {
      const view = render(<ScreenSizeProvider pathname={pathname}><main>{pathname}</main></ScreenSizeProvider>);
      expect(screen.getByRole('main')).toHaveTextContent(pathname);
      view.unmount();
    }
  } finally { vi.unstubAllGlobals(); }
});
