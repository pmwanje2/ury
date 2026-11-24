import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { initI18n } from '../i18n';
import ScreenSizeDialog from './ScreenSizeDialog';

beforeEach(async () => {
  await initI18n('en');
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('ScreenSizeDialog mobile (< 1024px) gate', () => {
  it('shows white-label notice and links to Desk (/app), never dead /urypos 404', () => {
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    render(<ScreenSizeDialog />);

    const text = document.body.textContent?.toLowerCase() ?? '';
    expect(text).toContain('desktop only');
    expect(text).not.toContain('ury');
    expect(text).not.toContain('kamra');
    expect(text).not.toContain('huf');
    expect(text).not.toContain('version 1');

    const deskButton = screen.getByRole('button', { name: /open desk/i });
    fireEvent.click(deskButton);

    expect(openSpy).toHaveBeenCalledWith(`${window.location.origin}/app`, '_blank', 'noopener,noreferrer');
  });
});
