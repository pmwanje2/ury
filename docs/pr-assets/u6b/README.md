# U6b guest feedback screenshots

Captured 2026-10-19 from the real Vite dev server using headless Google Chrome
at a 412x800 viewport. The reverse proxy used for this capture mocked only the
Frappe `get_feedback_form` and `submit_feedback` responses; no production
host, site, account, or ERP service was used. These are frontend evidence, not
live ERP acceptance.

- `form-enabled.png` — open form with manager-supplied labels.
- `submitted-thanks.png` — ratings submitted and thanks state.
- `already-submitted.png` — received/already-submitted state.
- `disabled-expired.png` — invalid/expired link refusal.
