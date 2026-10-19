import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { call } from '@ury/core';
import GuestPhoneQrDialog from '../src/components/GuestPhoneQrDialog';
import CaptainActionsMenu from '../src/captain/components/CaptainActionsMenu';

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

async function renderGuestQr(props: { visible: boolean; invoice: string | null }) {
  await act(async () => root.render(<GuestPhoneQrDialog {...props} />));
}

function button(label: string) {
  return [...container.querySelectorAll<HTMLButtonElement>('button')]
    .find((candidate) => candidate.textContent?.trim() === label);
}

describe('Guest phone QR', () => {
  it('hides the action when the profile flag or draft table invoice is unavailable', async () => {
    await renderGuestQr({ visible: false, invoice: 'INV-1' });
    expect(button('Guest phone QR')).toBeUndefined();

    await renderGuestQr({ visible: true, invoice: null });
    expect(button('Guest phone QR')).toBeUndefined();
  });

  it('issues only the current invoice, encodes the grant in a URL fragment, and replaces it on reissue', async () => {
    const post = vi.spyOn(call, 'post').mockImplementation(async (path: string, params?: Record<string, unknown>) => {
      if (path.endsWith('issue_table_grant')) {
        const session = post.mock.calls.filter(([method]) => method.endsWith('issue_table_grant')).length === 1
          ? 'grant-one' : 'grant-two';
        return { message: { session, expires_at: '2099-01-01 00:30:00' } } as never;
      }
      if (path.endsWith('get_qr_code')) {
        return { message: `data:image/svg+xml;base64,${btoa(String(params?.value))}` } as never;
      }
      throw new Error(`Unexpected endpoint ${path}`);
    });

    await renderGuestQr({ visible: true, invoice: 'INV-1' });
    await act(async () => button('Guest phone QR')?.click());
    expect(post).toHaveBeenNthCalledWith(1, 'ury.ury.api.self_ordering.issue_table_grant', { invoice: 'INV-1' });
    expect(post.mock.calls[1][0]).toBe('frappe.utils.print_format_generator.get_qr_code');
    expect(String(post.mock.calls[1][1]?.value)).toContain('/order/#g=grant-one');
    expect(String(post.mock.calls[1][1]?.value)).not.toContain('?g=');
    expect(container.querySelector('img')?.getAttribute('src')).toMatch(/^data:image\/svg\+xml;base64,/);

    await act(async () => button('Guest phone QR')?.click());
    expect(post).toHaveBeenNthCalledWith(3, 'ury.ury.api.self_ordering.issue_table_grant', { invoice: 'INV-1' });
    expect(String(post.mock.calls[3][1]?.value)).toContain('/order/#g=grant-two');
    expect(container.querySelector('img')?.getAttribute('src')).toMatch(/^data:image\/svg\+xml;base64,/);
  });

  it('shows ERP refusal text verbatim', async () => {
    vi.spyOn(call, 'post').mockRejectedValue({
      _server_messages: JSON.stringify([JSON.stringify({ message: 'URY_GUEST_GRANT_REQUIRED: Please ask staff for a new ordering link.' })]),
    });
    await renderGuestQr({ visible: true, invoice: 'INV-1' });
    await act(async () => button('Guest phone QR')?.click());
    expect(container.textContent).toContain('URY_GUEST_GRANT_REQUIRED: Please ask staff for a new ordering link.');
  });
});

it('adds Guest phone QR to the captain actions menu only when enabled', async () => {
  const onGuestPhoneQr = vi.fn();
  await act(async () => root.render(
    <CaptainActionsMenu
      isOpen
      onOpenChange={vi.fn()}
      showGuestPhoneQr
      onGuestPhoneQr={onGuestPhoneQr}
    />
  ));
  expect(button('Guest phone QR')).toBeDefined();
  await act(async () => button('Guest phone QR')?.click());
  expect(onGuestPhoneQr).toHaveBeenCalledTimes(1);
});

