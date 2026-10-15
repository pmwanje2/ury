# E5: named-account short payment (local evidence, 15 October 2026)

Base: `7f0c2aeefbff3ac5c238462a7e4ab1cde546495c`, fetched from
`pmwanje2/ury:maximus/integration-hospitality-20261004a`. The integration head
was re-read before handoff; O4 merge `ebd31b120b23a68b951709e90e8665a6a71de96e`
is an ancestor. No backend, offline outbox, phone-payment, tender/change,
rounding or permissions code changed.

## RED then GREEN

- RED `06f1100f3cfab1165f7f5ffa55d1b5f67954a3d1`: actual React PaymentDialog
  tests; `npm run test:pos` reported **5 failed, 462 passed**, 0 skipped.
  The three named-account tender cases (40, 0, empty), processing and O4 cases
  failed at `expected true to be false`: Pay was incorrectly disabled.
- `2dac769`: test-only correction retains the button reference while its
  accessible text changes to Processing. No production code in that commit.
- GREEN `a6efd4818a728f54960e856829d0c822610ad0bb`: same directory suite,
  **467 passed**, 38 files, 0 failed/skipped. Base: **448 passed**, 37 files.
  There are 19 new cases; no original test was removed.
- `npm run build --workspace pos`: PASS, including the HTML-copy script and
  PWA build. Node compatibility directory selection
  `node --test pos/tests/*.test.mjs pos/tests/*.test.cjs`: **16 passed**,
  0 failed/skipped.
- `tsc -p pos/tsconfig.app.json --noEmit`: **not green**; the same 8 existing
  diagnostics on base and candidate. `npm run lint --workspace pos`:
  **not green**; the same 151 errors / 56 warnings, with only three line
  numbers moving by four lines in PaymentDialog. No lint rule/check weakened.

Node: `/home/paulmwanje/.nvm/versions/node/v24.20.0/bin/node` (24.20.0).
Locked dependencies installed using `npm ci --ignore-scripts`; no dependency
or lockfile change. The fork's two workflows are deployment workflows, not
component CI jobs; neither was dispatched. No remote write or merge occurred.

The gate checks both the loaded profile identity and its native binary flag,
a nonempty customer, and a known different default customer. A shortfall can
then pass the UI with positive or zero tender. The server remains responsible
for credit groups, limits, comp authorization and posting. This does not prove
zero-total comp settlement, real receivables, consolidation, stock or GL.

## Dev-server screenshots

Real URY App/router, Orders page, PaymentDialog, shared UI/theme and Zustand
stores, at **`/pos/orders`**, served only at `127.0.0.1:18783`. Local fixture
user/profile/bill/opening/auth state and synthetic ERP responses; every write
is refused. Not live ERP or authentication/permission acceptance. Chromium
captured each at widths 1280 and 412, height 1000, scale 1. No production
development harness was added.

| State | 1280 | 412 |
| --- | --- | --- |
| Named account: tender 40,000 / bill 100,000; Pay enabled | [image](account-short-1280.png) | [image](account-short-412.png) |
| Walk-in: same short tender; Pay disabled | [image](walkin-short-1280.png) | [image](walkin-short-412.png) |
| Walk-in: tender 120,000; change 20,000 unchanged | [image](walkin-change-1280.png) | [image](walkin-change-412.png) |

`/` and `/tg` from the generic SiteOps implementer brief are not URY cashier
routes. URY mounts its router at `/pos`; neither irrelevant route is offered
as payment-dialog evidence. The local fixture server reports 404 for both.

## Release dependencies

Prepare the fork PR against the integration branch above, preserving newer
integration work on any refresh. Parent reviews before authorizing one push.
Only after the exact candidate has been pushed may a separate SiteOps PR
amend ADR-0021/evidence and runtime/site source pins. Do not pin a local SHA;
the parent can retarget the pin to the eventual fork squash commit.

The Salama configuration companion is
`fix/salama-auto-credit-config-20261015`: enable the existing profile's native
partial-payment flag, retain the existing credit/comp Server Script, and use
the runbook for the two groups and three comp-reason customers. No fixture
master-creation capability, merge, deployment, fixture apply or ERP write is
part of this change. Coordinate with SiteOps hospitality issue #3712.
