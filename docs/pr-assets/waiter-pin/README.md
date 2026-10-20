# Waiter PIN UX screenshots

Captured 2026-10-20 from the `codex/salama-waiter-pin-ux` worktree at `53718a1f`.

| Screenshot | Scenario |
| --- | --- |
| [paired-tablet-pin-pad.png](paired-tablet-pin-pad.png) | Paired tablet with an open till shows the four-digit PIN pad. |
| [wrong-pin-message.png](wrong-pin-message.png) | A refused PIN shows `That PIN didn't work. Ask a manager for a new PIN.` |
| [paired-tablet-username-fallback.png](paired-tablet-username-fallback.png) | Paired tablet username fallback has no `Forgot password?` link. |
| [staff-pins-needs-pin.png](staff-pins-needs-pin.png) | Manager Staff PINs view shows full names, `New PIN`, `Give PIN`, and `Needs PIN`. |

## Capture boundary

These are source/dev-server screenshots only. The URY Vite app ran locally at
`http://127.0.0.1:4174` with headless Google Chrome 150 at a 1024x768 tablet
viewport. The checked-in Vite config could not run standalone because this fork
expects a Bench `sites/common_site_config.json`; capture used a temporary `/tmp`
Vite config pointing at the same `urypos` source and Vue plugin.

Playwright intercepted every `/api/**` request. No Frappe site, ERPNext site,
customer account, production service, or network API was contacted. The mocked
responses were:

- login views: no logged-in user and `floor_pad` set to paired/open;
- wrong PIN: `floor_pin.sign_in` returned an HTTP 417 refusal;
- Staff PINs: native permission returned true and `floor_pin.pin_staff`
  returned `Amina Waiter` (`has_pin: true`) and `Brian Waiter` (`has_pin: false`);
- authenticated shell support calls returned deterministic Salama POS metadata.
