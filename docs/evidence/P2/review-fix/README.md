# P2 review fix: idempotent re-tap, real errors, row gateway

Responds to the Grok FAIL review on pmwanje2/ury#41 (head `96147db`) and the
Sol note in SiteOps #3818. Same pins as [the P2 evidence](../README.md):
image `frappe/erpnext@sha256:0c84219f…` (Frappe 16.52.0, ERPNext 16.50.0),
payments `cca07d9f`, ea_payments `e8d66497`, Python 3.14.8. New disposable
project `ury-p2-r2si0lew`, only port `127.0.0.1:19372`, scheduler disabled,
removed with volumes afterwards ([teardown](teardown-r2.log),
[zero resources](cleanup-r2.json)).

## Behaviour changed

1. Re-tap: when a submitted, not cancelled/failed Payment Request already
   exists for this session's invoice, this gateway account and the same
   server amount, `create_payment_request` returns its name and payment URL.
   No second request and no second provider order.
2. `get_payment_status` also returns `payment_url` for an open request
   (none once Paid/Cancelled/Failed).
3. The broad `except` around `make_payment_request` is gone. Only missing
   configuration (checked before ERPNext is called) gets
   `URY-PAYMENT-NOT-CONFIGURED`. Other errors (for example ea_payments
   `PAY-005`, or ERPNext "already created") reach the guest as they are.
4. The gateway account is the Payment Gateway Account (Phone) whose
   `payment_account` is the Phone row's own account, passed explicitly.
   The company default gateway is only a tie-breaker between profile rows.

Amount stays server-only (positive outstanding, else rounded/grand total).
Cash-settling an invoice that already has a Paid gateway request is a
double-charge risk; that is SiteOps PAY-007 (separate PR), not this change.

## RED then GREEN

Tests-only RED `cf87424`, then production-only GREEN `71f7035`. Each run
mounted the clean checkout at that SHA (first lines of every log).

| Revision | Selection | Result | Log |
| --- | --- | --- | --- |
| RED `cf87424` | guest native-DB suite (12) | 4 failures, 0 errors | [log](red-guest-cf87424.log) |
| RED `cf87424` | `ury.ury.api.test_self_ordering` (34) | 1 failure | [log](red-api-cf87424.log) |
| GREEN `71f7035` | guest native-DB suite (12) | 12 OK | [log](green-guest-71f7035.log) |
| GREEN `71f7035` | `ury.ury.api.test_self_ordering` (34) | 34 OK | [log](green-api-71f7035.log) |

RED reasons: re-tap got `Online payment isn't set up` (from ERPNext
"Payment Request is already created"); status had `payment_url` None;
`PAY-005` was relabelled as not set up; with another company-default
gateway the Phone row was refused as `URY-PAYMENT-NOT-CONFIGURED`; the
mocked API test saw the relabelled message.

Modules are run separately. Run together in one process, two unrelated
`TestRequestAssistance` tests error at both RED and GREEN (MagicMock
pickling via the System Settings cache), same as at base.

## Live HTTP re-tap

Pesapal Sandbox with dummy credentials, provider/SMS HTTP stubbed as before,
exact Salama fence enabled. Real guest HTTP calls ([script](retap.mjs),
[result](retap-result.json), [native readback](native-readback-r2.json)):

- first and second `create_payment_request` (the second also sent `amount=1`):
  both HTTP 200, no `Location`, same `ACC-PRQ-2026-00001` and same URL,
  UGX 12,000;
- `get_payment_status` returned the same `payment_url`;
- native readback: one submitted Payment Request (gateway
  `Pesapal - UGX - P2`, mode Pesapal), one Integration Request, one
  provider order in the server log.

Not repeated: browser screenshots. The first-tap page and refusal paths are
unchanged in the UI; the earlier screenshots stay as they were.
