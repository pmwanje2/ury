# Waiter split and food-ready visual evidence

Run `harness/run.sh` from this checkout. It starts a Vite server over the
real `pos/` source and drives the screens with headless Chrome over the
DevTools protocol. The captures are tablet-sized browser frames, not
production screenshots.

| Frame | Evidence |
|---|---|
| [waiter-menu-split-bill.png](waiter-menu-split-bill.png) | Waiter overflow menu with the permission-gated Split bill action |
| [waiter-split-dialog.png](waiter-split-dialog.png) | Existing bill split dialog on the waiter screen |
| [waiter-sibling-checks.png](waiter-sibling-checks.png) | Existing related-check panel with one Print button per sibling |
| [waiter-kitchen-alert.png](waiter-kitchen-alert.png) | Captain Tables Kitchen panel, unread food-ready row, and the new-food-ready toast |

Each PNG has a `.txt` sibling containing `document.body.innerText` captured at
the same time.

## Mock disclosure

The harness uses no live Frappe site, tenant, session, database,
printer, socket server, or deployed endpoint. The only mocks are in
[mock-data.json](harness/mock-data.json), the API middleware in
[vite.config.mjs](harness/vite.config.mjs), and the capture-only notification
bump endpoint:

- `frappe.boot` identifies `waiter@example.test` as a synthetic URY Captain;
  the mocked context gives that waiter table `T-05`, split permission, and an
  open draft invoice with two items.
- The entrypoint sets a synthetic browser `user_id` cookie for that same mock
  identity so the real session-scoped Notifications component can exercise its
  recipient guard. It is not a live authentication cookie.
- The mocked order and split-group calls supply `POS-INV-1001` and sibling
  `POS-INV-1002`. The real `CaptainOrder`, `CaptainActionsMenu`,
  `BillSplitDialog`, `SplitGroupPanel`, and `printOrder` code are mounted; no
  application components are mocked.
- `frappe.client.get_list` returns the synthetic Notification Log rows only
  for `waiter@example.test`. The initial row is read; the local
  `/harness/notification-bump` endpoint changes the mock to one unread row,
  and the capture script dispatches the browser focus refresh used by the
  existing notification component. This makes the actual `onNewUnread`
  callback render the real toast without pretending to have production
  realtime transport.
- The KOT lookup returns `restaurant_table: T-05`; all other unlisted methods
  return an empty `message`.

These captures demonstrate source-level UI composition and interaction only.
They do not prove server permissions, notification recipient assignment,
printing hardware, deployment, or ordinary-user acceptance.
