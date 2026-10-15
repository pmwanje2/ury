# P2b evidence

Source: URY `9cf57dcf6dc4b3795bbf1884069149c3974921c7`.
Base: `42806a32664d574dd865322cd89b09994dd7f218`.

The RED commit is `41ef7214`: `docs/evidence/P2b/red-native.log` records the
real persistence failures for paid order growth, stale unpaid replacement,
and Phone-plus-Cash settlement; `red-guest.log` records the wrong paid-growth
notice. The GREEN commit is `9cf57dcf`: the focused payment suite passed 18/18,
the API suite passed 34/34, and the guest suite passed 24/24.

The isolated bench was `ury-p2b-qghvrng3`, served only on
`127.0.0.1:19382`, with Frappe/ERPNext, `payments`, `ea_payments`, and URY.
The PAY-007 through PAY-010 Server Scripts were installed from SiteOps main
`3fea38e26aebfd6ff0da10cee5b3cc8ca20b70f1`; both scripts were enabled and
read back with the body hash recorded in `platform-baseline-pay007-010.json`.
The provider transport was the disposable local stub only; Payment Request
documents, the Paid transition, ERPNext `check_phone_payments`, and the final
invoice submit were real.

Browser proof:

- `guest-counter-en.png`, `guest-counter-sw.png`, and `guest-counter-lg.png`
  show the same one Paid request after growth and the localized counter
  message. `guest-result.json` records one provider navigation and five taps
  returning the same request; no second request was created.
- `cashier-phone-cash-before.png` shows the ordinary URY Cashier entering
  `12,000` Cash plus `12,000` Pesapal/Phone. `cashier-settled-payments.png`
  is the submitted native invoice readback screen.
- `native-before.json` / `native-after.json` record one Paid request,
  `docstatus=1`, `outstanding_amount=0`, and the two final payment rows.

The bench was torn down after capture with `docker compose -p
ury-p2b-qghvrng3 -f /tmp/ury-p2b-QgHVRNG3/compose.yaml down -v`.
