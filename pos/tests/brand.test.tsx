import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import { Button } from '@ury/ui';
import InitialLoader from '../src/components/InitialLoader';
import ScreenSizeDialog from '../src/components/ScreenSizeDialog';

it('names the configured site during POS startup', () => {
  vi.stubGlobal('frappe', { boot: { ury_brand: { name: 'Example site' } } });
  try {
    render(<InitialLoader />);
    expect(screen.getByText('Loading Example site...')).toBeInTheDocument();
  } finally { vi.unstubAllGlobals(); }
});

it('offers the classic POS on small displays without a vendor name', () => {
  render(<ScreenSizeDialog />);
  expect(screen.getByText(/classic POS/i)).toBeInTheDocument();
});

it('uses the contrast-selected text colour on branded accent buttons', () => {
  render(<Button variant="accent">Continue</Button>);
  expect(screen.getByRole('button')).toHaveClass('bg-primary', 'text-primary-foreground');
});

// Source text with comments removed: what remains can reach a screen, a label or the bundle.
const uiText = (text: string) => text.replace(/\/\*[\s\S]*?\*\//g, '').replace(/(^|[^:])\/\/.*$/gm, '$1');

it('ships no third-party assistant name, logo or wordmark in the POS sources', () => {
  // G51: users never see the assistant vendor's name or art; the product is Maximus.
  const sources = import.meta.glob(['../src/**/*.{ts,tsx}', '!../src/**/*.test.{ts,tsx}'], { query: '?raw', import: 'default', eager: true }) as Record<string, string>;
  expect(Object.keys(sources).length).toBeGreaterThan(10);
  const offenders = Object.entries(sources)
    .filter(([, text]) => /huf-logo-gradient/.test(text) || /\bHUF\b/.test(uiText(text)))
    .map(([path]) => path);
  expect(offenders).toEqual([]);
});
