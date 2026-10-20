# Remove duplicate guest-grant layer: visual evidence

Run `harness/run.sh <checkout> <out.png>` against a checkout that has
`node_modules` installed (`npm ci --ignore-scripts`). It stages the harness
in an untracked `.harness-menu/` directory inside that checkout, starts a Vite
dev server over the checkout's real `pos/` source, and captures with headless
Chrome over the DevTools protocol. These are browser frames, not production
screenshots.

| Frame | Source | Menu entries |
|---|---|---|
| [before-base-da4911fc-waiter-menu.png](before-base-da4911fc-waiter-menu.png) | base `da4911fc` | Reprint KOT, Transfer table, Transfer captain, Print bill, **Guest phone QR** |
| [after-head-waiter-menu.png](after-head-waiter-menu.png) | this branch | Reprint KOT, Transfer table, Transfer captain, Print bill |

Each PNG has a `.txt` sibling with `document.body.innerText`.

## Mock disclosure

- The harness mounts the real `CaptainActionsMenu` component (waiter order
  screen overflow menu), opened, with every permission flag set to true. It
  also passes the removed `showGuestPhoneQr` / `onGuestPhoneQr` props to both
  revisions, so the only difference between the frames is the revert.
- No API, Frappe site, tenant, session, cookie or database is used; the
  component makes no request. `window.frappe.boot` is a stub (`lang: en`).
- `harness/visual.css` is a capture-only stand-in for the Tailwind utility
  classes the menu uses, because the app's Tailwind build does not compile from
  the standalone harness root. It is not loaded by any production entrypoint.

