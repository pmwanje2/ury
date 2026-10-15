# O3b / O4 offline follow-ups

Tracker: [SiteOps #3712](https://github.com/Maximus-Tech-Enterprise-Apps/maximus-siteops/issues/3712).
Public AGPL URY fork only. No new DocType, service, or `import erpnext`.

## Outcome

Four review follow-ups on the landed O3 shell cache and O4 outbox:

1. A later user never receives the previous user's cached POS reads. The
   worker tags every cached read and shell with its owner. It serves a cached
   entry only when that owner is the user in Frappe's `user_id` cookie, and
   stores only once it is bound to that same user. Untagged entries, no
   cookie, or a cookie it cannot read mean nothing is served from cache. This
   does not depend on the page's bind acknowledgement, so a slow purge, a 2 s
   ack timeout, or a lost bind during worker replacement cannot expose another
   user's cache (review P1 on this PR). The page still waits, bounded, for the
   bind before its first reads so those reads can be stored.
2. The `/sw.js` comment in `ury/hooks.py` says scope `"/pos"`, not `"/"`.
3. Shell and read matching uses the normalized path. An encoded
   `/pos%2F..%2F…` traversal is not the shell or read cache. Legitimate `/pos`
   paths still match.
4. Replay deletes the outage id only after the offline queue is empty. A lost
   `sync_order` acknowledgement leaves the id in place, so a later extra round
   stays on that same outage.

## RED/GREEN evidence

| Behaviour | RED | GREEN |
| --- | --- | --- |
| Bind the signed-in user before menu and profile reads | `a1731575` | `85d8e50e` |
| Serve cached reads only to their owner, the signed-in user (fail closed) | `dfd2166e` | `f3defd84` |
| Describe the service worker scope as `/pos` | `2d4055c4` | `a67a3c39` |
| Reject encoded `/pos` traversals from the shell cache | `b1e3ad05` | `ce6f5f67` |
| Keep the outage id until the offline queue is empty | `ac218689` | `318d4172` |

Each RED commit is tests only. Each GREEN commit is the minimum production
change and is a separate commit.

RED `dfd2166e` reproduced both review races with real Workbox 7.4.1 routing:
`pos/tests/offline-session.test.ts` had 5 failed, 6 passed. The failures were
Bob receiving Alice's cached profile while her purge was still pending, a
replacement worker restoring Alice and serving her profile to Bob, a read with
no signed-in cookie served from cache, an untagged entry served, and an entry
tagged for another user served. GREEN `f3defd84`: 11 passed.

## Verification at code SHA f3defd84

Captured from this worktree before the evidence commit. Node v24.22.0.
Vitest 3.2.6.

| Command | Actual result |
| --- | --- |
| `npm run test:pos -- --no-cache` | 35 files, **437 passed**, 0 failed |
| `node --test pos/tests/logout.test.ts pos/tests/order-tabs.test.ts pos/tests/till-close-api.test.mjs pos/tests/close-period-server-clock.test.cjs pos/tests/theme.test.cjs` | **36 passed**, 0 failed, 0 skipped |
| `npx --no-install tsc --noEmit -p pos/tsconfig.app.json` | the same 8 existing diagnostics as the O3/O4 base; none in the changed files |

Items 2 and 3 are covered by those unit tests. Items 1 and 4 also have the
browser captures below.

## Disposable native browser proof

Recaptured on the owner-tag worker (`f3defd84` build) in a fresh unique
Compose project **`ury-fu-8wgwxq`**, site `ury.localhost`, published only as
`127.0.0.1:18842 -> 8000/tcp`, subnet `10.254.19.0/24`. Image
`frappe/erpnext:v16.50.0`. The served `/sw.js` contained the owner header.
Synthetic cashier users only: `offline-5879c281@example.com` (dish Alice
Pepper Soup, profile `Offline Till 5879c281`) and
`offline-1e9d89c7@example.com` (dish Bob House Broth). No production site was
used.

The same Chrome profile loaded Alice's till online. The controlling worker was
`/sw.js`. `ury-pos-reads` held `getRestaurantMenu` for her till, including
Alice Pepper Soup. That entry and the `/pos` shell were tagged with
`X-Ury-Pos-Owner: offline-5879c281@example.com`.

Alice online, her dish visible:

![Alice cached menu](alice-cached-menu.png?raw=true)

With the network off, Alice reloaded and still got her own menu from her own
cache. The owner rule does not break offline reload for the signed-in owner.

![Alice offline sees her own menu](alice-offline-own-menu.png?raw=true)

The session cookie was then switched to Bob with no client cache purge, and the
browser was taken offline before reload. Bob's screen does not show Alice
Pepper Soup. The offline session record is Bob. `ury-pos-reads` no longer
contains Alice's dish. Every remaining read and the shell are tagged for Bob:
his payment, POS profile, and profile document reads. The till itself reports
that the POS session could not be opened, which is the offline session-status
failure, not Alice's menu.

Bob offline, Alice's dish absent:

![Bob offline does not see Alice menu](bob-offline-no-alice-menu.png?raw=true)

For the outage id, Alice captured one offline order (total 100, one pending).
Network was restored and the in-flight `sync_order` was failed, standing in for
a lost acknowledgement. A second round on that same order was accepted. IndexedDB
kept one pending row: client order `fae4d4ef-6a98-41dd-83d0-11f0ac5f12c6`,
outage `475dd9c6-20cd-4913-9a5d-9e8dd1b1af21`, two rounds, total 200. Two
`sync_order` attempts failed. The screen shows Offline capture, 1 pending,
Total 200, and "Order captured offline." It does not show a different-outage
refusal. Chrome did not grant persistent storage; the banner says so.

Same outage after the lost sync:

![Same outage extra round](same-outage-extra-round.png?raw=true)

Trimmed capture, without cache bodies or credentials:
[browser-proof.json](browser-proof.json).

## Cleanup

After each capture, `docker compose -p <project> -f <lab>/compose.yaml down --volumes --remove-orphans`
removed that project's containers, `db`/`sites`/`logs` volumes, and network,
for the first capture `ury-fu-q4n8kd` and the recapture `ury-fu-8wgwxq`.
Exact-project container, volume, and network queries were empty. The synthetic
databases are discarded with those volumes. Each host lab directory, including
its Chrome profiles, was moved to the local trash as
`ury-fu-q4n8kd-20261015` and `ury-fu-8wgwxq-20261015`. Neither is a running
bench. No shared Docker resources were pruned.

Browser note: the worker reads the `user_id` cookie through the Cookie Store
API (Chrome 87+, the tills' browser). Where that API is missing, the worker
serves nothing from cache, so offline reload is unavailable but isolation
holds.

CANNOT-VERIFY: production deployment, a physical printer, and a real Cloudflare
or mobile-money provider. Hosted CI is reported on the draft PR, not inferred
from these local runs. This evidence authorizes no merge or deployment.
