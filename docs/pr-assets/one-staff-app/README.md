# One staff app visual evidence

Run `harness/run.sh` from this checkout. It starts a Vite dev server over the
real `pos/` source and drives it with headless Chrome over the DevTools
protocol. The captures are tablet/phone-sized browser frames, not production
screenshots.

| Frame | Viewport | Evidence |
|---|---:|---|
| [paired-pin-pad-tablet.png](paired-pin-pad-tablet.png) | 800x1280 | Paired tablet PIN entry |
| [wrong-pin-phone.png](wrong-pin-phone.png) | 390x844 | Four-digit refusal and cleared entry |
| [username-fallback-phone.png](username-fallback-phone.png) | 390x844 | In-app username fallback |
| [captain-tables-tablet.png](captain-tables-tablet.png) | 800x1280 | Captain tables, owner badge, Switch waiter |
| [offline-pad-phone.png](offline-pad-phone.png) | 390x844 | Provisional/offline lock state |
| [pad-ar-rtl-phone.png](pad-ar-rtl-phone.png) | 390x844 | English fallback keys in Arabic RTL layout |

Each PNG has a `.txt` sibling containing `document.body.innerText` captured at
the same time.

## Mock disclosure

The harness uses no live Frappe site, tenant, cookie, session, database, or
deployed endpoint. The only mocks are in [mock-data.json](harness/mock-data.json)
and the API middleware in [vite.config.mjs](harness/vite.config.mjs):

- `frappe.boot.lang` is `en` unless `lang=ar`; `frappe.boot.sysdefaults.currency`
  is `UGX`, with `#,###.##`; the boot user is `captain@example.test`, with the
  `URY Captain` role; the brand is the normal URY POS brand.
- `ury.ury_pos.floor_pin.sign_in` always returns the stable refusal code
  `URY_FLOOR_PIN_REFUSED` so the wrong-PIN frame exercises the real refusal
  rendering. No PIN is accepted or persisted.
- `get_captain_context` returns a synthetic Kampala Main / Dining context for
  `captain@example.test`, with table-order access and an open till.
- `frappe.client.get_list` returns two synthetic Dining tables, one free and
  one occupied by `waiter@example.test`, plus that waiter's display name and
  one active draft POS Invoice for the owner badge.
- `ury.ury.api.ury_table_bookings.get_upcoming_bookings` returns an empty map.
- All other mocked API methods return `message: null` and are not used by the
  captured views.

The harness mounts the real `FloorPinPad` and `CaptainTables` components. It
does not mock React components, routing logic, @ury/ui primitives, or
@ury/core client code. The offline frame passes only the real component's
`offline` prop, representing the already-detected provisional cookie state;
it does not simulate a network or claim offline server authority.

`harness/visual.css` is a capture-only style fallback for the Tailwind utility
classes used by these two components. It is not loaded by the production POS
entrypoint and changes no application behavior; it keeps the standalone Vite
fixture readable when the package Tailwind preset is unavailable from the
docs harness root.
