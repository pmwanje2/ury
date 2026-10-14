# O3b / O4 offline follow-ups

Tracker: [SiteOps #3712](https://github.com/Maximus-Tech-Enterprise-Apps/maximus-siteops/issues/3712).
Public AGPL URY fork only. No new DocType, service, or `import erpnext`.

## Outcome

Four review follow-ups on the landed O3 shell cache and O4 outbox:

1. The signed-in user is bound to the POS worker, and that acknowledgement is
   awaited, before the first menu or profile read. A later user in the same
   window does not receive the previous user's cached POS reads.
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
| Describe the service worker scope as `/pos` | `2d4055c4` | `a67a3c39` |
| Reject encoded `/pos` traversals from the shell cache | `b1e3ad05` | `ce6f5f67` |
| Keep the outage id until the offline queue is empty | `ac218689` | `318d4172` |

Each RED commit is tests only. Each GREEN commit is the minimum production
change and is a separate commit.

## Verification at code SHA 318d41724a90bcfb8b796d0610d44f4f50b1a6de

Captured from this worktree before the evidence commit. Node v24.22.0.
Vitest 3.2.6.

| Command | Actual result |
| --- | --- |
| `npm run test:pos -- --no-cache` | 35 files, **432 passed**, 0 failed |
| `node --test pos/tests/logout.test.ts pos/tests/order-tabs.test.ts pos/tests/till-close-api.test.mjs pos/tests/close-period-server-clock.test.cjs pos/tests/theme.test.cjs` | **36 passed**, 0 failed, 0 skipped |

Items 2 and 3 are covered by those unit tests. Items 1 and 4 also have the
browser captures below.

## Disposable native browser proof

Unique Compose project **`ury-fu-q4n8kd`**, site `ury.localhost`, published only
as `127.0.0.1:18841 -> 8000/tcp`, subnet `10.254.18.0/24`. Image
`frappe/erpnext:v16.50.0`. Synthetic cashier users only:
`offline-d1a6a7e9@example.com` (dish Alice Pepper Soup, profile
`Offline Till d1a6a7e9`) and `offline-2ed3876b@example.com` (dish Bob House
Broth). No production site was used.

The same Chrome profile loaded Alice's till online. The controlling worker was
`/sw.js`, and `ury-pos-reads` held `getRestaurantMenu` for her till, including
Alice Pepper Soup.

Alice online, her dish visible:

![Alice cached menu](alice-cached-menu.png?raw=true)

The session cookie was then switched to Bob with no client cache purge, and the
browser was taken offline before reload. Bob's screen does not show Alice
Pepper Soup. The offline session record is Bob. `ury-pos-reads` no longer
contains Alice's dish; it holds Bob's payment, POS profile, and profile
document reads. The till itself reports that the POS session could not be
opened, which is the offline session-status failure, not Alice's menu.

Bob offline, Alice's dish absent:

![Bob offline does not see Alice menu](bob-offline-no-alice-menu.png?raw=true)

For the outage id, Alice captured one offline order (total 100, one pending).
Network was restored and the in-flight `sync_order` was failed, standing in for
a lost acknowledgement. A second round on that same order was accepted. IndexedDB
kept one pending row: client order `42e23a16-9fd3-488a-b264-8767c81fc3ab`,
outage `8d27e538-5caa-4817-b2d2-045b5c811374`, two rounds, total 200. Two
`sync_order` attempts failed. The screen shows Offline capture, 1 pending,
Total 200, and "Order captured offline." It does not show a different-outage
refusal. Chrome did not grant persistent storage; the banner says so.

Same outage after the lost sync:

![Same outage extra round](same-outage-extra-round.png?raw=true)

Trimmed capture, without cache bodies or credentials:
[browser-proof.json](browser-proof.json).

## Cleanup

After the captures, `docker compose -p ury-fu-q4n8kd -f /tmp/ury-fu-q4n8kd/compose.yaml down --volumes --remove-orphans`
removed that project's containers, `db`/`sites`/`logs` volumes, and network.
Exact-project container, volume, and network queries were empty. The synthetic
database is discarded with those volumes. The host lab directory, including its
Chrome profiles, was moved to the local trash as
`ury-fu-q4n8kd-20261015`; it is not a running bench. No shared Docker
resources were pruned.

CANNOT-VERIFY: production deployment, a physical printer, and a real Cloudflare
or mobile-money provider. Hosted CI is reported on the draft PR, not inferred
from these local runs. This evidence authorizes no merge or deployment.
