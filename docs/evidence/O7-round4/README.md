# O7 round 4 — offline /pos root entry

Playwright drove headless Chrome 150.0.7871.186 at 1280×800 against the POS
dev server. A local upstream answered every `/api/` call as an HTML HTTP 502.
The till was already opted in: the `/pos` worker controlled the page, the
cookie session and cached opening were present, and one pending capture was
in the outbox. This is a local render of the branch, not a live tenant.

Opening the cached root `/pos` while that link was down landed on `/pos/pos`.
The till navigation stayed visible and the pending controls stayed on the
page. The page did not show Access Denied or "Failed to fetch POS invoices".

![Offline root entry lands on the till](offline-root-entry-1280x800.png?raw=true)
