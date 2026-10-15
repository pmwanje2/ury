# Gateway 502 — opted-in till stays usable

Playwright (Chrome 150.0.7871.186) loaded the built `/pos` till for an
opted-in cashier. The service worker cached the menu and table reads, then
the local proxy returned HTTP 502 for those same-origin allowlisted GETs
(`/pos`, `getRestaurantMenu`, `URY Table`). The till still showed the cached
menu instead of "Failed to load POS" / "Failed to load menu items".

`proxy-502.log` is the proxy's down-phase request log. Non-allowlisted reads
(logged-in user, opening entry, checklist) stayed HTTP 200 so the till could
boot; the menu and table GETs in that log were answered 502.

This is a local built-POS proof with a mock ERP, not a live tenant.
