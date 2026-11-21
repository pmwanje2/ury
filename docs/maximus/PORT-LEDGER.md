# Port ledger: Maximus fork onto upstream `version-16`

Decision D21/D22 (route B). This ledger lists every Maximus fork commit that is not in any
`ury-erp/ury` branch and records where it goes on the version-16 line. Rows are keyed by **fork
commit**, not by base, so a later base merge-forward (B1: we track upstream `version-16`) does
not change any row. A base move only adds a "base" entry below.

## Base and frozen line

| Item | Value |
|---|---|
| Integration branch | `maximus/v16-integration` |
| Base (upstream `version-16`, `git ls-remote` 2026-11-20) | `100c43fe2e52b21e41523c61e7fc1902baa96940` (`__version__ = "3.0.0-beta.2"`) |
| Old line, frozen | tag `maximus/v2-final` → `32741738f004b2f22c4aa7d8c8484ae986c2cd61` (live pin) |
| Upstream merge-base of the old line with `version-16` | `b05d3ba4c7a27aaff7278d7fdd419e2d6f2d71d7` |
| Upstream `version-2` merged into the old line | `79215b29d2161675344c29810ebb8b6fe710f9b6` |

### Base moves

| Date | From | To | PR |
|---|---|---|---|
| 2026-11-20 | — | `100c43fe` | branch cut (no merge) |
| 2026-11-20 (P1 re-check) | `100c43fe` | `100c43fe` | no move: `git ls-remote` still `100c43fe`, so no merge-forward PR |

## Scope and method

Selection: `git rev-list --no-merges 32741738 --not --remotes=upstream` with every upstream
branch fetched. That is **695** commits. It equals `git rev-list --no-merges 79215b29..32741738`,
because everything before `79215b29` on the old line is upstream history (1,402 commits since the
version-16 merge-base minus 707 upstream commits).

Source PR: the first-parent fork merge or squash PR that brought the commit in. "direct" means it was
committed to the integration line without a fork PR.

Status:
- **port** — carried in the named theme PR on `maximus/v16-integration`. A ported commit is
  re-authored (tests first as RED, then code as GREEN), not cherry-picked.
- **superseded** — upstream `version-16` already has the change (patch-id match with
  `git patch-id --stable`, or the cited upstream commit or a later native/neutral replacement).
- **dropped** — not carried, with the reason (evidence-only files, reverted pairs).

The Note column identifies the landing PR, replacement, or reason for dropping each row.
“This PR (T9c)” identifies the final candidate; its changes reach integration upon merge.

`git cherry upstream/version-16 32741738 79215b29` gives 691 `+` and 4 `-`. The 4 `-` rows are the
patch-identical ones marked superseded below.

## Summary

| Theme | Name | ported | superseded | dropped | total |
|---|---|---:|---:|---:|---:|
| N | Neutralise upstream (new work; no fork commits) | 0 | 0 | 0 | 0 |
| T0 | Security guards | 12 | 0 | 0 | 12 |
| T1 | Build closure (npm locks, test:closure, POS CI) | 9 | 0 | 2 | 11 |
| T2 | Posting and closing correctness | 88 | 12 | 0 | 100 |
| T3 | KOT, hold/fire, station print | 82 | 1 | 0 | 83 |
| T4 | Floor PIN | 39 | 0 | 7 | 46 |
| T5 | Offline capture | 79 | 4 | 2 | 85 |
| T6 | Guest, feedback, pricing | 68 | 11 | 35 | 114 |
| T7 | Branding | 55 | 6 | 0 | 61 |
| T8 | Room lookup and Kamra | 4 | 0 | 0 | 4 |
| T9 | POS UX remainder | 119 | 0 | 4 | 123 |
| T10 | Docs and evidence (not carried) | 0 | 2 | 54 | 56 |
| | **Total** | 555 | 36 | 104 | 695 |

Theme N (neutralise upstream: Insights auto-grant gate, Report override removal, migrate-time
sidebar hide, Stock Reconciliation draft guard, `role_permissions.py`) has no fork commits; it
is new work and landed in #95.

Notes:
- The plan names `fbee56e9`/`24f99f84`→`c5a67776` (sent-note removal authorization) under T0
  but says it lands with the U3 held-order projections. Those are T3, so the rows are T3.
- `2fbb6e04` ("one Custom Field fixture row per field") is ported with the first theme that adds
  a fork Custom Field (T2, `custom_blind_cash_count`), together with the fixture-filter
  completeness assertion. Its GREEN `71a1152f` is superseded: the version-16 fixture has no
  repeated rows.
- The urgent-fix rule: a fix made on `maximus/v2-final`'s line during the port is added here as
  a new row and carried as a separate theme PR.

## Theme PRs

A row is closed when its theme PR merges into `maximus/v16-integration`. Until then it lists
the open PR.

| Theme | PR | RED | GREEN | State |
|---|---|---|---|---|
| T1 | #93, #124 | `5b5b34da`, `108d922f` | `664d8ff4`, `b8f3c4da` | merged: locks, closure and hosted POS CI |
| N | #95 | `8b6aa8a0`, `80f70004`, `1f98feff` | `042c8e29` | merged; no fork rows |
| T0 | #96 | `ba3154e3`, `1747bae1`, `6692547b`, `7f0f931e`, `a7578aaf` | `b0d12217` | merged |
| T5 part 3 | #123 | `e6a0290a`, `b59ba37e` | `6c24d276`, `3bb914d0` | merged over #107 and #110 |
| T9a | #126 | `a51f9071` | `4adcca54` | merged: backend and legacy floor |
| T9b | #127 | see PR | see PR | merged at `a70b6c47`: React and shared till |
| T9c | this PR | `59f00eb0` | `3d12942d` | final candidate: 18 T9 residual fork rows; see cross-theme reconciliation below |

T9c also closes two cross-theme gaps found while reconciling the source PRs. The #118 paired-tablet boundary now handles #123’s provisional offline identity (RED `af70b144`, GREEN `fa52c1ec`). The v16 workflow now runs all eleven frozen site-less POS modules in separate processes, including the CI portions of `615f62e4`, `bc031298` and `688b596b` (RED `5adc41c7`, GREEN `c57441fe`). The candidate has **0 open or unassigned implementation rows**. Integration reaches that state when this PR merges.

Other merged theme PRs: T2 #109/#117; T3 #114/#115/#125; T4 #118; T6 #102/#105/#106/#108/#113 (follow-ups #112/#116/#120); T7 #122; T8 #114. B5 must be rerun on the integrated revision before freeze.

T5 accounts for 85 distinct fork commits: 12 ported in #107, 43 in #110,
24 ported in part 3, four superseded by the v16 default-customer resolver,
and two dropped test harnesses. Part 3 also finishes UI and projection work
on 16 previously counted rows. `8174af18` counts once in part 3; its fixture and
filter landed in #107. No T5 implementation rows remain deferred.

T0 notes on rows marked "port" in the rows below:
- `04099204`, `fc2a3970`: carried as tests only where they apply; the duplicate guest-grant
  layer they remove (#56/#59) never reached `version-16`, so nothing is removed.
- `016938a0`: the `pos/tests` guest specs it edits do not exist on `version-16`; the POST
  contract is covered by `self-order/src/lib/api-http.test.ts` and `api.test.ts` in #96.
- `d9457602`: the `load_country` part is superseded by `version-16`'s own guard; the
  dashboard and wizard-status parts are in #96.

### T9b and T9c — final disposition by fork commit

#127 covers the React POS. This PR (T9c) completes the legacy Vue and backend paths listed below.
The shared #27 rows `1e732d8e` and `1bd5e78d` combine #126’s API with #127’s React floor cards and booking tests.
The appointment badge is advisory and separate from the branch’s hard reservation check. No configuration or customer data is seeded.

This keyed supplement updates the corresponding fork rows with the final split between #126, #127 and T9c.

| Fork commits | Disposition on v16 |
|---|---|
| `e0d5d18e`, `b5e86a30` | Captain and cashier floor dialogs read #126’s invoice-scoped eligible candidates. Search filters this roster locally; a refused read leaves no options. `captain-transfer-dialog.test.tsx`. |
| `8f4ac36b`, `40f8d9f8` | The cashier table menu requires the transfer role and a positive candidate read for that table’s invoice; closing or switching menus invalidates the read. #126 owns server write eligibility and self-transfer refusal. `captain-transfer-menu.test.tsx`. |
| `01bd8d07`, `b7ba8a3c` | Captain floor uses #126’s `reservation_enabled` context for hard lock reads and occupancy rules. Cashier floor gates reservation polling and lock checks on branch settings. Both disabled-floor suites exercise these paths; unrelated select and branding changes are outside this disposition. |
| `492ac51d`, `7dec3e97`, `7d18b8b1` | React captain already sends to the kitchen, locks the pending action and returns to Tables; `captain-walk-in-order` covers anonymous sends. T9c ports the legacy Cart/Pax controls, pending lock, refusal and acknowledged send. |
| `7d6bc789`, `a92006cb` | React retains the matching invoice customer and displays Frappe’s billed-order refusal. T9c adds readable Vue errors and preserves the customer on a blank update. |
| `89429dae`, `3a3a676a` | v16 `sync_order` resolves a new invoice from the order-type/profile default. React supplies the loaded existing invoice customer; T9c resolves blank updates from the saved invoice, then the configured default. |
| `499a5d1d`, `d942f807` | React cashier opens the acknowledged invoice in Orders; captain returns to Tables (floor sign-out keeps precedence). T9c returns Vue waiters to Tables and opens the acknowledged check for cashiers. |
| `e1722257`, `078404d2`, `4ea7132b` | Shared CSRF-safe sign-out, scoped cleanup and pending-action guards. v16 React has no persisted order-tab collection; old tab keys are purged. T9c routes the legacy Vue caller through the shared CSRF POST. |
| `bd78795d`, `a379feb4`, `05737a5e`, `ebe37108` | Management and POS retain state on logout failure. Successful cleanup includes legacy Mosaic localStorage keys matching `/^.+_.+_strike$/`; another app on the same origin using that pattern can lose matching keys. Mosaic checks the CSRF POST before purging. #123 worker/outbox protections retained. Legacy Vue waiter/cashier logout preserves foreign strike keys on refusal and successful retry; Mosaic retains its existing strike cleanup. |
| `c94bd75e`, `7b1d816f` | Orders exposes print, split and settle for eligible checks and applies the profile’s print-before-pay rule. Outstanding and both bill-pickup paths pass the linked invoice and combined amount to settlement even without a split group; the pickup tests verify the dialog total without posting a payment. |
| `ca0dfdda`, `f8d76b8e` | Profile, payment modes, customer groups, territories and paid-limit/merge reads use cookie-owned keys; stale responses cannot populate another user’s cache. |
| `75e634ab`, `091bfa0e` | Resolve branch on the server and read URY Production Unit with native document permissions; `production-api.test.ts`. |
| `7b3c4430`, `2157f927` | Read the current Frappe cookie locally for cache ownership. Guest/missing cookies cannot read legacy profiles; `profile-cache.test.ts`. |
| `bd7cc393`, `77368707`, `df3e6f35`, `44c186c4` | Cashier Home opens the branch’s unpaid drafts oldest first, with independently optional site-clock ages and item previews. Split cards use a generic label until numbered metadata is available in selected detail. Permission-filtered native lists replace the frozen custom query. |
| `8f35fad2`, `98b312db` | Bill pickup distinguishes a missing/deleted invoice from an unavailable lookup; `BillRequestStrip.test.tsx`. |
| `7e7cd07e`, `82075f4f` | Cashier role takes Home precedence across roles; explicit analytics, captain and provisional offline register routes retained. |
| `57cdac91`, `4ed639cc`, `a7b1ad05` | Bill requests require the strip’s native branch read to match the Orders queue’s confirmed branch. A queue branch change discards old rows, choices and late lookups; a same-owner, same-branch list retry can keep rows. Exact, table-only and split pickup covered. |
| `2d5d6ed5`, `bab890ba`, `f049f262` | Orders mounts #125’s shared Notifications component; native record and exact KOT check navigation use current user/branch. |
| `6a24e1ca`, `a1fa7644`, `2be36009`, `13435364`, `868e7217` | Reused #125’s native read state, refresh-race guards and site clock; `Notifications.test.tsx` qualifies the shared implementation on cashier Home. |
| `caf7f11f`, `c7885262` | T9c: `cancel_order` uses native discard for drafts and native cancel for submitted invoices. Vue search handles nullable Walk-in fields. `test_ury_order.py`, `matchOrder.test.js`. |
| `0197abec`, `0aaa0d9b`, `087cc044`, `d2a64e6e` | Opening, closing and checklist gates reuse till logout; rejection preserves checklist inputs and permits retry. |
| `f9e31842`, `7fec963b`, `40c43f55`, `f3277dcf` | New routine React sends pass an explicit empty customer to v16; edits preserve invoice identity and named/aggregator orders retain validation. T9c covers the shared Vue post-send navigation. React and Vue retain their respective authentication flows. |
| `caa7ad62`, `36e594c9` | All React till logout surfaces retain `/login?redirect-to=%2Fpos`; failed logout stays on the current screen. |
| `1e732d8e`, `1bd5e78d` | React badges use #126’s optional read-only endpoint, site-local time and first name. Empty/absent reads leave the table selectable; hard reservation locks remain independent. |
| `26a630e6`, `b6c3330f` | Known-offline and lost-ack walk-ins use the configured customer or refuse capture; pending local rounds and named customers retain identity. No seeded default. |
| `dbc36d59`, `3936081b`, `82c78d69`, `320267e7` | Captain sends a blank customer for native resolution on new live orders, preserves matching invoice customer on edit, and keeps v16 floor authorization; no captain offline queue. |
| `8d84fa3f`, `05bcc006`, `3e7beb8c` | Captain menu/order panes scroll independently inside one dynamic viewport, with visible total/send controls; desktop and phone exercised. |
| `c7e345ad`, `69c14fe6` | New populated carts confirm an order-type change. Cancel/reselect keeps lines; confirmation clears lines and releases dine-in table/room. Saved order edit restrictions retained. |
| `b244590e`, `b7210ffd` | Removed the unsourced Shift Brief card; the real shift dashboard remains. |

## Rows (oldest first)

| Fork commit | Date | Source | Theme | Status | Subject | Note |
|---|---|---|---|---|---|---|
| `0bc10600` | 2026-10-01 | direct | T0 | port | test(csrf): cover session token for urypos and mosaic | PR #96 |
| `ce3a4e14` | 2026-10-01 | direct | T0 | port | fix(csrf): render session token for urypos and mosaic | PR #96 |
| `492ac51d` | 2026-10-01 | direct | T9 | port | test(pos): reproduce waiter send-to-kitchen failures | #127 + this PR (T9c); see keyed disposition above. |
| `7dec3e97` | 2026-10-01 | direct | T9 | port | test(pos): accept equivalent blank Pax HTML attributes | #127 + this PR (T9c); see keyed disposition above. |
| `7d18b8b1` | 2026-10-01 | direct | T9 | port | fix(pos): streamline waiter sends to kitchen | #127 + this PR (T9c); see keyed disposition above. |
| `7d6bc789` | 2026-10-01 | direct | T9 | port | test(pos): reproduce customer fallback and billed-order feedback gaps | #127 + this PR (T9c); see keyed disposition above. |
| `a92006cb` | 2026-10-01 | direct | T9 | port | fix(pos): preserve invoice customer and explain billed-order sends | #127 + this PR (T9c); see keyed disposition above. |
| `89429dae` | 2026-10-01 | direct | T9 | port | test(pos): prefer profile default over new invoice customer | #127 + this PR (T9c); see keyed disposition above. |
| `3a3a676a` | 2026-10-01 | direct | T9 | port | fix(pos): reuse customer only from existing invoices | #127 + this PR (T9c); see keyed disposition above. |
| `499a5d1d` | 2026-10-01 | direct | T9 | port | test(pos): reproduce role-based post-send routing | #127 + this PR (T9c); see keyed disposition above. |
| `d942f807` | 2026-10-01 | direct | T9 | port | fix(pos): restore cashier post-send check routing | #127 + this PR (T9c); see keyed disposition above. |
| `e1722257` | 2026-10-01 | direct | T9 | port | test(pos): reproduce unsafe logout and shared order tabs | #127 + this PR (T9c); see keyed disposition above. |
| `078404d2` | 2026-10-01 | direct | T9 | port | fix(pos): make logout CSRF-safe and isolate saved order tabs | #127 + this PR (T9c); see keyed disposition above. |
| `4ea7132b` | 2026-10-01 | direct | T9 | port | refactor(test): keep logout regressions outside POS app compilation | #127 + this PR (T9c); see keyed disposition above. |
| `bd78795d` | 2026-10-01 | direct | T9 | port | test(frontend): reproduce unsafe logout and destructive cache clearing | #127 + this PR (T9c); see keyed disposition above. |
| `a379feb4` | 2026-10-01 | direct | T9 | port | fix(frontend): reuse CSRF-safe logout and scoped storage cleanup | #127 + this PR (T9c); see keyed disposition above. |
| `05737a5e` | 2026-10-01 | direct | T9 | port | test(logout): reproduce leaked setup state and navigation re-entry | #127 + this PR (T9c); see keyed disposition above. |
| `ebe37108` | 2026-10-01 | direct | T9 | port | fix(logout): clear shared-till state and retain navigation guards | #127 + this PR (T9c); see keyed disposition above. |
| `9223e62f` | 2026-10-01 | direct | T3 | port | test(kitchen): define serve undo recall and late contracts | #115 |
| `3cfe480c` | 2026-10-01 | direct | T3 | port | fix(kitchen): add serve undo recall and late feedback | #115 |
| `4c741657` | 2026-10-01 | direct | T3 | port | test(mosaic): catch nonreactive in-flight serve state | #115 |
| `89d2b61a` | 2026-10-01 | direct | T3 | port | fix(mosaic): keep in-flight serve state reactive | #115 |
| `4a72d819` | 2026-10-01 | direct | T3 | port | test(mosaic): reproduce browser-clock ticket age defect | #115 |
| `e33e5b4d` | 2026-10-01 | direct | T3 | port | fix(mosaic): anchor ticket elapsed time to server clock | #115 |
| `8f2bd68f` | 2026-10-01 | direct | T3 | port | test(mosaic): reproduce socket lifecycle gaps on kitchen actions base | #115 |
| `120e9614` | 2026-10-01 | direct | T3 | port | fix(mosaic): integrate socket lifecycle with kitchen actions | #115 |
| `5bbf1288` | 2026-10-01 | direct | T3 | port | test(kitchen): reproduce reconnect chime and stale resync races | #115 |
| `29e2c41d` | 2026-10-01 | direct | T3 | port | fix(kitchen): chime once for missed tickets and discard stale resyncs | #115 |
| `944747ff` | 2026-10-01 | direct | T2 | port | test(pos): cover occupied till context and readable opening errors | #109, #117; plain-text error parsing supplied by `995d2427`. |
| `1cdbc0bb` | 2026-10-01 | direct | T2 | port | fix(pos): explain occupied tills without exposing unreadable entries | #109, #117; plain-text error parsing supplied by `995d2427`. |
| `6ceaed89` | 2026-10-01 | direct | T2 | port | test(pos): repair opening context row and permission mocks | #117 |
| `ffe5f38c` | 2026-10-01 | direct | T2 | port | test(pos): cover shared tills and readable private occupancy messages | #109, #117 |
| `b30d397d` | 2026-10-01 | direct | T2 | port | fix(pos): preserve shared tills and format private occupancy messages | #109, #117 |
| `1dc234f4` | 2026-10-01 | direct | T2 | port | refactor(test): isolate opening Vitest harness from app compilation | #109 |
| `90038eea` | 2026-10-01 | direct | T2 | port | test(pos): reject cashier email in occupied till context | #109, #117 |
| `e65ba3bb` | 2026-10-01 | direct | T2 | port | fix(pos): keep occupied cashier email out of opening context | #109, #117 |
| `d3cd943d` | 2026-10-01 | direct | T2 | port | test(pos): cover localized occupied till holder and start time | #109 |
| `9b38f06b` | 2026-10-01 | direct | T2 | port | fix(pos): localize occupied till fallback and start time | #109 |
| `43ea6fb1` | 2026-10-01 | direct | T2 | port | test(pos): cover opt-in blind cash count and profile propagation | #109, #117 |
| `06360510` | 2026-10-01 | direct | T2 | port | feat(pos): add opt-in blind cash count to closing | #109, #117 |
| `a8c8ffdb` | 2026-10-01 | direct | T2 | port | test(pos): cover expected-total card leaks during blind count | #109 |
| `82fcba72` | 2026-10-01 | direct | T2 | port | fix(pos): hide grand and net totals during blind cash count | #109 |
| `115a3802` | 2026-10-01 | direct | T2 | port | refactor(test): isolate closing Vitest harness from app compilation | #109 |
| `2b5f238f` | 2026-10-01 | direct | T2 | port | test(pos): prove blind zero-row warnings do not disclose sales | #109 |
| `34b5d977` | 2026-10-01 | direct | T2 | port | fix(pos): base blind zero-row warnings only on opening amounts | #109 |
| `62de5817` | 2026-10-01 | direct | T2 | port | test(pos): prevent sales-dependent rows during blind count | #109 |
| `28def85f` | 2026-10-01 | direct | T2 | port | fix(pos): seed blind count rows only from opening and cached modes | #109 |
| `f626a332` | 2026-10-01 | direct | T2 | port | test(pos): cover blind count sales activity summary leaks | #109 |
| `e8af299b` | 2026-10-01 | direct | T2 | port | fix(pos): hide sales activity summaries during blind count | #109 |
| `badbb214` | 2026-10-01 | direct | T2 | port | test(pos): reconcile hidden sales-only tenders in blind close | #109 |
| `dc0c1732` | 2026-10-01 | direct | T2 | port | fix(pos): reconcile hidden sales-only tenders in blind close | #109 |
| `43811585` | 2026-10-01 | direct | T2 | superseded | test(pos): expose browser-clock closing period regression (RED) | v16 server clock (`227e4ded`); reconciled in #109. |
| `89e1f123` | 2026-09-08 | direct | T2 | superseded | Port POS Closing Entry clock-integrity check using server time | patch-identical to upstream 4138eb35 |
| `67e35429` | 2026-09-15 | #387 | T2 | superseded | fix(pos): anchor closing period end to server clock, not browser clock (#387) | patch-identical to upstream 227e4ded (#387) |
| `dbaa33ff` | 2026-10-01 | direct | T9 | port | test(pos): expose hard-coded customer defaults (RED) | #126: Customer draft reads the native Selling Settings customer group and territory; covered in `test_customer_defaults.py`. |
| `8a7cbd89` | 2026-10-01 | direct | T9 | port | fix(pos): use native Selling Settings customer defaults (GREEN) | #126: Customer draft reads the native Selling Settings customer group and territory; covered in `test_customer_defaults.py`. |
| `ebe03553` | 2026-10-01 | direct | T9 | port | test(pos): reject missing customer group and resolve native territory root (RED) | #126: Missing customer group refuses with a configuration message. A missing territory resolves the actual nested-set root; explicit territory wins. `test_customer_defaults.py`. |
| `184a3d62` | 2026-10-01 | direct | T9 | port | test(pos): preserve validation messages in customer defaults double (RED) | #126: Missing customer group refuses with a configuration message. A missing territory resolves the actual nested-set root; explicit territory wins. `test_customer_defaults.py`. |
| `db3bed33` | 2026-10-01 | direct | T9 | port | fix(pos): require customer group default and resolve native territory root (GREEN) | #126: Missing customer group refuses with a configuration message. A missing territory resolves the actual nested-set root; explicit territory wins. `test_customer_defaults.py`. |
| `799dc28e` | 2026-10-01 | direct | T2 | port | test(pos): RED whole-till closing authorization and invoice contract | #109, #117 |
| `7f603121` | 2026-10-01 | direct | T2 | port | fix(pos): count every bill on the holder's open till | #109, #117 |
| `81c2f646` | 2026-10-01 | direct | T2 | port | test(pos): RED opening holder drives till read and closing | #109 |
| `82fdb0d4` | 2026-10-01 | direct | T2 | port | test(pos): RED till query filter and GET-only endpoint | #117 |
| `e922832d` | 2026-10-01 | direct | T2 | port | fix(pos): close the opening holders till with GET-only reads | #109, #117 |
| `d36b7728` | 2026-10-01 | direct | T2 | port | test(pos): RED count-only close and selected opening profile | #109 |
| `1b05d806` | 2026-10-01 | direct | T2 | port | fix(pos): count-only close uses selected opening profile | #109 |
| `d0617904` | 2026-09-26 | direct | T7 | superseded | test(core): formatCurrency falls back to the site currency from frappe.boot | Upstream site currency (`9998891b`); verified in #122. |
| `9a6f7045` | 2026-09-26 | direct | T7 | superseded | test(core): match Frappe's own symbol resolution in the boot fallback cases | Upstream site currency (`9998891b`); verified in #122. |
| `efa0c6d1` | 2026-09-26 | direct | T7 | superseded | fix(core): use the site currency from frappe.boot when no symbol is stored | Upstream site currency (`9998891b`); verified in #122. |
| `00d2503a` | 2026-09-26 | direct | T7 | superseded | docs(core): document the frappe.boot currency fallback in formatCurrency | Upstream site currency (`9998891b`); verified in #122. |
| `c52e0844` | 2026-10-01 | direct | T2 | superseded | test(pos): expose blind closing clock integration failures (RED) | Upstream server-clock coverage (`227e4ded`, #109). |
| `0a9a068f` | 2026-10-01 | direct | T2 | superseded | test(pos): reconcile blind closing and server clock harnesses | Upstream server-clock coverage (`227e4ded`, #109). |
| `b0c3c6f6` | 2026-10-02 | direct | T9 | port | test(dashboard): reproduce site-day, ticket-time and table-age defects | #126: `test_dashboard_site_day.py` and `test_service_line_site_day.py`: site business-day bounds and open bill creation. v16 already computes plausible overnight ticket durations in SQL; preserve it, include only Served tickets and retain zero. Existing frontend consumers accept this contract. |
| `bf0db04b` | 2026-10-02 | direct | T9 | port | fix(dashboard): use site business day and valid service durations | #126: `test_dashboard_site_day.py` and `test_service_line_site_day.py`: site business-day bounds and open bill creation. v16 already computes plausible overnight ticket durations in SQL; preserve it, include only Served tickets and retain zero. Existing frontend consumers accept this contract. |
| `62a93557` | 2026-10-02 | direct | T9 | port | test(dashboard): reproduce running-low site-day and cutoff defects | #126: Running-low sales share the site business day and branch cutoff; `test_service_line_site_day.py` exercises the emitted query at midnight and cutoff. |
| `805a9699` | 2026-10-02 | direct | T9 | port | fix(dashboard): share site business day for running-low metrics | #126: Running-low sales share the site business day and branch cutoff; `test_service_line_site_day.py` exercises the emitted query at midnight and cutoff. |
| `c94bd75e` | 2026-10-02 | direct | T9 | port | test(pos): define visible Orders cashier actions (RED) | #127; see keyed disposition above. |
| `7b1d816f` | 2026-10-02 | direct | T9 | port | feat(pos): expose native Orders settle split and print actions (GREEN) | #127; see keyed disposition above. |
| `264f6146` | 2026-10-02 | direct | T2 | port | test(pos): require opening-inclusive closing reconciliation (RED) | #109; v16 React already includes opening cash (`028c1243`). |
| `4a5c4d68` | 2026-10-02 | direct | T2 | port | fix(pos): closing expected amount includes the opening float | #109; v16 React already includes opening cash (`028c1243`). |
| `ca0dfdda` | 2026-10-02 | direct | T9 | port | test(pos): reproduce cross-user profile and payment caches (RED) | #127; see keyed disposition above. |
| `f8d76b8e` | 2026-10-02 | direct | T9 | port | fix(pos): scope profile and permission-sensitive caches per session user (GREEN) | #127; see keyed disposition above. |
| `75e634ab` | 2026-10-02 | direct | T9 | port | test(pos): reproduce missing production-unit endpoint (RED) | #127; see keyed disposition above. |
| `091bfa0e` | 2026-10-02 | direct | T9 | port | fix(pos): load branch production units through existing native APIs (GREEN) | #127; see keyed disposition above. |
| `7b3c4430` | 2026-10-02 | direct | T9 | port | test(pos): require request-free cookie-owned caches and pure keys (RED) | #127; see keyed disposition above. |
| `2157f927` | 2026-10-02 | direct | T9 | port | fix(pos): read cache ownership from the live Frappe cookie (GREEN) | #127; see keyed disposition above. |
| `b023438c` | 2026-10-02 | direct | T2 | port | test(closing): cover whole-till consolidation and submit guards (RED) | #117 |
| `15c551df` | 2026-10-02 | direct | T2 | port | fix(closing): consolidate the whole till on submit (GREEN) | #117 |
| `c1540303` | 2026-10-02 | direct | T2 | port | test(closing): require native closing tax amount field (RED) | #117 |
| `d972cb0f` | 2026-10-02 | direct | T2 | port | fix(closing): map native tax totals to closing rows (GREEN) | #117 |
| `7cfd7fc9` | 2026-10-02 | direct | T9 | port | test(urypos): reproduce floor table age clock and midnight defects (RED) | #126: `Table.js` and `Table.test.js`: occupied age uses the server clock across local midnight. |
| `784781cb` | 2026-10-02 | direct | T9 | port | fix(urypos): use server time for occupied table age (GREEN) | #126: `Table.js` and `Table.test.js`: occupied age uses the server clock across local midnight. |
| `cd51f906` | 2026-10-02 | direct | T9 | port | test(urypos): reproduce room loading blocked by floor requests (RED) | #126: `Table.test.js`: pending or denied floor reads leave the selected room menu and cashier usable. |
| `ab8bfc62` | 2026-10-02 | direct | T9 | port | fix(urypos): keep room menu loading independent of floor requests (GREEN) | #126: `Table.test.js`: pending or denied floor reads leave the selected room menu and cashier usable. |
| `c9057116` | 2026-10-02 | direct | T9 | port | test(urypos): reproduce future-time jitter as a day-old table (RED) | #126: Clamp future clock jitter at zero; preserve actual midnight rollover (`Table.test.js`). |
| `b06854d9` | 2026-10-02 | direct | T9 | port | fix(urypos): clamp clock jitter without losing midnight rollover (GREEN) | #126: Clamp future clock jitter at zero; preserve actual midnight rollover (`Table.test.js`). |
| `1f97c59f` | 2026-10-02 | direct | T9 | port | test(urypos): require uniform padded site-clock durations (RED) | #126: Apply the site offset consistently and pad elapsed minutes (`Table.test.js`). |
| `4d749dd7` | 2026-10-02 | direct | T9 | port | fix(urypos): apply site offset and minute padding uniformly (GREEN) | #126: Apply the site offset consistently and pad elapsed minutes (`Table.test.js`). |
| `6cc06b60` | 2026-10-02 | direct | T9 | port | test(urypos): reproduce long-lived and merged open-bill ages (RED) | #126: Use full creation timestamps for open bills and exact merged-table matches; retain the legacy time-only fallback (`Table.js`). |
| `b81352c4` | 2026-10-02 | direct | T9 | port | fix(urypos): age occupied tables from the open bill creation (GREEN) | #126: Use full creation timestamps for open bills and exact merged-table matches; retain the legacy time-only fallback (`Table.js`). |
| `f89aed6a` | 2026-10-02 | direct | T9 | port | test(urypos): reproduce split seating age and idle floor updates (RED) | #126: `Table.test.js`: reversed split drafts retain the oldest seating age; mounted floor/pickup views tick without a refetch. |
| `ff38ad1e` | 2026-10-02 | direct | T9 | port | fix(urypos): preserve oldest seating age and tick idle floors (GREEN) | #126: `Table.test.js`: reversed split drafts retain the oldest seating age; mounted floor/pickup views tick without a refetch. |
| `98fdcfcf` | 2026-10-02 | direct | T9 | port | test(urypos): clarify reversed invoice fixture order (REFACTOR) | #126: `Table.test.js`: reversed split drafts retain the oldest seating age; mounted floor/pickup views tick without a refetch. |
| `bd7cc393` | 2026-10-02 | direct | T9 | port | test(pos): cashier settlement home contracts (RED) | #127; see keyed disposition above. |
| `77368707` | 2026-10-02 | direct | T9 | port | test(pos): supply real session reader in orders harness | #127; see keyed disposition above. |
| `df3e6f35` | 2026-10-02 | direct | T9 | port | feat(pos): settlement-first cashier home (GREEN) | #127; see keyed disposition above. |
| `44c186c4` | 2026-10-02 | direct | T9 | port | test(pos): provide roles in legacy header fixture | #127; see keyed disposition above. |
| `8f35fad2` | 2026-10-02 | direct | T9 | port | test(pos): distinguish missing bill from unavailable lookup (RED) | #127; see keyed disposition above. |
| `98b312db` | 2026-10-02 | direct | T9 | port | fix(pos): show deleted bill as not open (GREEN) | #127; see keyed disposition above. |
| `7e7cd07e` | 2026-10-02 | direct | T9 | port | test(pos): cashier role wins for every multi-role identity (RED) | #127; see keyed disposition above. |
| `82075f4f` | 2026-10-02 | direct | T9 | port | fix(pos): follow cashier-role Home precedence (GREEN) | #127; see keyed disposition above. |
| `57cdac91` | 2026-10-02 | direct | T9 | port | test(pos): expose bill request branch leak and refresh regressions | #127; see keyed disposition above. |
| `4ed639cc` | 2026-10-02 | direct | T9 | port | fix(pos): scope bill requests to till branch and retain refreshed rows | #127; see keyed disposition above. |
| `a7b1ad05` | 2026-10-02 | direct | T9 | port | test(pos): bind Orders harness to the till branch read | #127; see keyed disposition above. |
| `c0fb83a4` | 2026-10-02 | direct | T2 | port | test(pos): expose daily-close business-day regressions (RED) | #117 |
| `ee393dd3` | 2026-10-02 | direct | T2 | port | fix(pos): daily close check uses the business day (GREEN) | #117 |
| `7d758811` | 2026-10-02 | direct | T0 | port | test: reproduce dashboard guards and wizard status gaps (RED) | PR #96 |
| `d9457602` | 2026-10-02 | direct | T0 | port | fix: native dashboard guards and v16 wizard status (GREEN) | PR #96 |
| `2d5d6ed5` | 2026-10-03 | direct | T9 | port | test(pos): cashier Home native notifications contracts (RED) | #127; see keyed disposition above. |
| `bab890ba` | 2026-10-03 | direct | T9 | port | test(pos): retain offline socket boundary across mock restoration | #127; see keyed disposition above. |
| `f049f262` | 2026-10-03 | direct | T9 | port | feat(pos): share native notifications on cashier Home (GREEN) | #127; see keyed disposition above. |
| `6a24e1ca` | 2026-10-03 | direct | T9 | port | test(pos): native record read-state navigation and refresh races (RED) | #127; see keyed disposition above. |
| `a1fa7644` | 2026-10-03 | direct | T9 | port | fix(pos): preserve native record read state across navigation and refresh (GREEN) | #127; see keyed disposition above. |
| `2be36009` | 2026-10-03 | direct | T9 | port | test(pos): isolate native notification writes at the HTTP boundary | #127; see keyed disposition above. |
| `13435364` | 2026-10-03 | direct | T9 | port | test(pos): reproduce notification age timezone defect (RED) | #127; see keyed disposition above. |
| `868e7217` | 2026-10-03 | direct | T9 | port | fix(pos): use existing site clock for notification ages (GREEN) | #127; see keyed disposition above. |
| `caf7f11f` | 2026-10-04 | direct | T9 | port | test: reproduce draft cancellation and Walk-in order log failures (RED) | this PR (T9c); see keyed disposition above. |
| `c7885262` | 2026-10-04 | direct | T9 | port | fix: discard open checks natively and guard empty order search fields (GREEN) | this PR (T9c); see keyed disposition above. |
| `9dcd3126` | 2026-10-04 | #2 | T2 | port | test: reject invalid void reasons before table release (RED) | #109, #117 |
| `4c514e5b` | 2026-10-04 | #2 | T2 | port | fix: validate void reason before releasing tables (GREEN) | #109, #117 |
| `4eaa0a48` | 2026-10-04 | #3 | T2 | superseded | test: reject hardcoded rupee in zero-balance warning (RED) | version-16 zero_balance_warning uses {{amount}} (7bceb499) |
| `f96b61a1` | 2026-10-04 | #3 | T2 | superseded | fix: remove hardcoded rupee from zero-balance warning (GREEN) | version-16 zero_balance_warning uses {{amount}} (7bceb499) |
| `b31e4f0b` | 2026-10-05 | #4 | T7 | port | test: define Website Settings branding contracts (RED) | #122 |
| `8c2cd542` | 2026-10-05 | #4 | T7 | port | feat: resolve site branding from Website Settings (GREEN) | #122 |
| `58df8b1e` | 2026-10-05 | #4 | T7 | port | test: retain Frappe boot datetime serialization (RED) | #122 |
| `f310b901` | 2026-10-05 | #4 | T7 | port | fix: preserve Frappe boot serialization with HTML-safe JSON (GREEN) | #122 |
| `17cbbb55` | 2026-10-05 | #4 | T7 | superseded | test: require purple primary actions and AA contrast (RED) | D53 site-defined identity in #122 supersedes fixed purple. |
| `09393a21` | 2026-10-05 | #4 | T7 | superseded | style: use AA-contrast Maximus purple primary scale (GREEN) | D53 site-defined identity in #122 supersedes fixed purple. |
| `66dc5435` | 2026-10-05 | #4 | T7 | port | test: include native app_name in hostile template context (RED) | #122 |
| `e2cf79c4` | 2026-10-05 | #4 | T7 | port | fix: JSON-escape native app_name in SPA entries (GREEN) | #122 |
| `004a7673` | 2026-10-05 | #4 | T7 | port | test: make branding render harness independent of fork logout tests | #122 |
| `f60b1138` | 2026-10-05 | #4 | T7 | port | test: preserve native session context in upstream branding renders | #122 |
| `90da9b1a` | 2026-10-05 | #4 | T1 | port | chore: keep base lock identity and existing dependency records | root lock identity; regenerated for v16 workspaces; PR #93 |
| `50937297` | 2026-10-05 | #4 | T7 | port | docs: explain site identity defaults and public source configuration | #122 |
| `9ec19ca8` | 2026-10-05 | #4 | T1 | port | chore: prune unrelated lock additions outside test dependency closure | root lock pruning; regenerated for v16 workspaces; PR #93 |
| `46971662` | 2026-10-05 | #5 | T7 | port | test: define staff branding hook precedence (RED) | #122 |
| `0859e0e3` | 2026-10-05 | #5 | T7 | port | feat: support app branding hooks on staff POS surfaces (GREEN) | #122 |
| `23c175ea` | 2026-10-05 | #5 | T7 | port | test: malformed staff branding hooks fall back instead of failing (RED) | #122 |
| `b0d08557` | 2026-10-05 | #5 | T7 | port | fix: ignore malformed staff branding hooks (GREEN) | #122 |
| `82a14437` | 2026-10-11 | #6 | T2 | port | test: reproduce shared till continuation, cashier attribution and opening logout (#1652, #1653) | #109, #117 |
| `48b19743` | 2026-10-11 | #6 | T2 | port | fix: continue eligible operators on the open till and audit settlement cashier (#1652, #1653) | #109, #117 |
| `40473b54` | 2026-10-11 | #6 | T2 | port | test: reproduce merged cashier audit and silent daily-close continuation gate (#1652) | #109, #117 |
| `bc697654` | 2026-10-11 | #6 | T2 | port | fix: attribute merged settlement and keep native daily-close explanation (#1652) | #109, #117 |
| `cb80fde7` | 2026-10-11 | #6 | T2 | port | test: reproduce occupied till Session Start badge mismatch (RED) | #109 |
| `d5db68ac` | 2026-10-11 | #6 | T2 | port | fix: show occupied till opening time in Session Start badge (GREEN) | #109 |
| `0197abec` | 2026-10-11 | #7 | T9 | port | test: reproduce missing logout on POS pre-session gates (RED #1653) | #127; see keyed disposition above. |
| `0aaa0d9b` | 2026-10-11 | #7 | T9 | port | fix: expose shared logout on POS checklist and pre-session gates (GREEN #1653) | #127; see keyed disposition above. |
| `087cc044` | 2026-10-11 | #7 | T9 | port | test: reproduce checklist input reset on failed logout (RED #1653) | #127; see keyed disposition above. |
| `d2a64e6e` | 2026-10-11 | #7 | T9 | port | fix: preserve checklist progress across failed logout (GREEN #1653) | #127; see keyed disposition above. |
| `fc911dd0` | 2026-10-12 | #13 | T7 | port | test(pos): capture cashier flow and compact alert RED | #122 |
| `64a92b4e` | 2026-10-12 | #13 | T7 | port | feat(pos): improve cashier bill hierarchy and compact native alerts | #122 |
| `d8a8ea25` | 2026-10-12 | #13 | T10 | dropped | docs(pos): attach synthetic cashier viewport evidence | docs/evidence or docs/pr-assets only |
| `7e3d98c2` | 2026-10-12 | #13 | T10 | dropped | docs: move Salama review assets to separate evidence branch | docs/evidence or docs/pr-assets only |
| `1786588f` | 2026-10-12 | #13 | T10 | dropped | docs: remove remaining source-tree screenshot evidence | docs/evidence or docs/pr-assets only |
| `d9fbcffd` | 2026-10-12 | #13 | T10 | superseded | docs: restore unrelated upstream URY asset removed with PR evidence | patch-identical to upstream 4a73ef27 |
| `ffc36406` | 2026-10-12 | #14 | T7 | port | test(waiter): expose unreadable cards and inaccessible table controls | #122 |
| `e8ee645d` | 2026-10-12 | #14 | T7 | port | test(waiter): read focus CSS and use the native active badge | #122 |
| `b774118e` | 2026-10-12 | #14 | T7 | port | fix(waiter): improve native table and menu readability | #122 |
| `1786c79d` | 2026-10-12 | #14 | T10 | dropped | docs(waiter): attach actual source before and after rendering evidence | docs/evidence or docs/pr-assets only |
| `bdcfb16f` | 2026-10-12 | #14 | T7 | port | test(waiter): simplify readability coverage to native behavior | #122 |
| `0bca410b` | 2026-10-12 | #14 | T10 | dropped | docs: move Salama review assets to separate evidence branch | docs/evidence or docs/pr-assets only |
| `b0d879ba` | 2026-10-12 | #14 | T7 | port | test(waiter): retain readability behavior in four focused checks | #122 |
| `a79b599b` | 2026-10-12 | #14 | T10 | dropped | docs: remove remaining source-tree screenshot evidence | docs/evidence or docs/pr-assets only |
| `a34f9485` | 2026-10-12 | #14 | T10 | superseded | docs: restore unrelated upstream URY asset removed with PR evidence | patch-identical to upstream 4a73ef27 |
| `6f59f318` | 2026-10-12 | #12 | T3 | port | test(mosaic): capture kitchen readability regressions (RED) | #115 |
| `6152d712` | 2026-10-12 | #12 | T3 | port | style(mosaic): improve native kitchen ticket readability (GREEN) | #115 |
| `8168fddd` | 2026-10-12 | #12 | T3 | port | test(mosaic): expose hidden ticket layout gaps (RED) | #115 |
| `0e09b161` | 2026-10-12 | #12 | T3 | port | fix(mosaic): exclude hidden tickets from native layout (GREEN) | #115 |
| `81b309aa` | 2026-10-12 | #12 | T3 | port | test(mosaic): capture native footer dismissal gesture (RED) | #115 |
| `9a9ade8a` | 2026-10-12 | #12 | T3 | port | fix(mosaic): restore footer background dismissal (GREEN) | #115 |
| `b5f995cc` | 2026-10-12 | #12 | T10 | dropped | docs: attach actual local Mosaic implementation evidence | docs/evidence or docs/pr-assets only |
| `9f4cd57a` | 2026-10-12 | #12 | T3 | port | test: expose muted kitchen notes and ambiguous elapsed units | #115 |
| `bd5c4726` | 2026-10-12 | #12 | T3 | port | fix: emphasize kitchen notes and keep native actions within viewport | #115 |
| `96b11404` | 2026-10-12 | #12 | T10 | dropped | docs: move Salama review assets to separate evidence branch | docs/evidence or docs/pr-assets only |
| `cff20d51` | 2026-10-12 | #12 | T3 | port | test: expose kitchen footer overflow beneath native toolbar | #115 |
| `e4402a74` | 2026-10-12 | #12 | T3 | port | fix: reserve native kitchen toolbar and error space above tickets | #115 |
| `7fd5e8d9` | 2026-10-12 | #12 | T3 | port | test(mosaic): expose unlabelled requests and unpadded hour labels (RED) | #115 |
| `a492651e` | 2026-10-12 | #12 | T3 | port | fix(mosaic): band special requests and clarify hour labels (GREEN) | #115 |
| `73d3a83c` | 2026-10-12 | #12 | T10 | dropped | docs: remove remaining source-tree PR screenshot | docs/evidence or docs/pr-assets only |
| `55c0c360` | 2026-10-12 | #12 | T3 | port | test(mosaic): expose hidden items and misleading kitchen identity (RED) | #115 |
| `5e0b1a4e` | 2026-10-12 | #12 | T3 | port | fix(mosaic): show every item before serving and preserve kitchen identity (GREEN) | #115 |
| `f9e31842` | 2026-10-13 | #49 | T9 | port | test: reproduce walk-in customer and waiter navigation blockers | #127; see keyed disposition above. |
| `7fec963b` | 2026-10-13 | #49 | T9 | port | test: require explicit blank RPC customer for native fallback | #127; see keyed disposition above. |
| `40c43f55` | 2026-10-13 | #49 | T9 | port | test: use the native invoice success response shape | #127; see keyed disposition above. |
| `f3277dcf` | 2026-10-13 | #49 | T9 | port | fix: allow native walk-in customer resolution for routine orders | #127; see keyed disposition above. |
| `58fc9171` | 2026-10-13 | #17 | T7 | port | test(guest): RED assistance, ERP refusal text and configurable identity | #122 (identity); #113 (guest assistance/refusals). |
| `756c4923` | 2026-10-13 | #17 | T7 | port | feat(guest): GREEN native waiter requests, ERP refusals and profile brand | #122 (identity); #113 (guest assistance/refusals). |
| `5320837e` | 2026-10-13 | #17 | T7 | port | test(guest): RED review edge cases for identity, cooldown and requests | #122 (identity); #113 (guest assistance/refusals). |
| `de3a207e` | 2026-10-13 | #17 | T7 | port | fix(guest): GREEN brand fallbacks and durable waiter cooldown | #122 (identity); #113 (guest assistance/refusals). |
| `3acc9758` | 2026-10-13 | #17 | T7 | port | test(guest): RED real malformed QR signature page regression | #122 |
| `6f0d783b` | 2026-10-13 | #17 | T7 | port | fix(guest): GREEN keep malformed QR links renderable | #122 |
| `265613bb` | 2026-10-13 | #16 | T6 | port | test(self-order): require Swahili and Luganda on the guest page (RED) | #113 |
| `f131242a` | 2026-10-13 | #16 | T6 | port | feat(self-order): translate the guest page into Swahili and Luganda (GREEN) | #113 |
| `482f3857` | 2026-10-13 | #16 | T6 | port | test(self-order): RED bundle catalogs, cart, targets, and guest copy | #113 |
| `84a57535` | 2026-10-13 | #16 | T6 | port | fix(self-order): bundle guest catalogs and keep the cart (GREEN) | #113 |
| `71b49616` | 2026-10-13 | #16 | T6 | port | test(self-order): RED retranslate errors, confirmations, and footer | #113 |
| `f2da9c2a` | 2026-10-13 | #16 | T6 | port | fix(self-order): retranslate guest messages and the footer (GREEN) | #113 |
| `ad5e9aaf` | 2026-10-13 | #16 | T6 | port | test(self-order): RED retranslate the device recovery error | #113 |
| `ed2de277` | 2026-10-13 | #16 | T6 | port | fix(self-order): retranslate the device recovery error (GREEN) | #113 |
| `dfe89af4` | 2026-10-13 | #19 | T2 | port | test: refuse an over-long void reason before any side effect (RED) | #117 |
| `3371d297` | 2026-10-13 | #19 | T2 | port | fix: refuse an over-long void reason before any side effect (GREEN) | #117 |
| `b14b559d` | 2026-10-13 | #18 | T9 | port | test(urypos): RED keep unsent items when View order reopens the same table | #126: The later confirmed-leave contract supersedes the original retention expectation. `view-order-unsent-cart.test.js` covers same/foreign carts and refresh after consent. |
| `cd0ad88a` | 2026-10-13 | #18 | T9 | dropped | fix(urypos): keep unsent items when View order reopens the same table (GREEN) | reverted in 8b6762c0; T9a: Dropped: reverted pair stays out; the final confirmed-leave behavior is carried separately. |
| `b5add614` | 2026-10-13 | #18 | T9 | port | test(urypos): RED reject a foreign cart and refresh same-table assignment | #126: The later confirmed-leave contract supersedes the original retention expectation. `view-order-unsent-cart.test.js` covers same/foreign carts and refresh after consent. |
| `c2ae5e7f` | 2026-10-13 | #18 | T9 | dropped | fix(urypos): keep a cart only when it belongs to that table | reverted in a19965a2; T9a: Dropped: reverted pair stays out; the final confirmed-leave behavior is carried separately. |
| `a19965a2` | 2026-10-13 | #18 | T9 | dropped | Revert "fix(urypos): keep a cart only when it belongs to that table" | revert of c2ae5e7f; T9a: Dropped: reverted pair stays out; the final confirmed-leave behavior is carried separately. |
| `8b6762c0` | 2026-10-13 | #18 | T9 | dropped | Revert "fix(urypos): keep unsent items when View order reopens the same table (GREEN)" | revert of cd0ad88a; T9a: Dropped: reverted pair stays out; the final confirmed-leave behavior is carried separately. |
| `fe859755` | 2026-10-13 | #18 | T9 | port | test(urypos): RED ask before leaving unsent items | #126: `Table.js`: ask before leaving an unsent cart; cancel preserves it and confirm reloads the selected order (`view-order-unsent-cart.test.js`). |
| `30fe2b88` | 2026-10-13 | #18 | T9 | port | fix(urypos): ask before leaving a table with unsent items | #126: `Table.js`: ask before leaving an unsent cart; cancel preserves it and confirm reloads the selected order (`view-order-unsent-cart.test.js`). |
| `f76ec18c` | 2026-10-13 | #18 | T9 | port | test(urypos): RED compare recent-order snapshot and line comments | #126: Compare saved recent-order quantities and line comments when detecting unsent changes (`view-order-unsent-cart.test.js`). |
| `25e6ac3f` | 2026-10-13 | #18 | T9 | port | fix(urypos): compare the saved snapshot and line comments | #126: Compare saved recent-order quantities and line comments when detecting unsent changes (`view-order-unsent-cart.test.js`). |
| `37b581f0` | 2026-10-13 | #18 | T9 | port | test(urypos): RED ignore a stale recent-order snapshot from another table | #126: Only compare a recent-order snapshot belonging to the same table (`view-order-unsent-cart.test.js`). |
| `99d6dd8a` | 2026-10-13 | #18 | T9 | port | fix(urypos): use a recent-order snapshot only for its own table | #126: Only compare a recent-order snapshot belonging to the same table (`view-order-unsent-cart.test.js`). |
| `dee00f6d` | 2026-10-13 | #21 | T6 | port | test: reproduce guest reload and translated refusal regressions (RED) | #113 |
| `6946be3e` | 2026-10-13 | #21 | T6 | port | fix: resume guest QR sessions and show ERP refusal once (GREEN) | #113 |
| `3315ae8c` | 2026-10-13 | #22 | T6 | port | test: reproduce missing staff assistance request cue (RED) | #113 |
| `b9136f23` | 2026-10-13 | #22 | T6 | port | fix: list both staff request types with a neutral waiting count (GREEN) | #113 |
| `a1f01ed7` | 2026-10-13 | #22 | T6 | port | test: prevent old-room request polling during a room switch (RED) | #113 |
| `a681b5d3` | 2026-10-13 | #22 | T6 | port | fix: keep staff request polling within the loaded room (GREEN) | #113 |
| `63dbeca6` | 2026-10-13 | #22 | T6 | port | test: expose unavailable requests when room reads fail (RED) | #113 |
| `f3648b9a` | 2026-10-13 | #22 | T6 | port | fix: report room-read failures in the staff request cue (GREEN) | #113 |
| `6106472e` | 2026-10-13 | #20 | T6 | superseded | test: record who resolved a service request (RED) | #102 native resolution audit; frozen assertions verified in #113. |
| `d8c2ba4f` | 2026-10-13 | #20 | T6 | superseded | fix: record who resolved a service request (GREEN) | #102 native resolution audit; frozen assertions verified in #113. |
| `9cced5a0` | 2026-10-13 | #20 | T6 | superseded | test: fill only missing service-request audit fields (RED) | #102 native resolution audit; frozen assertions verified in #113. |
| `e7dfa673` | 2026-10-13 | #20 | T6 | superseded | fix: fill only missing service-request audit fields (GREEN) | #102 native resolution audit; frozen assertions verified in #113. |
| `c00c1ae2` | 2026-10-13 | #23 | T6 | port | test: reproduce stale guest heading and denied requests cue (RED) | #113 |
| `f90f4eab` | 2026-10-13 | #23 | T6 | port | fix: hide invalid guest context and permission-denied requests (GREEN) | #113 |
| `d35f67c7` | 2026-10-13 | #24 | T7 | port | test(theme): define native Website Theme POS token contracts (RED) | #122 |
| `f08a3913` | 2026-10-13 | #24 | T7 | port | feat(theme): render native Website Theme tokens for POS (GREEN) | #122 |
| `95f4b51b` | 2026-10-13 | #24 | T7 | port | test(theme): cover numeric font words and primary button contrast (RED) | #122 |
| `01f8fbe1` | 2026-10-13 | #24 | T7 | port | fix(theme): encode font identifiers and consume button contrast (GREEN) | #122 |
| `b33a7a67` | 2026-10-13 | #24 | T7 | port | test(theme): preserve numeric font word boundaries (RED) | #122 |
| `1fcaeac5` | 2026-10-13 | #24 | T7 | port | fix(theme): terminate font digit escapes explicitly (GREEN) | #122 |
| `9cb9d296` | 2026-10-13 | #24 | T7 | port | test(theme): cover brand remapping of stock blue utilities (RED) | #122 |
| `8059ec4c` | 2026-10-13 | #24 | T7 | port | fix(theme): map blue utilities to native Website Theme tokens (GREEN) | #122 |
| `82c39434` | 2026-10-13 | #24 | T7 | port | test(theme): anchor brand shades and enforce white-text AA (RED) | #122 |
| `d254d121` | 2026-10-13 | #24 | T7 | port | fix(theme): anchor primary palette on exact brand RGB (GREEN) | #122 |
| `31ac5e6b` | 2026-10-13 | #24 | T7 | port | test(theme): cover gray contrast and body typography (RED) | #122 |
| `9b20db5b` | 2026-10-13 | #24 | T7 | port | fix(theme): keep gray text readable and preserve rem sizing (GREEN) | #122 |
| `99a3229a` | 2026-10-13 | #24 | T7 | port | test(theme): require AA after gray pixel quantization (RED) | #122 |
| `ccd9b11f` | 2026-10-13 | #24 | T7 | port | fix(theme): retain AA margin through pixel rounding (GREEN) | #122 |
| `78405a30` | 2026-10-13 | #26 | T9 | port | test: reproduce checklist UTC midnight mismatch (RED SAL-ACC-01) | #126: Checklist lookup and submission use `frappe.utils.getdate()`; tested at site midnight in `test_shared_till.py`. The separate #26 React logout rows landed in #127. |
| `77aba68c` | 2026-10-13 | #26 | T9 | port | fix: use native site-local checklist dates (GREEN SAL-ACC-01) | #126: Checklist lookup and submission use `frappe.utils.getdate()`; tested at site midnight in `test_shared_till.py`. The separate #26 React logout rows landed in #127. |
| `caa7ad62` | 2026-10-13 | #26 | T9 | port | test: reproduce loss of till return path on logout (RED SAL-ACC-01) | #127; see keyed disposition above. |
| `36e594c9` | 2026-10-13 | #26 | T9 | port | fix: retain native till return path after logout (GREEN SAL-ACC-01) | #127; see keyed disposition above. |
| `1e732d8e` | 2026-10-14 | #27 | T9 | port | test: RED for waiter table booking badges (X55) | #126 + #127; see keyed disposition above. |
| `1bd5e78d` | 2026-10-14 | #27 | T9 | port | feat: show advisory table booking badges to waiters (X55) | #126 + #127; see keyed disposition above. |
| `54756d84` | 2026-10-14 | #28 | T9 | port | test(urypos): require the table owner on the floor plan (RED) | #126: Show the assigned waiter on the occupied floor card, merged partner and refusal; fall back to the readable identifier (`Table.test.js`). |
| `0d516c4d` | 2026-10-14 | #28 | T9 | port | feat(urypos): show the table owner on the floor plan (GREEN) | #126: Show the assigned waiter on the occupied floor card, merged partner and refusal; fall back to the readable identifier (`Table.test.js`). |
| `bdea6e8d` | 2026-10-14 | #28 | T9 | port | test(urypos): require one branch-scoped invoice read per floor refresh (RED) | #126: One branch-scoped open POS Invoice query supplies owner and age for all occupied cards; tested with lookalike table names and multiple tables. |
| `e23681e4` | 2026-10-14 | #28 | T9 | port | fix(urypos): read floor owners from the one branch-scoped invoice query (GREEN) | #126: One branch-scoped open POS Invoice query supplies owner and age for all occupied cards; tested with lookalike table names and multiple tables. |
| `1f735ba9` | 2026-10-14 | #29 | T4 | port | test(U1): RED opt-in floor sign-out profile settings | #118: ported #118 (`ury/ury_pos/test_get_pos_profile_floor_signout.py`) |
| `508eda6d` | 2026-10-14 | #29 | T4 | port | feat(U1): GREEN expose opt-in floor sign-out settings | #118: ported #118 (`getPosProfile` floor flags; 2 Custom Fields) |
| `a053a2e5` | 2026-10-14 | #29 | T4 | port | test(U2): RED shared idle watcher and kiosk wake reset | #118: ported #118 (`pos/tests/idle-watch.test.ts`, plus the kiosk wake case in `self-order/src/hooks/useIdleReset.test.tsx`) |
| `927f01fa` | 2026-10-14 | #29 | T4 | port | feat(U2): GREEN share wall-clock idle watcher | #118: ported #118 (`packages/core/src/idle.ts`, `self-order/src/hooks/useIdleReset.ts`) |
| `fe62a296` | 2026-10-14 | #29 | T4 | dropped | test(U3): RED shared tablet auth and sign-out behavior | #118: dropped (the urypos Vue floor sign-in is not carried; on v16 the floor surface is /pos) |
| `c1a6e5b4` | 2026-10-14 | #29 | T4 | dropped | feat(U3): GREEN shared floor waiter sign-in and sign-out | #118: dropped (urypos; replaced by the /pos floor flow #118) |
| `700c1b61` | 2026-10-14 | #29 | T4 | port | test: RED fail-closed shared tablet logout | #118: ported #118 as the /pos form (`pos/tests/floor-session.test.ts`) |
| `210ce4ad` | 2026-10-14 | #29 | T4 | port | fix: fail closed when shared tablet logout fails | #118: ported #118 as the /pos form (`floorSignOut` in `pos/src/lib/floor-session.ts`) |
| `8d70bb95` | 2026-10-14 | #30 | T3 | port | test(mosaic): RED station KOT printing and after-commit events | #115 |
| `26ebb944` | 2026-10-14 | #30 | T3 | port | feat(mosaic): GREEN print station KOTs through signed QZ jobs | #115 |
| `57cc8271` | 2026-10-14 | #30 | T10 | dropped | docs: add E3 KOT station printing screenshots | docs/evidence or docs/pr-assets only |
| `9f0fba59` | 2026-10-14 | #30 | T3 | port | test: remove trailing station test whitespace | #115 |
| `81e02aff` | 2026-10-14 | #30 | T3 | port | test(mosaic): RED manager settings use session roles | #115 |
| `45a52da9` | 2026-10-14 | #30 | T3 | port | fix(mosaic): GREEN read manager roles from session context | #115 |
| `043888cb` | 2026-10-14 | #32 | T4 | port | test(U6): RED stay-signed-in roles replace billing exemption | #118: ported #118 (fixture and hook assertions; the urypos Auth.test half is dropped) |
| `b309e3ab` | 2026-10-14 | #32 | T4 | port | feat(U6): GREEN stay signed in by POS Profile role list | #118: ported #118 (`custom_floor_stay_signed_in_roles` field; the urypos half is dropped) |
| `62e50c61` | 2026-10-14 | #36 | T4 | port | test: RED SSO stay-signed-in session admission | #118: ported #118 as the /pos form ("keeps a stay-role user signed in without the tab marker") |
| `8fcad741` | 2026-10-14 | #36 | T4 | port | fix: GREEN admit stay-signed-in ERP sessions to URY POS | #118: ported #118 as the /pos form (`isStaySignedIn` cold-boot exemption in AuthGuard) |
| `eff8d965` | 2026-10-14 | #36 | T4 | dropped | refactor: remove unused test capture after GREEN | #118: dropped (urypos test-only refactor) |
| `eb6bb3f6` | 2026-10-14 | #34 | T9 | port | test(floor): RED alert when another waiter's order read is refused | #126: A refused order read names the assigned waiter when known and returns to the floor; other read errors retain their own handling (`Table.test.js`). |
| `9845eb1c` | 2026-10-14 | #34 | T9 | port | fix(floor): GREEN show the assigned waiter when the order read is refused | #126: A refused order read names the assigned waiter when known and returns to the floor; other read errors retain their own handling (`Table.test.js`). |
| `dd99ac67` | 2026-10-14 | #34 | T9 | port | test(floor): RED keep a rejected floor return handled | #126: Handle a rejected return navigation after a refused read; `Table.test.js` checks the alert and handled promise. |
| `c73a28c9` | 2026-10-14 | #34 | T9 | port | fix(floor): GREEN handle a rejected return to the floor | #126: Handle a rejected return navigation after a refused read; `Table.test.js` checks the alert and handled promise. |
| `1718c1b8` | 2026-10-14 | #35 | T9 | port | S6: resolve branch from native default permission without guessing (#35) | #126: `getBranch` and both room APIs use the single assigned branch or the unique assigned native default Branch User Permission. Ambiguous or foreign defaults refuse (`test_branch_resolution.py`). |
| `ccbc60a6` | 2026-10-14 | #37 | T2 | superseded | test(pos): RED allow a zero count when nothing is expected | v16 permits blank closes (`5e794c8a`, #109); blind zero-count is separate. |
| `2c06ba47` | 2026-10-14 | #37 | T2 | superseded | fix(pos): GREEN allow a zero count when nothing is expected | v16 permits blank closes (`5e794c8a`, #109); blind zero-count is separate. |
| `3f833a9a` | 2026-10-14 | #37 | T2 | port | test(pos): RED blind zero count must not reveal expected cash | #109 |
| `ab02784b` | 2026-10-14 | #37 | T2 | port | fix(pos): GREEN submit a blind zero count without reading expected cash | #109 |
| `e9afc4d2` | 2026-10-14 | #31 | T5 | port | test: RED idempotent offline order and settlement writes (O1) | PR #107 |
| `123b03bb` | 2026-10-14 | #31 | T5 | port | fix: make offline order and settlement retries idempotent (O1) | PR #107 |
| `1df2b9cd` | 2026-10-14 | #31 | T10 | dropped | docs: add O1 disposable bench evidence | docs/evidence or docs/pr-assets only |
| `30862bc2` | 2026-10-14 | #31 | T5 | port | test: RED reject mismatched client-order targets (O1 review) | PR #107 |
| `f454372a` | 2026-10-14 | #31 | T5 | port | fix: GREEN bind client-order lookup to requested target (O1 review) | PR #107 |
| `d9456f94` | 2026-10-14 | #31 | T10 | dropped | docs: refresh O1 bench screenshot after review fix | docs/evidence or docs/pr-assets only |
| `37a35774` | 2026-10-14 | #33 | T5 | port | test: RED offline append capture and printed KOT replay | PR #107 |
| `4c9687b9` | 2026-10-14 | #33 | T5 | port | fix: replay offline rounds additively within the cashier shift | PR #107 |
| `37f1b0c9` | 2026-10-14 | #33 | T10 | dropped | docs: add O2 running bench evidence | docs/evidence or docs/pr-assets only |
| `847c9746` | 2026-10-14 | #33 | T5 | port | test: RED preserve occupied gate for append retries | PR #107 |
| `9d29e6c9` | 2026-10-14 | #33 | T5 | port | fix: retain occupied gate for append requests without snapshot | PR #107 |
| `0934894f` | 2026-10-14 | #33 | T5 | port | test: RED preserve saved defaults during offline total comparison | PR #107 |
| `60ddd45e` | 2026-10-14 | #33 | T5 | port | fix: preserve invoice defaults while checking offline totals | PR #107 |
| `04547a40` | 2026-10-14 | #33 | T10 | dropped | docs: refresh O2 bench screenshots and RED GREEN evidence | docs/evidence or docs/pr-assets only |
| `f5e402b7` | 2026-10-14 | #39 | T5 | port | test(O4): RED durable cashier outbox, replay and offline close guards | PR #110; offline-outbox.test.ts ported; offline-cashier.test.tsx in #123 |
| `ae74f6e8` | 2026-10-14 | #39 | T5 | port | feat(O4): GREEN durable offline outbox and replay guards | PR #110 |
| `6f2001d1` | 2026-10-14 | #39 | T5 | port | test(O4): RED cashier capture, print, banner, logout and fixture wiring | PR #107; fixture test in #107, offline-capture.test.ts in #110; offline-banner/-cashier tests in #123 |
| `b856500f` | 2026-10-14 | #39 | T5 | port | test(O4): qualify async cashier harness and native fixture hook keys | PR #107; fixture test in #107, offline-capture.test.ts in #110; cashier harness in #123 |
| `8174af18` | 2026-10-14 | #39 | T5 | port | feat(O4): GREEN wire capture, station printing, banner and native opt-in fixture | #123: POS UI wiring (OrderPanel/Header/OfflineBanner); fixture+filter part ported in #107 |
| `5786d423` | 2026-10-14 | #39 | T5 | port | test(O4): RED queue controls, guarded logout, reprint and refused offline mutations | PR #110; outbox/capture tests ported; offline-banner/-cashier/-refusals tests in #123 |
| `49c87b1b` | 2026-10-14 | #39 | T5 | port | test(O4): qualify browser lock doubles and async DOM flushes | PR #110; offline-locks.ts and capture flushes ported; banner/cashier parts in #123 |
| `0b903763` | 2026-10-14 | #39 | T5 | port | feat(O4): GREEN cashier queue controls, positive rounds and guarded purge | PR #110; engine part; component wiring in #123 |
| `7fab2bab` | 2026-10-14 | #39 | T5 | port | test(O4): RED bounded replay, role revocation and offline action safety | PR #110; offline-outbox bounded replay/role revocation ported; banner/cashier/cancel-order tests in #123 |
| `d36caa1f` | 2026-10-14 | #39 | T5 | port | feat(O4): GREEN bound replay and refuse offline voids, discounts and live settlement | PR #110; engine part; component wiring in #123 |
| `c654d404` | 2026-10-14 | #39 | T5 | dropped | test(O4): qualify legacy module loader and asynchronous empty-outbox guards | node:test logout harness; v16 core logout covered by pos/tests/offline-logout.test.ts |
| `5d0b3aba` | 2026-10-14 | #39 | T5 | superseded | test(O4): RED use native profile walk-in customer for offline capture | v16 resolve_default_customer (ury/ury_pos/default_customer.py, @ury/core/default-customer) |
| `77ac68f8` | 2026-10-14 | #39 | T5 | superseded | test(O4): await durable native walk-in capture acknowledgement | v16 resolve_default_customer |
| `7e9e0e76` | 2026-10-14 | #39 | T5 | superseded | feat(O4): GREEN reuse native profile customer for offline walk-in orders | v16 resolve_default_customer |
| `ae738005` | 2026-10-14 | #39 | T5 | dropped | test(O4): preserve till-read API coverage through the real module loader | fork till-close node:test loader; not part of v16 closing (T2 owns closing) |
| `f4f0f6bb` | 2026-10-14 | #39 | T5 | superseded | revert(O4): remove unqualified walk-in GREEN before restarting RED | v16 resolve_default_customer (revert of a superseded GREEN) |
| `c2f951ca` | 2026-10-14 | #39 | T5 | port | test(O4): RED await cache purge and native walk-in capture | PR #110; offline-capture purge test ported (re-pointed to purgeOfflineSession); walk-in part superseded by v16 resolve_default_customer |
| `ae288fd4` | 2026-10-14 | #39 | T5 | port | feat(O4): GREEN await O3 purge and capture native walk-in orders | PR #110; engine part; component wiring in #123 |
| `a3bfb04e` | 2026-10-15 | #39 | T5 | port | test(O4): RED retain transient authentication failures | PR #110 |
| `432e778e` | 2026-10-15 | #39 | T5 | port | feat(O4): GREEN retain transport authentication failures for retry | PR #110 |
| `5acb4bbb` | 2026-10-15 | #39 | T10 | dropped | docs(O4): capture native v16 offline replay screenshots and TDD evidence | docs/evidence or docs/pr-assets only |
| `86aeb989` | 2026-10-15 | #39 | T10 | dropped | chore(O4): normalize captured evidence output | docs/evidence or docs/pr-assets only |
| `44b1ca95` | 2026-10-14 | #38 | T5 | port | test(O3): RED offline read cache excludes POST and non-allowlisted API routes | PR #110 |
| `ec3d88e9` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN PWA shell and network-first read cache | PR #110 |
| `aab27234` | 2026-10-14 | #38 | T5 | port | test(O3): RED boot registers offline capture from the loaded till flag | PR #110; flag on/off boot tests ported (re-pointed to getPosProfileFull) |
| `e6778762` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN register offline capture from the loaded POS profile | PR #110 |
| `cee1e21b` | 2026-10-14 | #38 | T5 | port | test(O3): RED precache URLs and a stalled worker must not wedge the till | PR #110 |
| `107cd7c3` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN root precache URLs and do not wait on serviceWorker.ready | PR #110 |
| `2e4629e0` | 2026-10-14 | #38 | T5 | port | test(O3): RED offline reload keeps the cached shell session | #123: offline shell auth fallback |
| `eb98df49` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN keep the cached shell when the logged-user read is offline | #123: offline shell auth fallback |
| `ab5acddb` | 2026-10-14 | #38 | T5 | port | test(O3): RED DevTools offline keeps the cached shell session | #123: offline shell auth fallback |
| `af202f83` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN keep the cookie session when the logged-user read has a network error | #123: offline shell auth fallback |
| `47d03127` | 2026-10-14 | #38 | T5 | port | test(O3): RED a network failure on the opening check still shows the shell | #123: offline opening fallback |
| `e40f16d7` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN show the cached shell when the opening check has a network error | #123: offline opening fallback |
| `b034e969` | 2026-10-14 | #38 | T10 | dropped | docs(O3): offline POS shell and service-worker cache evidence | docs/evidence or docs/pr-assets only |
| `6e6a55b3` | 2026-10-14 | #38 | T5 | port | test(O3): RED POS-only worker scope and URY-only purge | PR #110 |
| `79a6d694` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN POS-only worker scope and URY-only purge | PR #110 |
| `220654fe` | 2026-10-14 | #38 | T5 | port | test(O3): RED read allowlist is same-origin with exact parameters at the Workbox router | PR #110 |
| `6907f93d` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN own worker with exact same-origin read routes | PR #110 |
| `ddd0135a` | 2026-10-14 | #38 | T5 | port | test(O3): RED real frappe-js-sdk network failures are told apart from 401/403 | #123: SDK transport tagging |
| `31f5b0fd` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN tag real SDK transport failures; keep 401/403 as HTTP errors | #123: SDK transport tagging |
| `ee9cf865` | 2026-10-14 | #38 | T5 | port | test(O3): RED offline auth fallback only on a real transport failure on an opted-in till | #123: offline auth fallback |
| `ed6617a5` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN offline auth fallback only on a transport failure on an opted-in till | #123: offline auth fallback |
| `4b7ad97c` | 2026-10-14 | #38 | T5 | port | test(O3): RED NetworkFirst never serves cached user data over a pending 401/403 | PR #110 |
| `f39825a9` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN NetworkFirst without timeout; 401/403 purge session caches | PR #110 |
| `36521ff6` | 2026-10-14 | #38 | T5 | port | test(O3): RED sign-out is an awaited, race-safe boundary for cached user data | PR #110; core purge/logout only; Header/Mosaic/frontend logout UI in #123 |
| `440fbd9d` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN race-safe, awaited sign-out for the offline caches | PR #110; core purge/logout only; Header/Mosaic/frontend logout UI in #123 |
| `c5e24f3e` | 2026-10-14 | #38 | T5 | port | test(O3): RED offline opening fallback needs the till flag and a confirmed opening | #123: offline opening fallback |
| `fd3c448a` | 2026-10-14 | #38 | T5 | port | feat(O3): GREEN offline opening fallback gated by the till flag and a confirmed opening | #123: offline opening fallback |
| `6c88a3cc` | 2026-10-15 | #38 | T10 | dropped | docs(O3): refresh offline evidence at fd3c448a | docs/evidence or docs/pr-assets only |
| `0c9f482b` | 2026-10-15 | #39 | T10 | dropped | docs(O4): record final O3 merge verification | docs/evidence or docs/pr-assets only |
| `a1731575` | 2026-10-15 | #40 | T5 | port | test(pos): RED bind the signed-in user before menu and profile reads | #123: bind user before boot reads |
| `85d8e50e` | 2026-10-15 | #40 | T5 | port | fix(pos): GREEN bind the signed-in user before menu and profile reads | #123: bind user before boot reads |
| `2d4055c4` | 2026-10-15 | #40 | T5 | port | test(pos): RED describe the service worker scope as /pos | PR #110 |
| `a67a3c39` | 2026-10-15 | #40 | T5 | port | fix(pos): GREEN describe the service worker scope as /pos | PR #110 |
| `b1e3ad05` | 2026-10-15 | #40 | T5 | port | test(pos): RED reject encoded /pos traversals from the shell cache | PR #110 |
| `ce6f5f67` | 2026-10-15 | #40 | T5 | port | fix(pos): GREEN match only normalized POS cache paths | PR #110 |
| `ac218689` | 2026-10-15 | #40 | T5 | port | test(pos): RED keep the outage id until the offline queue is empty | PR #110 |
| `318d4172` | 2026-10-15 | #40 | T5 | port | fix(pos): GREEN keep the outage id until the offline queue is empty | PR #110 |
| `5e7515fd` | 2026-10-15 | #40 | T10 | dropped | docs(O3b): record bind-before-read and same-outage evidence | docs/evidence or docs/pr-assets only |
| `dfd2166e` | 2026-10-15 | #40 | T5 | port | test(pos): RED serve cached reads only to their owner, the signed-in user | PR #110 |
| `f3defd84` | 2026-10-15 | #40 | T5 | port | fix(pos): GREEN serve cached reads only to their owner, the signed-in user | PR #110 |
| `ffab6774` | 2026-10-15 | #40 | T10 | dropped | docs(O3b): recapture owner-tagged cache evidence | docs/evidence or docs/pr-assets only |
| `280789db` | 2026-10-15 | #40 | T10 | dropped | docs(O3b): recapture labelled Alice/Bob evidence on f3defd84 | docs/evidence or docs/pr-assets only |
| `e47d4f73` | 2026-10-15 | #41 | T6 | port | test: define guest phone payment request contract | #105 |
| `4b23552e` | 2026-10-15 | #41 | T6 | port | test: isolate v16 redirect regression and refresh gateway fixtures | #105 |
| `bdf6afc2` | 2026-10-15 | #41 | T6 | port | test: cover native default gateway and positive outstanding before fix | #105 |
| `2dd642a3` | 2026-10-15 | #41 | T6 | port | fix: populate guest Phone tender and retain JSON payment response | #105 |
| `e6a4bbe5` | 2026-10-15 | #41 | T6 | port | refactor: clarify native phone payment settlement boundary | #105 |
| `a2547fe2` | 2026-10-15 | #41 | T6 | port | test: sort phone regression imports | #105 |
| `96147dbd` | 2026-10-15 | #41 | T10 | dropped | docs: record P2 disposable acceptance evidence | docs/evidence or docs/pr-assets only |
| `cf87424d` | 2026-10-15 | #41 | T6 | port | test: require idempotent guest phone payment retap | #105 |
| `71f7035f` | 2026-10-15 | #41 | T6 | port | fix: return existing guest phone payment request on retap | #105 |
| `d4d4d3ee` | 2026-10-15 | #41 | T10 | dropped | docs: record P2 review-fix evidence | docs/evidence or docs/pr-assets only |
| `bad7b4af` | 2026-10-15 | #41 | T6 | port | test: paid guest re-tap must not reopen checkout; remainder only | #105 |
| `ac006c81` | 2026-10-15 | #41 | T6 | port | test: mocked create tests model no earlier paid requests | #105 |
| `699aa904` | 2026-10-15 | #41 | T6 | port | fix: paid guest re-tap shows Paid; growth requests only the remainder | #105 |
| `142fee29` | 2026-10-15 | #41 | T10 | dropped | docs: record P2 paid re-tap evidence | docs/evidence or docs/pr-assets only |
| `41ef7214` | 2026-10-15 | #42 | T6 | port | test(P2b): reproduce second phone request and stale unpaid checkout (RED) | #105 |
| `9cf57dcf` | 2026-10-15 | #42 | T6 | port | fix(P2b): keep one phone payment per check and send growth to counter (GREEN) | #105 |
| `62da08d3` | 2026-10-15 | #42 | T10 | dropped | docs(P2b): capture guest and cashier settlement proof | docs/evidence or docs/pr-assets only |
| `7dd947f7` | 2026-10-15 | #42 | T10 | dropped | docs(P2b): record independent verification and final readback | docs/evidence or docs/pr-assets only |
| `b238d63b` | 2026-10-15 | #42 | T10 | dropped | docs(P2b): attest disposable bench teardown | docs/evidence or docs/pr-assets only |
| `180e29f0` | 2026-10-15 | #42 | T6 | port | test(P2b): keep queued checkout and cashier requests on growth (RED) | #105 |
| `a2a6d4dc` | 2026-10-15 | #42 | T6 | port | fix(P2b): never cancel a queued checkout or a cashier request (GREEN) | #105 |
| `7feb8c02` | 2026-10-15 | #42 | T6 | port | test(P2b): never cancel a guest request with a failed checkout (RED) | #105 |
| `5ba10e16` | 2026-10-15 | #42 | T6 | port | fix(P2b): never cancel a guest request once checkout started (GREEN) | #105 |
| `e0d5d18e` | 2026-10-15 | #43 | T9 | port | test: cover eligible captain transfer candidates | #126 + #127; see keyed disposition above. |
| `b5e86a30` | 2026-10-15 | #43 | T9 | port | fix: share invoice-scoped captain transfer eligibility | #126 + #127; see keyed disposition above. |
| `8f4ac36b` | 2026-10-15 | #43 | T9 | port | test: refuse self-transfers and gate captain menus (RED) | #126 + #127; see keyed disposition above. |
| `40f8d9f8` | 2026-10-15 | #43 | T9 | port | fix: align captain transfer writes and menu eligibility (GREEN) | #126 + #127; see keyed disposition above. |
| `06f1100f` | 2026-10-15 | #44 | T2 | port | test(pos): expose named-account short-payment refusal (RED) | #109 |
| `2dac7691` | 2026-10-15 | #44 | T2 | port | test(pos): follow the same button through processing | #109 |
| `a6efd481` | 2026-10-15 | #44 | T2 | port | fix(pos): allow short settlement only for opted-in named accounts (GREEN) | #109 |
| `31c3421a` | 2026-10-15 | #44 | T10 | dropped | docs(E5): capture local cashier payment evidence at 1280 and 412 | docs/evidence or docs/pr-assets only |
| `967956a7` | 2026-10-15 | #44 | T2 | port | test(pos): expose native unpaid-account payment-row requirement (RED) | #109 |
| `25c29a9d` | 2026-10-15 | #44 | T2 | port | fix(pos): keep unpaid settlement native-valid with a zero mode row (GREEN) | #109 |
| `7511f64b` | 2026-10-15 | #44 | T2 | port | docs(E5): record native unpaid-row RED and GREEN evidence | #109 |
| `46345746` | 2026-10-15 | #44 | T2 | port | test(E5): remove test lint debt and correct pristine-base evidence | #109 |
| `42ffaf84` | 2026-10-15 | #45 | T5 | port | test(pos): RED gateway HTTP statuses do not fall back or queue | PR #110; offline-gateway and network-first gateway tests ported; getPosInvoice column assertion in #123 |
| `bc796c58` | 2026-10-15 | #45 | T5 | port | fix(pos): GREEN treat gateway statuses as offline for an opted-in till | PR #110; engine part; component wiring in #123 |
| `3930ad21` | 2026-10-15 | #45 | T10 | dropped | docs(pos): show the opted-in till still open behind HTTP 502 | docs/evidence or docs/pr-assets only |
| `ae5626ba` | 2026-10-15 | #45 | T5 | port | test(pos): RED a gateway error must not queue a named-account short settlement | PR #110 |
| `4230f81f` | 2026-10-15 | #45 | T5 | port | fix(pos): GREEN queue only a fully paid cash settlement on a gateway error | PR #110; engine part; component wiring in #123 |
| `1b0b7678` | 2026-10-15 | #46 | T4 | port | test: RED contracts for paired floor PIN sign-in and manager screens | #118: ported #118 (`ury/ury_pos/test_floor_pin.py`; the urypos Login/Auth tests are dropped) |
| `ce7d3394` | 2026-10-15 | #46 | T4 | port | feat: GREEN paired floor PIN sign-in and tablet manager screens | #118: ported #118 (`ury/ury_pos/floor_pin.py`, fixtures, hooks; the urypos screens are replaced by the /pos dialogs) |
| `40855026` | 2026-10-15 | #46 | T4 | port | test: RED native collision retry and durable failed PIN audit | #118: ported #118 |
| `04123044` | 2026-10-15 | #46 | T4 | port | fix: GREEN native collision retry and persistent failed PIN audit | #118: ported #118 (the urypos.py page context is dropped; the /pos page carries it) |
| `32e49342` | 2026-10-15 | #46 | T4 | port | test: RED PIN rotation must not regenerate the previous PIN | #118: ported #118 |
| `6dcae843` | 2026-10-15 | #46 | T4 | port | fix: GREEN ensure reissued PIN invalidates the previous value | #118: ported #118 |
| `6631a1bb` | 2026-10-15 | #46 | T4 | dropped | test: RED PIN sessions keep password change hidden after reload | #118: dropped (urypos-only change-password visibility; the /pos menu has no change-password entry) |
| `97c9c8fd` | 2026-10-15 | #46 | T4 | dropped | fix: GREEN retain non-secret PIN session type across page reload | #118: dropped (urypos) |
| `7e32fa7a` | 2026-10-15 | #47 | T5 | port | test(pos): RED gateway session continuity, one open check, and Add New Order | PR #110; offline-one-open-order ported; POSOpeningProvider/add-new-order/shell-auth parts in #123 |
| `36a386af` | 2026-10-15 | #47 | T5 | port | fix(pos): GREEN keep the till through a gateway outage and one open check per table | PR #110; engine part; component wiring in #123 |
| `9da2dfd6` | 2026-10-15 | #47 | T5 | port | fix(pos): keep Add New Order inside the outlet when the banner wraps | #123: Add New Order layout |
| `c32410e8` | 2026-10-15 | #47 | T10 | dropped | docs(O7): Playwright evidence for gateway session, one open check, and Add New Order | docs/evidence or docs/pr-assets only |
| `3477222d` | 2026-10-15 | #47 | T5 | port | test(pos): RED selecting a table during a gateway outage keeps the till | #123: table read outage in pos-store |
| `d33af7b0` | 2026-10-15 | #47 | T5 | port | fix(pos): GREEN a gateway outage on table selection keeps the till | #123: table read outage in pos-store |
| `fc086b0b` | 2026-10-15 | #47 | T5 | port | test(pos): RED a table read issued before logout must not reach the open-check cache | #123: table read session guard |
| `08d753aa` | 2026-10-15 | #47 | T5 | port | fix(pos): GREEN bind open-check cache writes to the session that issued the read | #123: table read session guard |
| `f35300c3` | 2026-10-15 | #47 | T5 | port | test(pos): RED an occupied-table gateway fallback keeps the round id it sent | PR #110 |
| `810fa301` | 2026-10-15 | #47 | T5 | port | fix(pos): GREEN keep the sent round id when a gateway error hits an occupied table | PR #110 |
| `c850377f` | 2026-10-15 | #47 | T5 | port | test(pos): RED a partly replayed table check keeps its client id across outages | PR #110 |
| `92b4d694` | 2026-10-15 | #47 | T5 | port | fix(pos): GREEN a table's unsettled check keeps its client id across outages | PR #110 |
| `4ab669f7` | 2026-10-15 | #47 | T5 | port | test(pos): RED a stale table read must not change the active till | #123: stale table read guard |
| `97fe1ae0` | 2026-10-15 | #47 | T5 | port | fix(pos): GREEN drop a stale table read before it reaches the active till | #123: stale table read guard |
| `9226e4b8` | 2026-10-15 | #48 | T5 | port | test(pos): RED offline /pos root entry shows Access Denied | #123: /pos root entry offline |
| `5af9e16d` | 2026-10-15 | #48 | T5 | port | fix(pos): GREEN open the till from /pos when the link is down | #123: /pos root entry offline |
| `5bc54852` | 2026-10-15 | #48 | T10 | dropped | docs(O7): Playwright evidence for offline /pos root entry | docs/evidence or docs/pr-assets only |
| `26a630e6` | 2026-10-15 | #49 | T9 | port | fix(pos): GREEN combine online walk-ins with offline customer safeguards | #127; see keyed disposition above. |
| `b6c3330f` | 2026-10-16 | #49 | T9 | port | test(pos): type offline walk-in fixture without explicit any | #127; see keyed disposition above. |
| `f18bd7a6` | 2026-10-16 | #50 | T3 | port | test(U1): reproduce dropped KOT notes and repeat dish variants | #125 |
| `6ea03420` | 2026-10-16 | #50 | T3 | port | fix(U1): preserve KOT item notes and diff repeat dishes by note | #125 |
| `b499a966` | 2026-10-16 | #50 | T3 | port | test(U1): complete legacy no-note invoice fixtures | #125 |
| `423d9388` | 2026-10-16 | #50 | T3 | port | test(U1): RED whole-order void must preserve noted KOT references | #125 |
| `570cf37f` | 2026-10-16 | #50 | T3 | port | fix(U1): GREEN carry item notes through whole-order KOT voids | #125 |
| `a553909a` | 2026-10-16 | #54 | T6 | port | test(U6a): RED guest feedback stays closed until the exact bill is eligible | #108 |
| `972bcc43` | 2026-10-16 | #54 | T6 | port | fix(U6a): GREEN accept one native feedback for the exact bill | #108 |
| `0804b172` | 2026-10-16 | #54 | T6 | port | test(U6a): RED receipt retries must recheck feedback under invoice lock | #108 |
| `c564646f` | 2026-10-16 | #54 | T6 | port | fix(U6a): GREEN serialize feedback and recheck committed retries | #108 |
| `ae34f1bf` | 2026-10-16 | #54 | T6 | port | test(U6a): RED IP and verified credential budgets must be independent | #108 |
| `4788ad8f` | 2026-10-16 | #54 | T6 | port | fix(U6a): GREEN charge independent native IP and verified scope budgets | #108 |
| `8d59fd48` | 2026-10-16 | #54 | T6 | port | test(U6a): RED receipt minting requires native invoice print permission | #108 |
| `eca00609` | 2026-10-16 | #54 | T6 | port | fix(U6a): GREEN check native print authority before minting receipt grant | #108 |
| `8271fc1c` | 2026-10-16 | #54 | T6 | port | test(U6a): RED malformed grant signatures must fail closed | #108 |
| `86813f9b` | 2026-10-16 | #54 | T6 | port | fix(U6a): GREEN reject malformed signatures before secret lookup | #108 |
| `5fe9d91b` | 2026-10-16 | #54 | T6 | port | test(U6a): RED native snapshot conflicts need safe retry | #108 |
| `3d232574` | 2026-10-16 | #54 | T6 | port | fix(U6a): GREEN return safe native transaction conflict retry | #108 |
| `00b79383` | 2026-10-16 | #51 | T1 | port | ci: run hermetic POS tests on hospitality PRs | PR #124 |
| `933a5213` | 2026-10-16 | #51 | T1 | dropped | test(pos): RED proof that POS CI fails when a test fails | CI fail-proof, reverted in 123932e4 |
| `123932e4` | 2026-10-16 | #51 | T1 | dropped | Revert "test(pos): RED proof that POS CI fails when a test fails" | revert of 933a5213 |
| `24f99f84` | 2026-10-16 | #50 | T3 | port | test: expose sent-note removal authorization bypass (RED) | #125; sent-note removal authorization |
| `438fd41e` | 2026-10-16 | #50 | T3 | port | fix: authorize sent dish-note reductions before order mutation | #125 |
| `4b3a198a` | 2026-10-16 | #56 | T6 | dropped | test(G1): RED exact-invoice guest grant denial and lifecycle contracts | reverted by PR #63 (969e2258/042b2a48) |
| `7bc6787d` | 2026-10-16 | #56 | T6 | dropped | feat(G1): GREEN exact-invoice occupancy grants in existing URY sessions | reverted by PR #63 (969e2258/042b2a48) |
| `c41c76a6` | 2026-10-16 | #56 | T6 | dropped | test(G1): RED native commit-window replay and pre-model guest budgets | reverted by PR #63 (969e2258/042b2a48) |
| `0fd978ae` | 2026-10-16 | #56 | T6 | dropped | fix(G1): GREEN native replay window and bounded assistant budgets | reverted by PR #63 (969e2258/042b2a48) |
| `c3753ee2` | 2026-10-16 | #56 | T6 | dropped | test(G1): RED legacy split membership and existing menu bootstrap durability | reverted by PR #63 (969e2258/042b2a48) |
| `0054a3f6` | 2026-10-16 | #56 | T6 | dropped | fix(G1): GREEN preserve native legacy split membership and menu bootstrap | reverted by PR #63 (969e2258/042b2a48) |
| `77de547f` | 2026-10-16 | #53 | T3 | port | test(U3): RED hold/fire kitchen exclusion, ownership and offline controls | #125 |
| `0483b605` | 2026-10-16 | #53 | T3 | port | feat(U3): GREEN native opt-in staff hold and fire | #125 |
| `2b7003d8` | 2026-10-16 | #53 | T3 | port | test(U3): RED preserve held lines across new rounds and cart additions | #125 |
| `b9fa5632` | 2026-10-16 | #53 | T3 | port | test(U3): keep guest framework double representative | #125 |
| `c3c356c8` | 2026-10-16 | #53 | T3 | port | fix(U3): GREEN keep held identities across new rounds and additions | #125 |
| `cff641e2` | 2026-10-16 | #53 | T3 | port | fix(U3): preserve legacy payload shape when hold/fire is off | #125 |
| `446cd162` | 2026-10-16 | #53 | T3 | port | test(U3): RED cancel duplicate sent rows without held quantities | #125 |
| `b9d90790` | 2026-10-16 | #53 | T3 | port | fix(U3): GREEN aggregate only sent quantities for whole-order cancellation | #125 |
| `365a2125` | 2026-10-16 | #53 | T3 | port | refactor(U3): declare pairing invariant and install section before its field | #125 |
| `462bb60d` | 2026-10-16 | #53 | T3 | port | test(U3): capture local dev held and sent UI evidence | #125 |
| `8c44d6fa` | 2026-10-16 | #53 | T3 | port | test(U3): RED refuse settlement with held lines on merged bill | #125 |
| `54b8d997` | 2026-10-16 | #53 | T3 | port | fix(U3): GREEN check held items on both native merged bills | #125 |
| `addac30f` | 2026-10-16 | #53 | T3 | port | test(U3): RED preserve native menu price and tax resolution across locks | #125 |
| `959e3d9d` | 2026-10-16 | #53 | T3 | port | fix(U3): GREEN retain native menu price and tax resolution when locking | #125 |
| `fbee56e9` | 2026-10-16 | #53 | T3 | port | test: expose sent-note removal authorization bypass (RED) | #125; sent-note removal authorization (with U3 projections) |
| `9f28a2e8` | 2026-10-16 | #53 | T3 | port | test(U3): RED sent-only note guards and held-note compatibility | #125 |
| `86aeae75` | 2026-10-16 | #53 | T3 | port | test(U3): RED reject reused client child-row names before mutation | #125 |
| `89f4ea92` | 2026-10-16 | #53 | T3 | port | fix(U3): GREEN validate client child-row names before order mutation | #125 |
| `bc031298` | 2026-10-16 | #54 | T6 | port | test(U6a): restore isolated ordering harness and gate integration | #125 kitchen tests; #124 workflow; this PR (T9c) restores module selection. |
| `40a3fb2f` | 2026-10-16 | #56 | T6 | dropped | test(G1): retain note and held-item regressions through native locking seam | reverted by PR #63 (969e2258/042b2a48) |
| `6b778bce` | 2026-10-16 | #56 | T6 | dropped | test(self-order): RED keep private kitchen notes out of guest DTOs | reverted by PR #63 (969e2258/042b2a48) |
| `f8f30087` | 2026-10-16 | #56 | T6 | dropped | fix(self-order): GREEN omit unprovenanced notes from guest projections | reverted by PR #63 (969e2258/042b2a48) |
| `15727619` | 2026-10-16 | #56 | T6 | dropped | test(self-order): keep kitchen-note caps while verifying guest privacy | reverted by PR #63 (969e2258/042b2a48) |
| `e9c5573b` | 2026-10-16 | #52 | T6 | port | test(self-order): RED guest notes stay separate and add-on prices stay server-side | #108 |
| `f833d401` | 2026-10-16 | #52 | T6 | dropped | fix(self-order): GREEN guest notes and server add-on prices | reverted in 786ec4f0 |
| `786ec4f0` | 2026-10-16 | #52 | T6 | dropped | Revert "fix(self-order): GREEN guest notes and server add-on prices" | revert of f833d401 |
| `3f266735` | 2026-10-16 | #52 | T6 | port | test(self-order): RED executed layout regressions after production revert | #108 |
| `94ac64ea` | 2026-10-16 | #52 | T6 | port | fix(self-order): GREEN safe guest notes and recoverable priced add-ons | #108 |
| `4b7d8145` | 2026-10-16 | #52 | T6 | superseded | test(self-order): RED pending add-on read must not hide the menu | Native lazy ProductDetail reads (`1e66cb79`, #108) remove the eager queue. |
| `6f816e27` | 2026-10-16 | #52 | T6 | superseded | fix(self-order): GREEN show the menu while optional add-ons load | Native lazy ProductDetail reads (`1e66cb79`, #108) remove the eager queue. |
| `85d29512` | 2026-10-16 | #52 | T6 | superseded | test(self-order): RED nine stalled add-on reads must not block the order | Native lazy ProductDetail reads (`1e66cb79`, #108) remove the eager queue. |
| `de1af645` | 2026-10-16 | #52 | T6 | superseded | test(self-order): RED measure each layout on a fresh HTTP/1.1 pool | Native lazy ProductDetail reads (`1e66cb79`, #108) remove the eager queue. |
| `212141e5` | 2026-10-16 | #52 | T6 | superseded | fix(self-order): GREEN load the order before one optional add-on read | Native lazy ProductDetail reads (`1e66cb79`, #108) remove the eager queue. |
| `5b38d63d` | 2026-10-16 | #52 | T10 | dropped | docs(self-order): capture four HTTP/1.1 layouts at 212141e5 | docs/evidence or docs/pr-assets only |
| `2697a0b9` | 2026-10-16 | #52 | T6 | superseded | test(self-order): RED retried add-ons must not starve the order | Native lazy ProductDetail reads (`1e66cb79`, #108) remove the eager queue. |
| `5a9a7799` | 2026-10-16 | #52 | T6 | superseded | fix(self-order): GREEN queue retried add-on reads behind the one in flight | Native lazy ProductDetail reads (`1e66cb79`, #108) remove the eager queue. |
| `38c2a175` | 2026-10-16 | #52 | T10 | dropped | docs(self-order): capture HTTP/1.1 retry layouts at 5a9a7799 | docs/evidence or docs/pr-assets only |
| `e511c029` | 2026-10-16 | #52 | T6 | port | test(self-order): RED run the native TypeScript compatibility check | #108 |
| `c9889682` | 2026-10-16 | #52 | T6 | port | fix(self-order): GREEN initialize idle timer refs for React 19 | #108 |
| `8fc2fd4b` | 2026-10-16 | #52 | T10 | dropped | docs(self-order): refresh retry screenshots after React 19 type fix | docs/evidence or docs/pr-assets only |
| `12c76a08` | 2026-10-19 | #56 | T6 | dropped | test(self-order): RED G1-B reviewer repairs | reverted by PR #63 (969e2258/042b2a48) |
| `bcafb61d` | 2026-10-19 | #56 | T6 | dropped | fix(self-order): GREEN G1-B reviewer repairs | reverted by PR #63 (969e2258/042b2a48) |
| `48a78f9f` | 2026-10-19 | #56 | T6 | dropped | fix(self-order): preserve legacy table guard | reverted by PR #63 (969e2258/042b2a48) |
| `044676f1` | 2026-10-19 | #59 | T6 | dropped | test: add red guest phone grant contracts | reverted by PR #63 (969e2258/042b2a48) |
| `75468e2f` | 2026-10-20 | #59 | T6 | dropped | test: align guest grant assertions | reverted by PR #63 (969e2258/042b2a48) |
| `62199041` | 2026-10-20 | #59 | T6 | dropped | feat: add guest phone QR grant flow | reverted by PR #63 (969e2258/042b2a48) |
| `3586ed5d` | 2026-10-19 | #56 | T6 | dropped | test(self-order): RED flag-off table sessions slide and staff grant refuses | reverted by PR #63 (969e2258/042b2a48) |
| `dfc28b5a` | 2026-10-19 | #56 | T6 | dropped | fix(self-order): GREEN flag-off sessions slide and staff grant refuses | reverted by PR #63 (969e2258/042b2a48) |
| `075e2ced` | 2026-10-19 | #56 | T6 | dropped | test(self-order): RED guest layouts read the bill over POST | reverted by PR #63 (969e2258/042b2a48) |
| `92a6c7c6` | 2026-10-19 | #56 | T6 | dropped | fix(self-order): GREEN guest UI doubles serve bill reads over POST | reverted by PR #63 (969e2258/042b2a48) |
| `2cd66bc8` | 2026-10-19 | #58 | T6 | port | test: add U6b guest feedback RED coverage | #108 |
| `8b1e5fdf` | 2026-10-19 | #58 | T6 | port | feat: add U6b guest feedback form | #108 |
| `922ddc3b` | 2026-10-19 | #58 | T10 | dropped | docs: add U6b feedback screenshots | docs/evidence or docs/pr-assets only |
| `7d5aad36` | 2026-10-19 | #58 | T6 | port | test: RED receipt QR must not break printing | #108 |
| `3ccfe4e9` | 2026-10-19 | #58 | T6 | port | fix: receipt QR blanks on permission or lookup failure | #108 |
| `4444b8e6` | 2026-10-19 | #58 | T6 | port | test: RED no receipt QR without a feedback template | #108 |
| `d96a8d23` | 2026-10-19 | #58 | T6 | port | fix: receipt QR requires a feedback template | #108 |
| `1c67c749` | 2026-10-19 | #58 | T6 | port | test: RED form read and submit share one bill check | #108 |
| `d1e9c533` | 2026-10-19 | #58 | T6 | port | refactor: share one feedback bill check between read and submit | #108 |
| `680fd710` | 2026-10-19 | #58 | T6 | port | test: RED translated guest feedback refusals | #108 |
| `0fef9ee8` | 2026-10-19 | #58 | T6 | port | feat: translated guest feedback refusals | #108 |
| `f424bd4b` | 2026-10-19 | #58 | T10 | dropped | docs: add U6b disabled-feedback screenshot | docs/evidence or docs/pr-assets only |
| `be82918c` | 2026-10-20 | #59 | T6 | dropped | test: align menu-only grant isolation | reverted by PR #63 (969e2258/042b2a48) |
| `c81f8d6c` | 2026-10-20 | #59 | T6 | dropped | test: align feedback mock with post-only guest reads | reverted by PR #63 (969e2258/042b2a48) |
| `dee3e4f7` | 2026-10-20 | #59 | T6 | dropped | docs: add G1E guest phone evidence | reverted by PR #63 (969e2258/042b2a48) |
| `2850446d` | 2026-10-20 | #59 | T6 | dropped | test: restore grant-mode setup in staff-authority test (lost in merge f659d46d) | reverted by PR #63 (969e2258/042b2a48) |
| `1f12a658` | 2026-10-20 | #59 | T6 | dropped | test: RED waiter guest-phone visibility comes from a staff-only server check | reverted by PR #63 (969e2258/042b2a48) |
| `96519802` | 2026-10-20 | #59 | T6 | dropped | fix: GREEN waiter guest-phone visibility from staff-only server check | reverted by PR #63 (969e2258/042b2a48) |
| `447b8dc5` | 2026-10-20 | #59 | T6 | dropped | test: RED granted context refuses flag-off, other-profile and revoked grants without extending | reverted by PR #63 (969e2258/042b2a48) |
| `297b4be8` | 2026-10-20 | #59 | T6 | dropped | fix: GREEN granted context refuses flag-off profiles before sliding the session | reverted by PR #63 (969e2258/042b2a48) |
| `722784ab` | 2026-10-20 | #59 | T6 | dropped | test: restore on-screen proof that a reissued guest-phone QR replaces the old one | reverted by PR #63 (969e2258/042b2a48) |
| `b9ad48bc` | 2026-10-20 | #59 | T6 | dropped | test: RED dead enable_guest_assistant profile flag is removed | reverted by PR #63 (969e2258/042b2a48) |
| `9b876d1d` | 2026-10-20 | #59 | T6 | dropped | fix: GREEN drop dead enable_guest_assistant field (column left inert; modified bumped for re-sync) | reverted by PR #63 (969e2258/042b2a48) |
| `1273f682` | 2026-10-20 | #59 | T10 | dropped | docs: retake guest phone frame with UGX boot; disclose harness-only frames and text | docs/evidence or docs/pr-assets only |
| `5f326fe4` | 2026-10-20 | #60 | T4 | port | test: specify waiter PIN staff list and paired tablet UX | #118: ported #118 (`pin_staff` tests; the urypos Login.test half is dropped) |
| `53718a1f` | 2026-10-20 | #60 | T4 | port | feat: add manager staff PIN list and paired tablet UX | #118: ported #118 (`pin_staff`; the urypos StaffPins.vue is replaced by `pos/src/components/StaffPinsDialog.tsx`) |
| `454bbca8` | 2026-10-20 | #60 | T10 | dropped | docs: add waiter PIN UX screenshots | docs/evidence or docs/pr-assets only |
| `fadb3078` | 2026-10-20 | #61 | T7 | port | test: add red coverage for maximus rebrand S4 S5 | #122 |
| `3255cc51` | 2026-10-20 | #61 | T7 | port | feat: rebrand URY POS and admin surfaces | #122 |
| `fb4944c7` | 2026-10-20 | #61 | T7 | port | test: preserve the waiter role value in labels | #122 |
| `a11a5bb8` | 2026-10-20 | #61 | T7 | port | fix: retain technical waiter role value | #122 |
| `5bbce3e1` | 2026-10-20 | #61 | T10 | dropped | docs: add maximus rebrand screenshot evidence | docs/evidence or docs/pr-assets only |
| `eb64f52d` | 2026-10-20 | #61 | T7 | port | test: RED shared Restaurant POS role labels and labelled save toast | #122 |
| `7321c85f` | 2026-10-20 | #61 | T7 | port | fix: GREEN shared Restaurant POS role labels and labelled save toast | #122 |
| `1281daaa` | 2026-10-20 | #61 | T7 | port | test: RED users table shows Restaurant POS role label | #122 |
| `3e233403` | 2026-10-20 | #61 | T7 | port | fix: GREEN users table shows Restaurant POS role label | #122 |
| `c720f18f` | 2026-10-20 | #61 | T7 | port | test: RED neutral Restaurant wording in remaining dashboard text | #122 |
| `36e203b2` | 2026-10-20 | #61 | T7 | port | fix: GREEN neutral Restaurant wording in remaining dashboard text | #122 |
| `d33bfbe6` | 2026-10-20 | #61 | T7 | port | test: type the Button double instead of any (lint) | #122 |
| `1eece335` | 2026-10-20 | #61 | T10 | dropped | docs: commit screenshot capture harness and full mock disclosure | docs/evidence or docs/pr-assets only |
| `2ecf73c6` | 2026-10-20 | #61 | T10 | dropped | docs: retake rebrand screenshots from dev servers at 1eece335a20824db172750f042163c3ea711788a | docs/evidence or docs/pr-assets only |
| `b6c7e255` | 2026-10-20 | #61 | T7 | port | test(frontend): RED POS profile printer hint uses neutral wording | #122 |
| `d1b89383` | 2026-10-20 | #61 | T7 | port | fix(frontend): neutral POS profile printer hint | #122 |
| `8614e230` | 2026-10-20 | #64 | T3 | port | test: cover split ownership and sibling table release | #125 |
| `c6eaa679` | 2026-10-20 | #64 | T3 | port | fix: enforce split ownership and retain open tables | #125 |
| `f2ac2fcc` | 2026-10-20 | #64 | T3 | port | test: Desk print keeps a split table occupied; KOT double exposes the release helper (RED) | #125 |
| `949e41f6` | 2026-10-20 | #64 | T3 | port | fix: Desk print path keeps a table occupied while a sibling check is open (GREEN) | #125 |
| `04099204` | 2026-10-20 | #63 | T0 | port | test: specify removal of duplicate guest grants | PR #96 (see Theme PRs notes) |
| `969e2258` | 2026-10-20 | #63 | T6 | dropped | Revert "Merge pull request #59 from pmwanje2/codex/salama-g1-guest-phone" | revert of PR #59 (net zero with #59) |
| `042b2a48` | 2026-10-20 | #63 | T6 | dropped | Revert "Merge pull request #56 from pmwanje2/codex/salama-g1-guest-grants" | revert of PR #56 (net zero with #56) |
| `fc2a3970` | 2026-10-20 | #63 | T0 | port | test: support legacy session clock seam | PR #96 (see Theme PRs notes) |
| `27b8448e` | 2026-10-20 | #63 | T10 | dropped | docs: dev-server evidence that the waiter menu no longer offers Guest phone QR | docs/evidence or docs/pr-assets only |
| `8f2b6dee` | 2026-10-20 | #63 | T0 | port | test: restore native guest hardening contracts | PR #96 |
| `abeb1472` | 2026-10-20 | #63 | T0 | port | fix: restore native guest ordering hardening | PR #96 |
| `016938a0` | 2026-10-20 | #63 | T0 | port | test: restore guest POST transport coverage | PR #96 (see Theme PRs notes) |
| `89c7b3b2` | 2026-10-20 | #63 | T0 | port | test: refuse guest sessions on a disabled profile or without expiry (RED) | PR #96 |
| `bd2b5cea` | 2026-10-20 | #63 | T0 | port | fix: end guest sessions when the ordering profile is off or has no expiry (GREEN) | PR #96 |
| `f920af12` | 2026-10-20 | #62 | T4 | port | test: RED one-staff floor tablet session | #118 floor PIN; #127 role-aware captain/cashier landing. |
| `e8d8aae8` | 2026-10-20 | #62 | T4 | port | test: RED paired Guest stays in floor pad | #118: ported #118 (`pos/tests/floor-auth-slice.test.ts`) |
| `e77f750f` | 2026-10-20 | #62 | T4 | port | feat(pos): add paired floor tablet sign-in | #118 floor PIN; #127 role-aware captain/cashier landing. |
| `0af4c9eb` | 2026-10-20 | #62 | T10 | dropped | test: add one staff app visual evidence harness | docs/evidence or docs/pr-assets only |
| `c6e09d95` | 2026-10-20 | #62 | T4 | port | test: preserve provisional cashier profile loading | #118 floor session and #123 provisional cashier; this PR (T9c) isolates provisional paired tablets while preserving unpaired profile loading. |
| `629b960c` | 2026-10-20 | #62 | T4 | port | fix(pos): retain provisional cashier profile loading | #118 floor session and #123 provisional cashier; this PR (T9c) isolates provisional paired tablets while preserving unpaired profile loading. |
| `4cf6a8f6` | 2026-10-20 | #62 | T10 | dropped | test: refresh one staff app visual capture | docs/evidence or docs/pr-assets only |
| `7c153131` | 2026-10-20 | #62 | T4 | port | test: cover one staff floor session boundaries | #118 floor session and #123 provisional cashier; this PR (T9c) isolates provisional paired tablets while preserving unpaired profile loading. |
| `ccb2f26a` | 2026-10-20 | #62 | T4 | port | test: clear failed floor logout marker on sign-in | #118: ported #118 |
| `bfeae471` | 2026-10-20 | #62 | T4 | port | test: tighten floor session and page context receipts | #118: ported #118 |
| `e56a8211` | 2026-10-20 | #62 | T4 | port | fix(pos): isolate failed floor logout and translate switch | #118: ported #118 |
| `98cdf36b` | 2026-10-20 | #62 | T4 | port | test: pin mixed captain cashier home | #127 role-aware home route. |
| `ad6def98` | 2026-10-20 | #62 | T4 | port | test: serialize full floor page context | #118: ported #118 (`ury/www/test_pos_floor_pad.py`) |
| `0b72a2de` | 2026-10-20 | #62 | T4 | port | docs: keep cashier tills unpaired | #118: ported #118 (README) |
| `4882f528` | 2026-10-20 | #62 | T4 | dropped | test: remove unused floor session import | #118: dropped (lint-only change to a fork test re-authored here) |
| `f6a8d7b9` | 2026-10-20 | #62 | T4 | port | test: pin floor logout-failure marker clears on PIN sign-in and successful logout | #118: ported #118 |
| `f36bf7e1` | 2026-10-21 | #66 | T3 | port | test: add RED coverage for waiter split and kitchen alerts | #125 |
| `0c7c0f2d` | 2026-10-21 | #66 | T3 | port | feat: add waiter split checks and kitchen alerts | #125 |
| `19957702` | 2026-10-21 | #66 | T10 | dropped | docs: add waiter split and food-ready evidence | docs/evidence or docs/pr-assets only |
| `09526e26` | 2026-10-21 | #65 | T8 | port | Room charge: pass guest folio reference (kamra_ref) to the POS Invoice (#65) | #114 |
| `00cd6246` | 2026-10-21 | #66 | T10 | dropped | docs: refresh waiter visual evidence | docs/evidence or docs/pr-assets only |
| `eb27fb4a` | 2026-10-21 | #66 | T3 | port | test: cover waiter split reload and notification arrivals | #125 |
| `92293cf1` | 2026-10-21 | #66 | T3 | port | fix: refresh waiter split state and new alerts | #125 |
| `dcb351e2` | 2026-10-21 | #66 | T10 | dropped | docs: recapture waiter evidence with production CSS | docs/evidence or docs/pr-assets only |
| `8663fe14` | 2026-10-21 | #66 | T3 | port | test: pin waiter split reload and invoice selection | #125 |
| `8c3a481f` | 2026-10-21 | #66 | T3 | port | fix: keep original open invoice after waiter split | #125 |
| `040cc660` | 2026-10-21 | #66 | T3 | port | test: pin guest first QR round after a split to the original bill | #125 |
| `be23c53b` | 2026-10-21 | #68 | T8 | port | Room charge: send the cashier's room/folio text as room_number (#68) | #114 |
| `a6923e82` | 2026-10-21 | #67 | T2 | port | test: require configured void reasons before a till void (RED) | #109, #117 |
| `f9a69a47` | 2026-10-21 | #67 | T2 | port | fix: validate configured POS Profile void reasons before voiding (GREEN) | #109, #117 |
| `70944fad` | 2026-10-21 | #67 | T10 | dropped | docs: capture the coded void-reason select dialog | docs/evidence or docs/pr-assets only |
| `7fb8f5f6` | 2026-10-21 | #67 | T2 | port | test: pin Other prefix, field lookup, and void dialog copy (RED) | #109, #117 |
| `615f62e4` | 2026-10-21 | #67 | T2 | port | fix: detect the void-reason field and name it in the refusal (GREEN) | #109 / #117; void-reason CI selection restored in this PR (T9c). |
| `59e5cd67` | 2026-10-21 | #67 | T2 | port | test: load cancel-order reason checks without frappe | #117 |
| `c01a71ef` | 2026-10-21 | #67 | T2 | port | test: require Arabic and French void-reason labels (RED) | #109 |
| `7c8eefd5` | 2026-10-21 | #67 | T2 | port | fix: add Arabic and French void-reason labels (GREEN) | #109 |
| `18334611` | 2026-10-21 | #67 | T2 | port | test: refuse a stale void choice and reset the dialog | #109 |
| `8fd1ed2b` | 2026-10-21 | #70 | T4 | port | test(pos): require floor session parity | #118: ported #118 (`pos/tests/floor-session-parity.test.tsx`, `pos/tests/captain-floor-session.test.tsx`; the node:test `pos-profile-api.test.ts` is converted to a vitest case in the parity spec) |
| `5985d221` | 2026-10-21 | #70 | T4 | port | test: align floor-session parity tests with the floor-session module | #118: ported #118 |
| `2daca831` | 2026-10-21 | #70 | T4 | port | feat(pos): floor session parity on paired floor tablets | #118: ported #118 |
| `806ce2d0` | 2026-10-21 | #70 | T10 | dropped | docs: floor session parity screenshots from a disclosed mock harness | docs/evidence or docs/pr-assets only |
| `bd2c7bd5` | 2026-10-21 | #69 | T4 | port | test: cover manager tablet setup | #118: ported #118 |
| `e02397c5` | 2026-10-21 | #69 | T4 | port | test: stub window.location via Proxy in tablet pairing test | #118: ported #118 |
| `691ce2c5` | 2026-10-21 | #69 | T4 | port | feat(pos): manager Staff PINs and Pair tablet dialogs in the /pos header | #118: ported #118 |
| `236b885c` | 2026-10-21 | #69 | T4 | dropped | test: drop unused call import from manager tablet setup test (lint only) | #118: dropped (lint-only change to a fork test re-authored here) |
| `6c2a1433` | 2026-10-21 | #69 | T10 | dropped | docs: manager tablet setup screenshots from a disclosed mock harness | docs/evidence or docs/pr-assets only |
| `e2d04998` | 2026-10-21 | #69 | T10 | dropped | docs: disclose store seeding and borrowed CSS in the tablet setup harness | docs/evidence or docs/pr-assets only |
| `0de79d44` | 2026-10-22 | #70 | T4 | port | test(pos): after-send sign-out only on a paired floor tablet; translated unsent prompt | #118: ported #118 |
| `f1b4ef2f` | 2026-10-22 | #70 | T4 | port | feat(pos): after-send sign-out only on a paired floor tablet; translated unsent prompt | #118: ported #118 |
| `07bd1b25` | 2026-10-22 | #72 | T2 | port | test(pos): cover till expected cash for card tips | #109, #117 |
| `9c9512ae` | 2026-10-22 | #72 | T2 | port | fix(pos): recompute till expected cash from server data | #109, #117 |
| `b18bd62a` | 2026-10-22 | #72 | T2 | port | fix(pos): restore closing window and change fields | #117 |
| `358d8e65` | 2026-10-22 | #72 | T2 | port | fix(pos): hydrate native change fields for overpayments | #117 |
| `81a530a6` | 2026-10-22 | #72 | T2 | port | fix(pos): preserve closing amounts in isolated validation | #117 |
| `0dfeecc1` | 2026-10-22 | #71 | T2 | port | test(pos): waiter opening gate on floor order routes | #109 |
| `d721c691` | 2026-10-22 | #71 | T2 | port | feat(pos): admit a non-billing waiter when the branch till is open | #109; provider and router pieces superseded by `7452607b`. |
| `829118ad` | 2026-10-22 | #71 | T10 | dropped | docs: waiter opening gate screenshots from a disclosed mock harness | docs/evidence or docs/pr-assets only |
| `a2f057c9` | 2026-10-22 | #71 | T2 | superseded | test(pos): opening gate must return after a router push to /pos/pos | Upstream route-aware opening gate (`7452607b`, #109). |
| `fd220555` | 2026-10-22 | #71 | T2 | superseded | fix(pos): re-check the waiter opening bypass when the route changes | Upstream route-aware opening gate (`7452607b`, #109). |
| `7120316e` | 2026-10-22 | #71 | T10 | dropped | docs: wait for a document body before reading opening-gate captures | docs/evidence or docs/pr-assets only |
| `66cbeb15` | 2026-10-22 | #72 | T2 | port | test(pos): reject empty payment-row queries | #117 |
| `b0da0a21` | 2026-10-22 | #72 | T2 | port | fix(pos): skip empty payment-row lookup | #117 |
| `17c224e4` | 2026-10-22 | #72 | T2 | port | test(pos): preserve multi-cashier closing counts | #117 |
| `3846e534` | 2026-10-22 | #72 | T2 | port | fix(pos): preserve multi-cashier closing field | #117 |
| `864a6d27` | 2026-10-22 | #72 | T2 | port | test(pos): map change accounts to payment modes | #109 |
| `2c925938` | 2026-10-22 | #72 | T2 | port | fix(pos): use server payment account mapping | #109, #117 |
| `4ea4135c` | 2026-10-22 | #72 | T2 | port | test(pos): cover native rounding and invoice fields | #117 |
| `a23765e5` | 2026-10-22 | #72 | T2 | port | fix(pos): use native invoice rounding semantics | #117 |
| `ac63706d` | 2026-10-22 | #72 | T2 | port | test(pos): getPosProfile tolerates a profile without payment rows (Opus r2 N1) | #117 |
| `42684e95` | 2026-10-22 | #72 | T2 | port | fix(pos): payment-mode account map tolerates a profile without payment rows; use native change amount (Opus r2 N1, R1) | #117 |
| `bed02bad` | 2026-10-22 | #72 | T2 | port | chore(pos): drop unused closing totals after the native change rule (lint only, Opus r3 L1) | #117 |
| `dbc36d59` | 2026-10-22 | #73 | T9 | port | test(pos): cover captain walk-in customer resolution | #127; see keyed disposition above. |
| `3936081b` | 2026-10-22 | #73 | T9 | port | fix(pos): allow captain walk-in customer resolution | #127; see keyed disposition above. |
| `82c78d69` | 2026-10-22 | #73 | T9 | port | test(pos): captain leaves walk-in resolution to the server (review r1 F1) | #127; see keyed disposition above. |
| `320267e7` | 2026-10-22 | #73 | T9 | port | fix(pos): captain sends an empty customer for walk-in resolution (review r1 F1/F2) | #127; see keyed disposition above. |
| `57d52834` | 2026-10-23 | #74 | T2 | port | test(pos): preserve closing rows during validation (RED) | #117 |
| `2a3de9e5` | 2026-10-23 | #74 | T2 | port | fix(pos): update closing rows in place (GREEN) | #117 |
| `3ce66ba9` | 2026-10-23 | #74 | T2 | port | test(pos): closing rows keep their order and idx is 1..n (RED, Opus r1 F1/F2) | #117 |
| `3f4a792c` | 2026-10-23 | #74 | T2 | port | fix(pos): closing rows keep their order; missing modes appended; idx 1..n (GREEN) | #117 |
| `18862880` | 2026-10-28 | #75 | T0 | port | enforce native permissions in minimal setup API (#75) | PR #96 |
| `939af8fa` | 2026-11-10 | #77 | T7 | port | test(mosaic): RED kitchen header logo is height-bounded like the POS header | #122 |
| `c78422d9` | 2026-11-10 | #77 | T7 | port | fix(mosaic): GREEN size the kitchen header logo like the POS header | #122 |
| `8d84fa3f` | 2026-11-10 | #78 | T9 | port | test(pos): RED captain order screen fits the viewport | #127; see keyed disposition above. |
| `05bcc006` | 2026-11-10 | #78 | T9 | port | test(pos): RED select viewport panes by their layout comments | #127; see keyed disposition above. |
| `3e7beb8c` | 2026-11-10 | #78 | T9 | port | fix(pos): GREEN captain order screen fits the viewport | #127; see keyed disposition above. |
| `c9e819c4` | 2026-11-12 | #79 | T3 | port | test(hospitality): RED floor-first alerts and board-matched kitchen counts | #115 / #125; Mosaic count covered upstream. |
| `a9944b62` | 2026-10-21 | #79 | T3 | superseded | fix(kds): align overview counts with the unit view time window | same change landed upstream as 84658374 (in version-16) |
| `59c01718` | 2026-11-12 | #79 | T3 | port | fix(pos): GREEN prioritize captain floor and scope kitchen notifications | #125 (captain notifications) |
| `33878a5c` | 2026-11-12 | #79 | T3 | port | test(kds): RED preserve fork order-type filters in overview counts | #115 |
| `de0faa42` | 2026-11-12 | #79 | T3 | port | fix(kds): GREEN reuse station order-type filtering in counts | #115 |
| `c7e345ad` | 2026-11-12 | #80 | T9 | port | test(pos): RED order-type change confirms, then clears cart and releases table | #127; see keyed disposition above. |
| `69c14fe6` | 2026-11-12 | #80 | T9 | port | fix(pos): confirm before an order-type change clears the cart; release the table | #127; see keyed disposition above. |
| `b244590e` | 2026-11-12 | #81 | T9 | port | test(pos): RED dashboard shows no Shift Brief placeholder without a source | #127; see keyed disposition above. |
| `b7210ffd` | 2026-11-12 | #81 | T9 | port | fix(pos): hide the Shift Brief placeholder card until a summary source exists | #127; see keyed disposition above. |
| `01bd8d07` | 2026-11-14 | direct | T9 | port | test: reservation merge keeps our till, floor and permissions (RED) | #126 + #127; see keyed disposition above. |
| `b7ba8a3c` | 2026-11-14 | direct | T9 | port | fix: carry upstream reservations without changing a branch that has not enabled them (GREEN) | #126 + #127; see keyed disposition above. |
| `2fbb6e04` | 2026-11-14 | direct | T2 | port | test: one Custom Field fixture row per field (RED) | #117 |
| `71a1152f` | 2026-11-14 | direct | T2 | superseded | fix: drop the repeated Custom Field fixture rows from ury-erp/ury#358 (GREEN) | version-16 custom_field.json has 215 rows, no repeated names |
| `96637608` | 2026-11-14 | direct | T9 | port | test: reservation writes refuse a branch that has not enabled them (RED) | #126: `test_reservation_merge_safety.py` and native `test_ury_table_reservation.py`: refuse disabled-branch creates, edits, moves and status writes at the API and document boundary. |
| `a31a11b8` | 2026-11-14 | direct | T9 | port | fix: reservation writes refuse a branch that has not enabled them (GREEN) | #126: `test_reservation_merge_safety.py` and native `test_ury_table_reservation.py`: refuse disabled-branch creates, edits, moves and status writes at the API and document boundary. |
| `9df80663` | 2026-11-14 | direct | T1 | port | test: require frozen permissive legacy frontend closures | PR #93 |
| `f6c00c41` | 2026-11-14 | direct | T1 | port | test: require qualified legacy RSA signing dependency | PR #93 |
| `ac6dfa07` | 2026-11-14 | direct | T1 | port | test: reject unused legacy package manager and wrapper dependencies | PR #93 |
| `0d619408` | 2026-11-14 | direct | T1 | port | build: freeze legacy frontend closures and remove unused restricted dependencies | PR #93 |
| `b2ce8b35` | 2026-11-14 | direct | T1 | port | test: require reachable closure runner and strengthen source guards | PR #93 |
| `5af4a169` | 2026-11-14 | direct | T1 | port | build: expose closure checks and document lifecycle qualifications | PR #93 |
| `d4ae531c` | 2026-11-15 | direct | T8 | port | test(pos): room lookup before Room Charge, guest labels, floor PIN menu state (RED) | #114 room/guest label; #118 floor-PIN UI on React. |
| `8ccefdac` | 2026-11-15 | direct | T8 | port | feat(pos): room lookup before Room Charge, guest labels, floor PIN menu state (GREEN) | #114 room/guest label; #118 floor-PIN UI on React. |
| `2c68899e` | 2026-11-15 | direct | T3 | port | test(pos): carry the guest label through kitchen, alerts, prints and split lists (RED) | #114 |
| `b6e72399` | 2026-11-15 | direct | T3 | port | fix(pos): carry the guest label through kitchen, alerts, prints and split lists (GREEN) | #114 |
| `d3b3ee59` | 2026-11-15 | direct | T3 | port | test(pos): RED escape guest label in Jinja print formats and carry on duplicate KOT | #114 |
| `d30fcb94` | 2026-11-15 | direct | T3 | port | fix(pos): escape guest label in Jinja print formats and carry on duplicate KOT | #114 |
| `2a356ba1` | 2026-11-18 | #85 | T6 | port | fix(pos): till menu categories go stale; revalidate every load and fall back to item courses (#85) | #106 |
| `dc75dad1` | 2026-11-18 | #88 | T3 | port | fix(kds): kitchen display window is a POS Profile setting (default 3 hours) (#88) | #115 |
| `7046c0f4` | 2026-11-18 | #87 | T6 | port | fix(pricing): SAP price determination: bill the valid, released Item Price; a menu save keeps price history (#87) | #106 |
| `be286e9c` | 2026-11-19 | #86 | T6 | port | fix(pos): close menu-category test gaps from #85; aggregator menu follows the same rule (#86) | #106 |
| `05691301` | 2026-11-19 | #89 | T6 | port | fix(pricing): one price determination for guests, aggregators and billing; native candidate filter; re-propose a rejected rate (#87 review) (#89) | #106 |
| `688b596b` | 2026-11-19 | #90 | T6 | port | fix(pricing): #89 nits: guest pre-order price follows the invoice's menu, read-only proven, one UOM rule, CI runs the price modules (#90) | #106 pricing; this PR (T9c) restores pricing and kitchen CI selection. |
| `de0c82eb` | 2026-11-19 | #91 | T3 | port | test(mosaic): RED station tickets print to an installed printer by name | #115 |
| `266246da` | 2026-11-19 | #91 | T3 | port | test(mosaic): RED dialog assertion matches Vue SSR option output | #115 |
| `5aff2a90` | 2026-11-19 | #91 | T3 | port | feat(mosaic): GREEN station tickets print to any installed or network printer | #115 |
| `22401182` | 2026-11-20 | #91 | T3 | port | test(mosaic): RED disabled station settings save without a printer target | #115 |
| `b72cb11e` | 2026-11-20 | #91 | T3 | port | fix(mosaic): GREEN disabled station settings save without a printer target | #115 |
| `1c72ca62` | 2026-11-20 | #91 | T3 | port | test(mosaic): cover printer-list failure and per-unit installed printers | #115 |
| `50184832` | 2026-11-20 | #91 | T3 | port | test(mosaic): RED a saved printer missing from the list stays selectable | #115 |
| `483c74dd` | 2026-11-20 | #91 | T3 | port | fix(mosaic): GREEN show a saved printer missing from the list as "saved, not found" | #115 |

## Carrier PRs opened after the freeze (urgent-fix rule)

The 36 non-merge commits in `git rev-list --no-merges 32741738..2e7606f9` are the
post-freeze inventory for `codex/ury-build-closure-20261114` through #119, separate
from the 695 frozen rows. Source names the carrier PR; Note names the v16 landing.

| Fork commit | Date | Source | Theme | Status | Subject | Note |
|---|---|---|---|---|---|---|
| `9cd2f80e` | 2026-11-20 | #94 | T3 | port | test(mosaic): RED station print sends pageWidth so QZ raw HTML is not an 8-dot strip | #115: paper width in dots on the QZ raw HTML element (`station-print.js` and its tests). |
| `6c33c5d5` | 2026-11-20 | #94 | T3 | port | fix(mosaic): GREEN pass station Paper width to QZ as pageWidth | #115: paper width in dots on the QZ raw HTML element (`station-print.js` and its tests). |
| `37b6e9d2` | 2026-11-20 | #97 | T6 | port | test(self-order): RED guest header shows the area and location, never a hard-coded Table | #113: guest header uses the translated room area and location (`be527292`, `f59c0ec2`). |
| `b79a2c29` | 2026-11-20 | #97 | T6 | port | test(self-order): RED the Mobile source pin names the location label, not header.table | #113: guest header uses the translated room area and location (`be527292`, `f59c0ec2`). |
| `0a2dcdc6` | 2026-11-20 | #97 | T6 | port | fix(self-order): GREEN guest header shows area · location from URY Room (Simphony MOP location label) | #113: guest header uses the translated room area and location (`be527292`, `f59c0ec2`). |
| `2742264b` | 2026-11-20 | #98 | T6 | superseded (part) | test(self-order): RED guest menu by category in the menu's configured order | grouping and CategoryTabs are upstream `100c43fe` (`self-order/src/layouts/shared/CategoryTabs.tsx`, `TabletLayout.tsx` 64-81, `MobileQRLayout.tsx`); port only the ordering assertions (`course_priority`, `menu_order`); #113 records the final guest/UI disposition. |
| `9c23ba59` | 2026-11-20 | #98 | T6 | port | test(self-order): RED move the translated-category-label pin to the shared category menu | #113; carries with the T6 guest-language pins |
| `28d5db61` | 2026-11-20 | #98 | T6 | port | test(self-order): RED Mobile category chips stay below the sticky header | #113: MobileQRLayout measures the sticky header and passes `stickyTop={headerHeight}` to CategoryTabs; `guest-menu-categories.test.tsx` checks a 105 px offset. |
| `4d33f2dd` | 2026-11-20 | #98 | T10 | dropped | test(self-order): lint the RED category test (unused destructured names) | test lint only |
| `8bd92913` | 2026-11-20 | #99 | T3 | port | test(mosaic): station tickets ask QZ for double-density ESC/POS images (RED) | #115: Standard station printing uses double-density ESC/POS (`station-print.test.js`). |
| `ddcb74b1` | 2026-11-20 | #98 | T6 | superseded (part) | feat(self-order): GREEN guest menu by category in the menu's configured order | port `resolve_restaurant_menu` `course_priority`/`menu_order` and the sort by `custom_serving_priority` then menu row; version-16 orders categories by first appearance in an item-name-sorted list; #113 records the final guest/UI disposition. |
| `43d2762c` | 2026-11-20 | #99 | T3 | port | fix(mosaic): station tickets print one dot per column (double-density ESC/POS) (GREEN) | #115: Standard station printing uses double-density ESC/POS (`station-print.test.js`). |
| `10a0fd4e` | 2026-11-20 | #100 | T3 | port | test(mosaic): station printer command mode Standard (ESC *) or Raster (GS v 0) (RED) | #115: per-station Standard/Raster choice (`af73f73c`, `e0d50e1b`). |
| `3624525b` | 2026-11-20 | #100 | T3 | port | fix(mosaic): per-station Printer command mode, Raster (GS v 0) for generic ESC/POS printers (GREEN) | #115: per-station Standard/Raster choice (`af73f73c`, `e0d50e1b`). |
| `edd08bd3` | 2026-11-20 | #101 | T6 | superseded (part) | test: call waiter alerts every staff screen until acknowledged (RED) | upstream `100c43fe` has the staff API (`ury/ury/api/service_requests.py`), `pos/src/lib/service-request-api.ts` and the Captain `ServiceRequestPanel`; the alert, realtime, escalation and branch tests are the fork part, carried as #102; #113 records the final guest/UI disposition. |
| `013160d4` | 2026-11-20 | #101 | T6 | port | test: fix the call-waiter RED harness and retire the old dedupe assertion (still RED) | `013160d4` repaired the RED test harness and removed the obsolete deduplication assertion. `756c4923` introduced `request_assistance` with a table-and-invoice lookup and no rate-limit decorator. `c06e5757` made Assistance deduplication per table and rate-limited both guest endpoints (`request_assistance` and `request_bill`); `b4782339` narrowed deduplication to `Open` requests. These behaviors landed on v16 in `f59c0ec2` (#113). |
| `c06e5757` | 2026-11-20 | #101 | T6 | superseded (part) | feat: call waiter alerts every staff screen until acknowledged (GREEN) | Native list/acknowledge/resolve handlers and Captain panel exist at `100c43fe`; `pos/src/lib/service-request-api.ts` is byte-identical. #102 adds branch checks, alerts and realtime delivery. |
| `770bea15` | 2026-11-20 | #103 | T6 | port | test(self-order): RED known guest refusal ids show a translated next step and keep the basket | #113: translated actionable refusal and retained basket (`be527292`, `f59c0ec2`). |
| `6d223c68` | 2026-11-20 | #103 | T6 | port | test(self-order): RED a known refusal follows a language switch; unknown ids keep server text | #113: translated actionable refusal and retained basket (`be527292`, `f59c0ec2`). |
| `68ca8978` | 2026-11-20 | #103 | T6 | port | fix(self-order): GREEN known guest refusal ids show a translated next step; the basket is kept | #113: translated actionable refusal and retained basket (`be527292`, `f59c0ec2`). |
| `e896e428` | 2026-11-20 | #104 | T6 | port | test(guest): RED guest session reads do not write-lock the session row | #116: tests `96577b53` and implementation `1f706e3c` cover the bounded two-column slide, lock failures and app logging. |
| `33e7ecee` | 2026-11-20 | #104 | T6 | port | fix(guest): GREEN guest session reads slide expiry with a two-column update, not a full save | #116: tests `96577b53` and implementation `1f706e3c` cover the bounded two-column slide, lock failures and app logging. |
| `9f5b4e95` | 2026-11-20 | #101 | T6 | port | test: call waiter re-alerts after acknowledge, per-user realtime, audit and history (RED) | #102: re-alert, per-user delivery, audit and history (`1f4ec2ba`, `c1ff16d7`). |
| `b4782339` | 2026-11-20 | #101 | T6 | port | fix: call waiter re-alerts after acknowledge, per-user realtime, audit and history (GREEN) | #102: re-alert, per-user delivery, audit and history (`1f4ec2ba`, `c1ff16d7`). |
| `04ee09c5` | 2026-11-20 | #104 | T6 | port | test(guest): RED a deadlock on the session slide rolls back the whole transaction; short timeouts slide at half | #116: tests `96577b53` and implementation `1f706e3c` cover the bounded two-column slide, lock failures and app logging. |
| `d9a2e6ae` | 2026-11-20 | #104 | T6 | port | fix(guest): GREEN a deadlock on the session slide rolls back the transaction, not a lost savepoint | #116: tests `96577b53` and implementation `1f706e3c` cover the bounded two-column slide, lock failures and app logging. |
| `7091cb1c` | 2026-11-20 | #111 | T6 | port | test(self-order): guest lines carry the Item name, not the item code (RED) | #112: item-name lookup and append-only pricing coverage (`20722c3d`, `a524f4f8`, `b5991ba9`). |
| `b75ed156` | 2026-11-20 | #111 | T6 | port | test(self-order): append-only guest test expects the Item name on the priced line (RED) | #112: item-name lookup and append-only pricing coverage (`20722c3d`, `a524f4f8`, `b5991ba9`). |
| `8484afbd` | 2026-11-20 | #111 | T6 | port | fix(self-order): guest lines carry the Item name, not the item code (GREEN) | #112: item-name lookup and append-only pricing coverage (`20722c3d`, `a524f4f8`, `b5991ba9`). |
| `6b18fee1` | 2026-11-20 | #104 | T6 | port | test(guest): grant-removal slide test matches the two-column session update | #116: tests `96577b53` and implementation `1f706e3c` cover the bounded two-column slide, lock failures and app logging. |
| `e4f4188e` | 2026-11-20 | #104 | T6 | port | test(guest): RED a skipped session slide is logged without an Error Log | #116: tests `96577b53` and implementation `1f706e3c` cover the bounded two-column slide, lock failures and app logging. |
| `8e29a65d` | 2026-11-20 | #104 | T6 | port | fix(guest): GREEN a skipped session slide is logged without an Error Log | #116: tests `96577b53` and implementation `1f706e3c` cover the bounded two-column slide, lock failures and app logging. |
| `cb8b9ea1` | 2026-11-20 | #103 | T6 | port | test(self-order): RED unopened-table refusal names only on-screen actions | #113: translated actionable refusal and retained basket (`be527292`, `f59c0ec2`). |
| `7b1f03e6` | 2026-11-20 | #103 | T6 | port | fix(self-order): GREEN unopened-table refusal asks for staff, names no hidden button | #113: translated actionable refusal and retained basket (`be527292`, `f59c0ec2`). |
| `53c69dfd` | 2026-11-21 | #119 | T6 | port | test(guest): RED the session expiry refusal writes nothing and reads like any unusable token | #120: expiry refuses without a write (`ce7bc8d5`, `861fcc37`). |
| `1eaf6307` | 2026-11-21 | #119 | T6 | port | fix(guest): GREEN the session expiry refusal writes nothing and reads like any unusable token | #120: expiry refuses without a write (`ce7bc8d5`, `861fcc37`). |

Upstream supplies the `URY Service Request` model and staff handlers. The v16 port gates listing by branch and checks the request’s table branch before acknowledgement or resolution. #102 also carries staff banners, escalation, per-user notifications, re-alerting, audit and history. #113 carries the guest request path and guest limits.
