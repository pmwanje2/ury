# Maximus rebrand evidence

These frames were captured from Vite development servers serving the S4/S5
source at GREEN `3255cc51db4621984257e07087a6ceeab1f3d7a4`, with Chrome
headless at 1440x900:

- [POS loader](pos-loader.png)
- [classic POS dialog](pos-classic-dialog.png)
- [admin dashboard header and tiles](dashboard-brand.png)
- [POS role dropdown](role-dropdown.png)

The temporary harness set `frappe.boot.ury_brand.name` to `Maximus`, supplied a
neutral SVG logo for the dashboard, and mocked Frappe API responses with empty
arrays. No live Frappe site, authentication session, tenant data, or deployed
service was used. These are dev-server visual checks, not production or
ordinary-user acceptance evidence.
