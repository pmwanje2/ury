# O7 round 3 — gateway session, one open check, Add New Order

Playwright drove headless Chrome 150.0.7871.186 against the POS dev server
with a local upstream that answered every API call as an HTML HTTP 502.
This is a local render of the branch, not a live tenant.

## Gateway 502 keeps the opted-in session

The session read returned nginx's HTML 502. The cashier stayed on the till
with the cookie session marked provisional. The opening check got the same
502 and kept the opening confirmed earlier in the tab. The page did not go
to `/login`.

![Gateway 502 session kept](gateway-502-session.png?raw=true)

## One open check per table

Twenty dine-in captures alternated T-01 and T-02. The first check on each
table was settled in cash. Replay used the server's unchanged client-id
guard (a new id on an occupied table is still refused). Result: 4 client
ids, 4 invoices (2 submitted, 2 draft), 20 rounds, nothing left in Needs
review.

![One open check replay](one-open-check-replay.png?raw=true)

## Add New Order at 1280×800 and 412×800

The till was rendered with the offline banner and one pending check, which
is the layout that previously pushed the button below the viewport. The
button's box was fully inside the viewport and its center was the button
itself (not the footer). Playwright then clicked it without forcing the click.

| Viewport | Button box | Click |
| --- | --- | --- |
| 1280×800 | x 913, y 667, 351×44 | received |
| 412×800 | x 45, y 667, 351×44 | received |

![Add New Order at 1280](add-new-order-1280x800.png?raw=true)

![Add New Order at 412](add-new-order-412x800.png?raw=true)
