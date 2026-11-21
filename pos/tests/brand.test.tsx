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
