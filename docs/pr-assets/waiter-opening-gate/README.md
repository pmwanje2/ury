# Waiter opening gate visual evidence

Run `harness/run.sh` from this checkout. It starts a Vite server over the real
`pos/` source and drives the real `POSOpeningProvider`, `CaptainRouteGuard` and
`CaptainTables` with headless Chrome. The captures are 800x1280 tablet browser
frames, not production screenshots.

| Frame | Evidence |
|---|---|
| [waiter-tables-till-open.png](waiter-tables-till-open.png) | Waiter at `/pos/order` while another cashier's till is open: Tables, not the opening screen |
| [waiter-till-not-open.png](waiter-till-not-open.png) | Same route when captain context says the till is closed: "Till not open" |
| [cashier-gate-kept.png](cashier-gate-kept.png) | Cashier at `/pos/order` with no opening of their own: the existing opening screen |

Each PNG has a `.txt` sibling with `document.body.innerText`.

## Mock disclosure

The harness is a copy of `../floor-session-parity/harness` and reuses
`../waiter-split-food-ready/harness/visual.css`. Synthetic cast only: Amina
Captain (`URY Captain`), Sam Cashier (`URY Cashier`), Pat Cashier as the
occupied-till holder, Kampala POS, table room Dining. `?stage=` on the page
request selects the mock. No live Frappe site, session, database, printer or
deployed endpoint is used. The frames show UI behaviour only, not server
permissions, deployment or ordinary-user acceptance.

`role_allowed_for_billing` in the seeded profile is `[{ role: 'URY Cashier' }]`,
so the waiter is non-billing and the cashier is not. The opening API mock
returns no personal opening entry (`message: 1`) and a non-joinable occupied
till. Captain context `opening_state.pos_open` is true except for the
`till-closed` stage, where it is false.
