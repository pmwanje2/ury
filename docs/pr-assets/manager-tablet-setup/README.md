# Manager tablet setup visual evidence

Run `harness/run.sh` from this checkout. It starts a Vite server over the
real `pos/` source and drives the real `Header`, `StaffPinsDialog` and
`PairTabletDialog` with headless Chrome over the DevTools protocol. The
captures are 1024x768 browser frames, not production screenshots.

| Frame | Evidence |
|---|---|
| [manager-menu.png](manager-menu.png) | /pos user menu for a manager: Pair this tablet and Staff PINs |
| [staff-pins-list.png](staff-pins-list.png) | Staff PINs list, with "Needs PIN" for staff without one |
| [staff-pin-issued.png](staff-pin-issued.png) | A newly issued PIN, shown once |
| [pair-tablet.png](pair-tablet.png) | Pair this tablet dialog |

Each PNG has a `.txt` sibling with `document.body.innerText` captured at the
same moment.

## Mock disclosure

No live Frappe site, tenant, session, database or deployed endpoint is used.
The only mocks are [mock-data.json](harness/mock-data.json) and the API
middleware in [vite.config.mjs](harness/vite.config.mjs):

- `frappe.client.has_permission` returns `true` for the synthetic manager,
  which is what shows the two menu entries. The real gate is the server's
  POS Profile write permission.
- `ury.ury_pos.floor_pin.pin_staff` returns three synthetic staff;
  `issue_pin` returns the fixed PIN `4821`; `pair_device` returns a label.
- The brand name and logo in the header come from the mocked boot. Live sites
  take them from Website Settings.

These captures show UI composition and interaction only. They do not prove
server permissions, PIN hashing, deployment or ordinary-user acceptance.
