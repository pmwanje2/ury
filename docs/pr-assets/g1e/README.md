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

Which frames are harness pages:

- `till-guest-phone-qr.png`, `waiter-guest-phone-qr-action.png` and `guest-phone-qr-flag-off.png` are a harness page, not the real Till/OrderPanel or CaptainOrder screen. Only the dialog and the actions menu are product components. Harness-only text: the "SALAMA · URY TILL" header, the "Table T-07 · POS-INV-1042" title, "Draft table bill · QR grant expires in 30 minutes", "Captain order / Open table order", "Self Ordering Profile · guest invoice grant required: OFF", and "Guest phone QR is not available for this profile." With the flag off, the product renders nothing in place of the button.
- The three phone frames render the real self-order app (`self-order/src/main.tsx`). All text in them is product text, and only the API responses are mocked.
- These frames predate the server-side waiter visibility check. Since that change, visibility comes from `can_issue_table_grant`; the harness renders the dialog and menu with `visible` passed in directly.

Currency: `guest-phone-from-grant.png` was retaken with a harness `window.frappe.boot` (`sysdefaults.currency = UGX`), standing in for the boot data that `ury/www/order.py` injects in production. Without a boot currency, `packages/core/src/format.ts` falls back to "₹". That fallback predates this PR and is left unchanged here; removing it is a follow-up. In production, Salama shows UGX only if System Settings currency is UGX (not verified live). The bill rows and total show plain numbers with no currency; that is also existing behaviour.

- POS captures used a temporary Vite entry importing `GuestPhoneQrDialog` and `CaptainActionsMenu` directly from this worktree. The `issue_table_grant` and Frappe `get_qr_code` calls were served by a local Vite middleware. The QR image is an intentionally non-scannable local SVG placeholder; it demonstrates the dialog's real image/countdown layout, not a live grant.
- The flag-off capture rendered the same dialog with its real `visible={false}` gate.
- Phone captures used a temporary Vite entry importing the real `self-order/src/main.tsx`. Local middleware returned a representative granted ordering context, menu, bill/order, and action responses. The denied capture returned HTTP 403 with `URY_GUEST_GRANT_REQUIRED` from the same mock endpoint.
- Local mock data is synthetic (`Salama`, table `T-07`, invoice `POS-INV-1042`). These captures are presentation evidence only; backend qualification and live tenant acceptance remain separate gates.
