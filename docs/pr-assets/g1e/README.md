# Salama G1E guest-phone evidence

These are real screenshots captured on 2026-10-20 from Vite development servers and headless Chrome. The browser rendered the worktree's actual URY components and self-order application; no production site, Frappe site, or customer account was used.

## Captures

- `till-guest-phone-qr.png` — the till guest-phone QR dialog for a draft table invoice.
- `waiter-guest-phone-qr-action.png` — the waiter/captain actions menu entry.
- `guest-phone-qr-flag-off.png` — the action absent when the profile flag is off.
- `guest-phone-from-grant.png` — the self-order phone after consuming `#g=grant-demo`, showing the existing bill, orders, Call waiter, Request Bill, and menu items.
- `guest-phone-add-note-dialog.png` — the real guest note dialog opened from the granted phone view.
- `guest-phone-expired-denied.png` — the generic refusal after the mocked grant endpoint returns the expired/revoked refusal.

## Harness and disclosures

- POS captures used a temporary Vite entry importing `GuestPhoneQrDialog` and `CaptainActionsMenu` directly from this worktree. The `issue_table_grant` and Frappe `get_qr_code` calls were served by a local Vite middleware. The QR image is an intentionally non-scannable local SVG placeholder; it demonstrates the dialog's real image/countdown layout, not a live grant.
- The flag-off capture rendered the same dialog with its real `visible={false}` gate.
- Phone captures used a temporary Vite entry importing the real `self-order/src/main.tsx`. Local middleware returned a representative granted ordering context, menu, bill/order, and action responses. The denied capture returned HTTP 403 with `URY_GUEST_GRANT_REQUIRED` from the same mock endpoint.
- Local mock data is synthetic (`Salama`, table `T-07`, invoice `POS-INV-1042`). These captures are presentation evidence only; backend qualification and live tenant acceptance remain separate gates.
