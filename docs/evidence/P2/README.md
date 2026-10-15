# P2: guest Phone Payment Request (disposable integration proof)

Review fix (idempotent re-tap, unwrapped errors, row gateway):
[review-fix/README.md](review-fix/README.md).
Review fix 2 (paid re-tap never reopens checkout; remainder after growth):
[paid-retap/README.md](paid-retap/README.md).

Scope: [tracker #3712](https://github.com/Maximus-Tech-Enterprise-Apps/maximus-siteops/issues/3712),
item P2; approved [Gap #3445 section 6.2](https://github.com/Maximus-Tech-Enterprise-Apps/maximus-siteops/issues/3445).
Public AGPL URY fork only. No production deployment, merchant credentials, real SMS,
real payment, auto-submission, or changes to `make_invoice` / tenant scripts.

## Revisions and environment

| Input | Exact pin |
| --- | --- |
| URY base `maximus/integration-hospitality-20261004a` | `24acff6ca6d5a810727aa6bd40954f00534f837e` |
| Last tests-only RED | `bdf6afc2fe345d77b18d68def4630c7e704dd94c` |
| Production GREEN | `2dd642a` |
| Comment-only refactor / browser source | `e6a4bbe53f9e54eab066ae8626154588edeea5a2` |
| Qualified final code/test revision | `a2547fe225959666c2e9a4e905b5afbc7bf8ce78` |
| ea_payments `main` | `e8d66497e1254b90d23fdf882e06efb124541f61` |
| frappe/payments `version-16` | `cca07d9f9392e2ea0e521c5975151db9e4b6c321` |
| Frappe / ERPNext | 16.52.0 / 16.50.0 (image source is UNVERSIONED) |
| Image | `frappe/erpnext@sha256:0c84219faf21600d824408daf39c4bddd21d0ef4898a4aae36bf3cc57b95f5e2` |
| Interpreter | Python 3.14.8, GCC 14.2.0 (inside image) |
| Browser/build tools | Chrome headless; Node 24.22.0; Vite 6.4.3 |
| Local gates | Ruff 0.16.4; gitleaks 8.30.1; Git 2.43.0 |

Date: 2026-10-15. Unique Docker Compose project `ury-p2-hktjtxtp`, site
`p2.localhost`, only published port `127.0.0.1:19372:8000`. MariaDB 11.8 and
Redis 6.2 stayed on the project network. Scheduler disabled. All five apps
were installed before browser acceptance, as recorded in the native readback.
An initially partial payments installation was repaired with native
`bench --site p2.localhost install-app payments --force`, then `install-app ea_payments`.

The browser ran the clean, attested `e6a4bbe` release copy with self-order
assets built in scratch. The only later source change is test-import ordering.
The served and final `self_ordering.py` both had SHA-256
`a63c4d22b9df56fcc2a15cb81ee8dce71592f4f42c403ec4b90b43cf1dc632dd`.
See [initial attestation](source-attestation.json) and
[final clean-source attestation](source-attestation-final.json).

## RED then GREEN

Tests-only commits: `e47d4f7`, `4b23552`, `bdf6afc`. They precede the
production-only GREEN commit `2dd642a`. `e6a4bbe` only corrects settlement
comments; `a2547fe` only sorts test imports. Evidence commits follow these.

| Selection | Result | Capture |
| --- | --- | --- |
| Initial RED, 8 native-database regression tests | 8 failures, 0 errors | [red-initial.log](red-initial.log) |
| Initial GREEN, same tests | 8 passed, 0 skipped | [green-initial.log](green-initial.log) |
| Recheck clean detached `bdf6afc`, same 8 tests | 8 failures, 0 errors | [red-recheck.log](red-recheck.log) |
| Recheck clean detached `a2547fe`, same 8 tests | 8 passed, 0 skipped | [green-recheck.log](green-recheck.log) |
| Complete touched API module at `a2547fe` | 34 passed, 0 skipped | [api-green.log](api-green.log) |

RED redirect test genuinely reached v16's old Shopping Cart branch:
`AssertionError: 'redirect' == 'redirect'`. The other failures were the missing
server-staged tender or missing stable configuration refusal, not collection errors.
The raw RED recheck log lacks a SHA header because the revision command wrote
outside `tee`; [invocation attribution](red-invocation.md) preserves that distinction.

Commands (inside the disposable project; [runner](run-tests.py) pre-warms native
System Settings before the older module's broad `get_doc` mocks):

```sh
git -C <clean-mounted-checkout> rev-parse HEAD
git -C <clean-mounted-checkout> status --porcelain=v1
docker compose -p ury-p2-hktjtxtp -f compose.yaml exec -T bench \
  env/bin/python /proof/run_tests.py ury.tests.test_guest_phone_payment
docker compose -p ury-p2-hktjtxtp -f compose.yaml exec -T bench \
  env/bin/python /proof/run_tests.py ury.ury.api.test_self_ordering
```

The RED/GREEN rechecks mounted a separate clean checkout at the named revision
as `apps/ury`; production files were not rewritten to simulate RED. They used
the same eight test identities, image, site, and interpreter. Unit tests stub
only the PSP controller; invoice and Payment Request persistence/validation are real.
The eight cover both missing-configuration cases, exact total, positive outstanding,
ignored client amount/gateway, default gateway selection, redirect/mute-email flags,
and cashier settlement.

The exact tenant fence was disabled **only for these pathless unit-test calls**,
then restored. Leaving it enabled correctly refuses the helper's direct
`sync_order` with `SAL-POS-CHECK-POS-ONLY`; this is not a product failure or a
test skip. Browser guest and ordinary-cashier acceptance below ran with the
unaltered fence enabled. Native NumberFormat deprecation and a duplicate lab
`apps.txt` entry produced warnings, not failures/skips.

Source/prose `git diff --check`, new test-file Ruff, and gitleaks passed.
Raw `.log` artifacts retain native traceback tabs and Compose trailing spaces;
the source/prose whitespace check excludes those verbatim captures. Full touched-file
Ruff has seven pre-existing findings at the base: five in `self_ordering.py`,
two in `test_self_ordering.py` ([capture](lint-candidate.log)); no new findings.
These were not suppressed or fixed as unrelated P2 work. No hosted test CI
claim is made; the fork's existing workflows are deployment workflows.

## Guest and native document proof

Synthetic UGX restaurant, signed native pickup QR, a real anonymous Guest
browser session, native menu/order creation, default Phone gateway account
and matching POS Profile Pesapal mode. The English UI calls the existing
"Pay by phone" action **Pay Online**; no frontend label was changed.

Pesapal Settings used Sandbox and dummy credentials. Native SMS Settings
pointed to a fake transport host. [HTTP stub](provider-http-stub.py) intercepts
only the provider and SMS transport; it asserts provider order amount 12000
and currency UGX. No ERP document/controller/permission method is stubbed in
the browser server. Provider checkout URL and successful status are **stub
responses**, not a real Pesapal sandbox transaction. Navigation to the returned
URL was intercepted/aborted before any external checkout connection.

| Scenario | Observed result | Evidence |
| --- | --- | --- |
| Guest places order and presses Pay Online | invoice total / Phone tender / submitted Phone PR all UGX 12,000 | [guest page](guest-pay-online.png?raw=true), [native PR](payment-request-desk.png?raw=true) |
| No Shopping Cart transport | HTTP 200 JSON, no Location; client subsequently navigates to returned URL | [browser response and target](guest-result.json), [actual intercepted target](provider-navigation-intercepted.png?raw=true) |
| Missing company Payment Gateway Account | HTTP 417, `URY-PAYMENT-NOT-CONFIGURED`, no PR for refused invoice | [guest refusal](guest-refusal.png?raw=true), [response](refusal-result.json), [native records](native-readback-final.json) |
| Native request flags | channel Phone; mute_email 1; make_sales_invoice 0; draft invoice not auto-submitted | [before settlement](native-readback-final.json) |
| Native provider IPN/re-query then ordinary cashier | IPN verified by ea_payments against HTTP stub; PR Paid; cashier HTTP 200 and invoice docstatus 1 | [cashier result](cashier-result.json), [native after](native-after-settlement.json), [cashier screen](cashier-settled.png?raw=true) |

Primary request `ACC-PRQ-2026-00005`, invoice `P2-8be922db-00003`.
The refused invoice is `P2-8be922db-00005`; there is no corresponding request.
Earlier harness iterations created additional synthetic drafts/requests visible
in the captures; none were real sales. All were removed with the lab volumes.
The PR screenshot is Administrator readback, **not** ordinary-cashier PR access.

## Settlement and Salama findings

`make_invoice` **overwrites** the guest's Phone row: it calls
`invoice.set("payments", [])`, rebuilds from cashier-selected tenders, saves,
then submits. The ordinary cashier therefore must select the same gateway mode
and amount. In this proof, row `lsjofevmtt` was replaced by `orhpd7i359`,
Pesapal UGX 12,000, and submission succeeded. No `make_invoice` change is needed
for that matching-tender path; no automatic preservation is claimed.

The exact declaration read from SiteOps `28b86f027a0f20cc3c99b8948a627c81f4c7c300`,
`tenants/salama-prod/server-scripts.v1.json`, was installed unchanged:
**Salama - POS Invoice changes only through the POS**, Before Validate, enabled;
SHA-256 `23c0aab6265515d6547939c6abc131404abdfc79f8a129bb66ca2f868736b75a`.
Its explicit Administrator allowance permits the new save inside the existing
`_elevated()` block; the guest route need not be added to its staff allowlist.
The ordinary cashier (URY Cashier only, no System Manager) received HTTP 403
`SAL-POS-CHECK-POS-ONLY` on `frappe.client.save`, then successfully settled via
the already-allowlisted `ury.ury.doctype.ury_order.ury_order.make_invoice` route.
This is compatibility with that declared script in a lab, not verification of
Salama's currently deployed configuration or its other Server Scripts.

Native POS drafts leave `outstanding_amount` at zero until submission even
when unpaid. The fix uses a positive server outstanding amount when present,
otherwise the server rounded/grand total. The positive-outstanding test checks
60 against invoice total 100. No client amount parameter was added.

## Limits / review / teardown

- Native Phone submission invokes `request_for_payment` despite `mute_email`.
  ea_payments requires SMS Settings (PAY-005). The current guest flow does not
  collect a telephone number; its native sender here was Administrator, not a
  verified mobile. SMS delivery and real-provider phone acceptance are **not proven**.
- Existing guest price formatting displays a rupee symbol on the menu despite
  native UGX documents; unchanged, separately scoped frontend gap.
- Default URY Cashier cannot read Payment Request (HTTP 403 in cashier capture).
  Cashier matching-tender settlement works, but a visible Paid-request queue
  needs separate native permissions/UX qualification. No permission was expanded.
- PAY-007 rejection of an **unpaid** gateway tender is separate tracker P4;
  not implemented or claimed here. Split tender, post-request order changes,
  rate-limit behavior, Flutterwave, live merchant checkout and
  real funds/SMS remain outside this P2 proof.
- [Opus verification-specialist review](verification.md): evidence-fed code
  review PASS, no blocking code issue. Independent command execution remains
  **CANNOT-VERIFY**: its read-only sandbox failed before executing commands.
  The initial review lacked the subsequently added full RED recheck, whole API
  suite, attestation and cleanup captures. Its claim that native gateway lookup
  ignores company is contradicted by the [installed v16 source](gateway-details.txt).
  The [follow-up](verification-followup.md) accepts those captures and retracts
  that company-lookup concern, while retaining the raw RED-log attribution caveat.
  [CodeRabbit](coderabbit.log) was unavailable (organization HTTP 403); no pass claimed.
- The complete unique project was removed with
  `docker compose -p ury-p2-hktjtxtp -f compose.yaml down --volumes --remove-orphans`.
  [Teardown output](teardown.log) and [zero-resource readback](cleanup-counts.json)
  confirm no project containers, volumes or networks remain. This deleted only
  synthetic lab data; evidence remains here. No production host was mutated.
