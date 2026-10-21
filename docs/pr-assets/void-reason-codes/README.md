# Coded void-reason dialog

Run `harness/run.sh` from this checkout. It starts Vite from `pos/` (so the
real Tailwind config applies) over the real `pos/` source and drives the real
Orders page with headless Chrome over the DevTools protocol. The capture is a browser frame, not a production
screenshot and not a live site.

| Frame | Viewport | Evidence |
|---|---:|---|
| [void-dialog-select.png](void-dialog-select.png) | 1280x800 | Cancel dialog with the configured-reason Select |

`void-dialog-select.txt` is `document.body.innerText` at the same moment, plus
the option labels read from that same `<select>`. A closed native select does
not paint its options into the page text.

## Mock disclosure

The harness uses no live Frappe site, tenant, cookie, session, database, or
deployed endpoint. The only mocks are in [mock-data.json](harness/mock-data.json)
and the API middleware in [vite.config.mjs](harness/vite.config.mjs):

- `frappe.boot` is English, currency UGX, user `cashier@example.test`.
- `ury.ury_pos.api.getPosProfile` returns the limited till fields for a
  synthetic Kampala POS. It does not include the void-reason list.
- `GET /api/resource/POS Profile/Kampala POS` returns the full POS Profile
  document, including `custom_void_reasons` (one reason per line). This is the
  same `db.getDoc` read the till already uses.
- `getPosInvoice` returns one synthetic draft, `POS-INV-1001`.
- `getPosInvoiceItems` returns one Soup row. `get_split_group` is empty.
- `frappe.client.has_permission` returns cancel allowed so the real cancel
  button renders. `getBranch` returns Kampala Main. Other list calls are empty.
- Currency `UGX` returns a synthetic symbol if the profile fetch asks.

The harness mounts the real `Orders` page after the real `fetchPosProfile`
call. It does not mock React components, the dialog, the Select, routing, or
@ury/core. The reason list is the shared hospitality seed, not a Salama-only
code path.
