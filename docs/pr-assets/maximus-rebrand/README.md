# Maximus rebrand evidence (S4/S5)

Captured with [harness/run.sh](harness/run.sh): two Vite dev servers serve the
real `pos/` and `frontend/` source, and headless Chrome drives them through
the DevTools protocol ([harness/capture.mjs](harness/capture.mjs)). The source
revision is in the commit that adds these PNGs. Each PNG has a `.txt` sibling
with the page's `document.body.innerText` at capture time.

| Frame | What runs |
|---|---|
| [pos-loader.png](pos-loader.png) | The real `InitialLoader` component, 1440x900. |
| [pos-classic-dialog.png](pos-classic-dialog.png) | The real `ScreenSizeProvider` at 300px wide, below its 320px gate, so it renders `ScreenSizeDialog`. |
| [dashboard-brand.png](dashboard-brand.png) | The full `/ury/dashboard` route through `frontend/src/main.tsx`, 1440x900. |
| [role-dropdown.png](role-dropdown.png) | The full `/ury/user` route. The harness clicks Add User, then the Role chevron. |

The POS frames mount the two components directly
([harness/pos-entry.tsx](harness/pos-entry.tsx)). The full `/pos` app needs
a POS Profile and an open POS entry before it reaches these screens.

## Every mock value

All mocks are in [harness/mock-data.json](harness/mock-data.json). Vite
middleware ([harness/vite.config.mjs](harness/vite.config.mjs)) answers
`/api/method/*` from it. Nothing else is stubbed.

- `frappe.boot`: `lang` en; `sysdefaults.currency` UGX and `number_format` `#,###.##`;
  `user.roles` URY Manager and System Manager; `user.full_name` "Demo Manager";
  `versions.ury` v3.2.0; `ury_brand` name "Maximus", logo and favicon `/files/maximus-logo.svg`.
- `/files/maximus-logo.svg`: a blue rectangle with the word "Maximus", made for this harness.
- `frappe.auth.get_logged_user`: manager@example.test.
- `get_wizard_status`: step 1 and step 2 complete.
- `get_branches`: one branch, "Kampala Main".
- `get_dashboard_summary`: sales 1,250,000; orders 18; occupied tables 7 of 24;
  average order value 69,444; active cashiers 3; pending kitchen orders 4; menu items 42.
- `get_dashboard_charts`: empty object. `get_recent_transactions`: empty list.
- `frappe.client.get_list` for Notification Log: empty list.
- `frappe.client.get_list` for Role: URY Manager, URY Captain, URY Cashier, URY Admin, System Manager.
  The dropdown shows these values through the shared label map
  (`frontend/src/lib/roleLabels.ts`). System Manager has no mapping, so it shows as-is.
- `get_module_records` for User: Amina (URY Cashier) and Okello (URY Captain), example.test addresses.
- Any other method returns `{"message": null}`.

No live Frappe site, session, tenant data or deployed service was used. These
are dev-server visual checks, not production or ordinary-user acceptance.
