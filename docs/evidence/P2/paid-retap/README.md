# P2 review fix 2: a paid guest is never sent back to checkout

Responds to Grok review 5527497001 on pmwanje2/ury#41 (head `d4d4d3e`).
Same pins as [the P2 evidence](../README.md). New disposable project
`ury-p2-r3kg9pjb`, only port `127.0.0.1:19372`, scheduler disabled,
removed with volumes ([teardown](teardown-r3.log), [zero resources](cleanup-r3.json)).

## Behaviour

- **Blocking fix.** Paid and Partially Paid requests are excluded from the
  re-tap lookup. If money already received on this gateway covers the
  server total, `create_payment_request` returns `status: "Paid"`,
  `payment_url: null`, `paid_amount`, `remaining_amount: 0` and creates
  nothing. `get_payment_status` also returns no URL for Partially Paid.
  The guest page uses a small `paymentOutcome` helper: a Paid or Partially
  Paid response never redirects, even if a link is present, and shows
  "Paid (X) — thank you."
- **Order grew after a Paid request.** The next tap requests only the
  remainder (server total minus paid on this gateway). The stored Phone row
  stays the full total; ERPNext is given an unsaved copy of the invoice
  whose Phone row holds the remainder, so `make_payment_request` sizes the
  new request from it and its own validation still checks against the
  stored total. The response carries `paid_amount` and `remaining_amount`
  and the page says "Already paid X, paying the remaining Y" before
  redirecting. Re-tap of the remainder returns the same request. Several
  Paid requests then sum to the one Phone row, which SiteOps PAY-007/PAY-008
  (#3826) accept.
- Amount stays server-only. New copy is in en/sw/lg.

Not changed: an *unpaid* open request when the order grows (Grok's
non-blocking note). ERPNext still subtracts the open request and creates a
request for the difference. Cash-settling after a Paid request stays with
PAY-008.

## RED then GREEN

Tests-only RED `bad7b4a` + `ac006c8` (the second adds a `frappe.get_all`
mock so the mocked API tests model "no earlier Paid requests"), then GREEN
`699aa90` (production and frontend only). An unpushed first GREEN attempt
failed the mocked API module because it had no such mock; it was undone
locally, the test-only commit was added, and GREEN was recommitted.

| Revision | Selection | Result | Log |
| --- | --- | --- | --- |
| RED `ac006c8` | guest native-DB suite (15) | 2 failures, 1 error (`KeyError: 'paid_amount'`) | [log](red-guest-ac006c8.log) |
| RED `ac006c8` | `ury.ury.api.test_self_ordering` (34) | 34 OK (mock only) | [log](red-api-ac006c8.log) |
| RED `ac006c8` | frontend `node --test` (20) | 3 failures (catalog, missing `payment-outcome.ts`) | [log](red-frontend-ac006c8.log) |
| GREEN `699aa90` | guest native-DB suite (15) | 15 OK | [log](green-guest-699aa90.log) |
| GREEN `699aa90` | `ury.ury.api.test_self_ordering` (34) | 34 OK | [log](green-api-699aa90.log) |
| GREEN `699aa90` | frontend `node --test` (24) | 24 OK | [log](green-frontend-699aa90.log) |

RED reasons: re-tap after Paid returned the checkout URL; status returned a
URL for Partially Paid; the remainder response had no `paid_amount`.
`tsc --noEmit` reports the same three pre-existing `TS2554` errors
(Landscape/Portrait/Tablet layouts) at base and head; no new ones.

## Browser proof (Pesapal Sandbox, dummy keys, provider/SMS HTTP stubbed, Salama fence on)

Built guest page from `699aa90` in scratch ([script](paid.mjs),
[responses](paid-remainder-result.json), [native readback](native-readback-r3.json)):

1. Guest orders UGX 12,000 and taps Pay Online: `ACC-PRQ-2026-00001`,
   redirect to its link. Native ea_payments IPN (stubbed status) marks it Paid.
2. Guest taps again: `status: Paid`, `payment_url: null`, **0
   navigations**, page shows "Paid (12000 UGX) — thank you."
   [screenshot](guest-paid-retap.png?raw=true)
3. Guest adds a second tea (total 24,000) and taps: new
   `ACC-PRQ-2026-00002` for 12,000, `paid_amount` 12,000,
   `remaining_amount` 12,000, page says "Already paid 12000 UGX, paying
   the remaining 12000 UGX." and redirects to the new link.
   [screenshot](guest-remainder.png?raw=true)

Readback: invoice total 24,000, Pesapal Phone row **24,000**; requests
00001 (12,000, Paid) and 00002 (12,000, Requested); one Integration Request
each.
