# Floor session parity visual evidence

Run `harness/run.sh` from this checkout. It starts a Vite server over the real
`pos/` source and drives the real `CaptainOrder` screen with headless Chrome.
The captures are 800x1280 tablet browser frames, not production screenshots.

| Frame | Evidence |
|---|---|
| [unsent-items-prompt.png](unsent-items-prompt.png) | The waiter added a dish and tapped Back: the unsent-items prompt |
| [unsent-items-kept.png](unsent-items-kept.png) | After Cancel, the unsent line is still listed under New / Changed |

Each PNG has a `.txt` sibling with `document.body.innerText`.

Idle sign-out and sign-out after send have no visible frame: the screen just
returns to the PIN pad. They are covered by `pos/tests/floor-session-parity.test.tsx`
and `pos/tests/captain-split-bill.test.tsx`.

## Mock disclosure

The harness is a copy of `../waiter-split-food-ready/harness` (same synthetic
waiter, table T-05, two ordered dishes, three split checks) and reuses its
`visual.css`. One mock was added in [mock-data.json](harness/mock-data.json):
`ury.ury_pos.api.getRestaurantMenu` returns the same two synthetic dishes, so the
Menu tab has cards to tap. No live Frappe site, session, database, printer or
deployed endpoint is used. The frames show UI behaviour only, not server
permissions, deployment or ordinary-user acceptance.
