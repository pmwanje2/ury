# O4 cashier-till offline outbox

Tracker: [SiteOps #3712 O4](https://github.com/Maximus-Tech-Enterprise-Apps/maximus-siteops/issues/3712).
Contract: ADR-0040 section 4, extending ADR-0019. Public AGPL URY fork only;
no SiteOps clean-room app changes, new service, or new DocType.

## Outcome

Offline capture requires `POS Profile.ury_offline_capture = 1` and one of that
profile's billing roles. The shipped native Custom Field is a Check with default
`0`, included in the hooks fixture filter. Native IndexedDB stores orders,
positive same-outage rounds, timestamps, stable client/round IDs, provisional
cash/manual mobile-money settlement, print acknowledgement and replay progress.
Manual mobile-money requires a typed reference and puts it in native order
comments. Persistent storage is requested; a refusal produces a visible warning.

Replay is FIFO under the browser's cross-tab Web Lock, one order at a time.
An authenticated uncached ping precedes `sync_order(append=1)`; a settled order
then calls `qz_print_update` and `make_invoice` with the same IDs. Network errors,
login/Access redirects, incomplete acknowledgements and transient transport auth
failures retain the item. Business shift/total/permission/stock refusals park it
under Needs review. Capture, settlement and logout cannot race replay.

The banner exposes pending count, Replaying, reprint controls, offline tender
and Needs review. Offline voids, reductions, discounts, transfers, merges,
splits, rounds on online orders, card, pay-by-phone, fiscal printing and till
close are refused. Till close stays blocked with pending or review rows.
Logout/cache purge waits for an empty outbox and awaits O3's asynchronous purge
seam before navigation. Parked records are deliberately retained for operator
reconciliation; they are never silently deleted or retried as successful.

## RED/GREEN evidence

| Behaviour | RED | GREEN |
| --- | --- | --- |
| Durable FIFO outbox, retry IDs, refusals, close guard | `f5e402b7` | `ae74f6e8` |
| Cashier wiring, browser print, banner, fixture | `6f2001d1` | `8174af18` |
| Cashier controls, positive rounds, logout/reprint | `5786d423` | `0b903763` |
| Bounded replay, role revocation, offline action safety | `7fab2bab` | `d36caa1f` |
| Await O3 cache purge and native walk-in fallback | `c2f951ca` | `ae288fd4` |
| Retain transient authentication failures | `a3bfb04e` | `432e778e` |

The initial walk-in GREEN `7e9e0e76` was committed before a qualified GREEN run.
It was deleted in `f4f0f6bb`, then restarted with genuine RED and a separate
production-only GREEN. Harness-only commits are separate from production.

Captured commands/output, not inferred from tests added after implementation:

[Captured RED and teardown output](test-output.txt); [final merged verification output](merged-output.txt).

```text
npm run test:pos -- --no-cache pos/tests/offline-cashier.test.tsx pos/tests/offline-capture.test.ts
RED c2f951ca: Tests 2 failed | 29 passed (31)
  expected true to be false [navigation before async purge completed]
  expected [] to have a length of 1 [native walk-in order not captured]
GREEN ae288fd4: Test Files 2 passed (2); Tests 31 passed (31)

npm run test:pos -- --no-cache pos/tests/offline-outbox.test.ts
RED a3bfb04e: Tests 3 failed | 27 passed (30)
  expected 'review' to be 'pending'
  [401 SessionExpired; 400 CSRFTokenError; 403 Login to access]
GREEN 432e778e: Test Files 1 passed (1); Tests 30 passed (30)
```

## Verification at final code SHA 5c7caadf800609dc8c461c5ef7506a84be28fe22

| Command | Actual result |
| --- | --- |
| `npm run test:pos -- --no-cache` | 35 files, **428 passed**, no failed/skipped tests |
| `node --test pos/tests/logout.test.ts pos/tests/order-tabs.test.ts pos/tests/till-close-api.test.mjs` | **25 passed**, 0 failed/skipped |
| `python3 -m unittest ury.tests.unit.test_offline_capture_fixture` | **1 passed** |
| `npm run build --workspace pos` | production build passed; existing chunk-size warning |
| `bench --site ury.localhost run-tests --app ury --module ury.tests.test_idempotent_writes` | **12 passed** on disposable native v16 bench |
| `bench --site ury.localhost run-tests --app ury --module ury.tests.test_offline_replay` | **19 passed** on disposable native v16 bench |
| `npx --no-install tsc --noEmit -p pos/tsconfig.app.json` | fails with the same eight pre-existing diagnostics as the landed O3 base `678bf9b3` |

The native O1/O2 tests precede the final two-line frontend auth fix; their server
sources did not change. Node used v24.22.0; Vitest 3.2.6; Chrome
150.0.7871.186. Base and candidate typechecks used the same TypeScript binary
and dependency tree. Existing errors: unused `discountType`/`showAdjustment`
in PaymentDialog, unused ProductDialog argument, TableMergeDialog comparison,
and unused Table import. Typecheck is not reported green.

Independent `verification-specialist` (Opus) reviewed O4 at `432e778e` against
`a402c6c5`: PASS, and separately reviewed the O3 merge composition at the final
head: PASS. The first review independently ran the full **378-test** suite and
focused offline tests; the merged local run independently passed **428 tests**.
This is code review, not independent native/browser acceptance. CodeRabbit could not review:
`Connection failed: Invalid organization` (403).

## Disposable native browser proof

Unique Compose project **`ury-o4-tgmi9v`**, site `ury.localhost`, published only
as `127.0.0.1:18794 -> 8000/tcp`. Image
`frappe/erpnext:v16.50.0` at digest
`sha256:0c84219faf21600d824408daf39c4bddd21d0ef4898a4aae36bf3cc57b95f5e2`:
Frappe **16.52.0**, ERPNext **16.50.0**. These are lab v16 versions, not a claim
about an installed production pin. All records/users are synthetic.

O4 code above was combined in a scratch build with O3 **`fd3c448a`** for its
shell/read cache. The O4 offline sources and OrderPanel were compared byte for
byte with this branch. No O3 `pos/vite.config.ts`, core storage, or PWA sources
are included in the O4 diff. O3 must land for offline reload/read-cache support.

Fresh Chrome profiles logged in as ordinary **URY Cashier**
`offline-bb843f65@example.com`, not Administrator. Its opted-in native profile
was `Offline Till bb843f65`; the billing role was URY Cashier. CDP
`Network.emulateNetworkConditions` made the till offline. Three real UI orders
were captured, one settled cash, and the browser print window showed the
provisional bill. The profile's native walk-in customer was reused without an
offline server search. Chrome refused persistent storage, and the banner
correctly showed the storage warning.

Restoring network produced Replaying, then emptied the pending queue. Native
cashier API readback and filtered native Desk lists showed exactly **three
distinct POS Invoices**: one paid/submitted, two unpaid drafts, with one unique
`ury_client_order_id` per order. All three linked KOTs were **Served** with
`ury_offline_printed = 1`. The KOT screenshot uses native ListView columns for
those actual fetched fields; it is not a reconstructed table.

A fourth offline capture used a browser clock one day before the open shift.
The real server returned `URY_OFFLINE_OUTSIDE_SHIFT` (417); the banner retained
it as **0 pending / 1 Needs review**.
See [native readback and actual replay payloads](readback.json).

The disposable bench's Installed Application setup flags were completed to
admit native Desk; the earlier System Settings-only flag did not admit it on
this Frappe v16 version. No production configuration was changed.

### Screenshots

Three queued offline orders:

![Offline pending count](offline-pending.png?raw=true)

Cash settlement's browser print window:

![Provisional bill](provisional-bill.png?raw=true)

Serialized replay:

![Replaying](replaying.png?raw=true)

One invoice per order (one Paid, two Draft):

![Native POS Invoice list](pos-invoice-list.png?raw=true)

Native KOT Served and offline-print fields:

![Native KOT list](kot-list.png?raw=true)

Real outside-shift refusal retained:

![Needs review](needs-review.png?raw=true)

## Dependencies, cleanup and CANNOT-VERIFY

Production storage uses **native IndexedDB**: no new production dependency.
Test-only **`fake-indexeddb@6.2.5`**, exact package/lock pin, **Apache-2.0**,
verified from the installed package's licence metadata and LICENSE.
`idb-keyval` was not added. URY remains its existing public **AGPL-3.0** fork.

After capturing evidence, all fresh Chrome profiles were removed and
`docker compose -p ury-o4-tgmi9v -f /tmp/ury-o4-TGmi9V/compose.yaml down --volumes --remove-orphans`
removed the disposable containers, its database/sites/logs volumes and its
network. Exact-project container, volume and network queries returned empty.
The synthetic database is intentionally discarded, not recoverable from the
bench; screenshots/readback remain here. No shared Docker resources were
pruned and no production site was used.

CANNOT-VERIFY: CodeRabbit review (organization rejected); production deployment;
physical printer paper delivery (browser print path/window only); real Cloudflare
Access/session expiry and mobile-money provider settlement (unit classification
and manual tender, not provider interaction); tax-exclusive/taxed menu totals;
and grant of browser persistent storage (Chrome returned false). Local typecheck
remains baseline-red as described above (8 diagnostics at both landed O3 base and
final candidate). The transient 403 login-text fallback
matches English; a localized Guest-denial message remains an edge qualification
risk. Hosted CI is reported separately in the draft PR, not inferred from local
tests. This evidence authorizes no merge or deployment.
