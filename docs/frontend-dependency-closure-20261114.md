# Frozen frontend build closure

This carrier starts at `a31a11b8268d7c81735b71147451caad5664244a`.
It changes packaging metadata and adds source contract tests. Application Python,
reservation logic, frontend source, the five build scripts, and the modern root
`package-lock.json` remain unchanged.

The legacy `urypos` and `mosaic` projects have separate npm v3 locks. Install the
modern workspace from its existing root lock, then install each legacy project
with its own lock and `--workspaces=false`. The two root install aliases perform
those frozen legacy installs. The existing root build chain still runs all five
native frontend builds using Yarn Classic.

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm --prefix urypos ci --workspaces=false --ignore-scripts --no-audit --no-fund
npm --prefix mosaic ci --workspaces=false --ignore-scripts --no-audit --no-fund
npm run build
```

Run builds within a normal bench directory layout: the unchanged legacy Vite
configs load `../../../sites/common_site_config.json`. Qualification uses only a
task-owned synthetic port-only config, not a tenant config.

Removed declarations have no application, CSS, Tailwind, build configuration or
script consumers: the restricted Syncfusion calendar, the unused legacy pnpm
package manager, and Mosaic's unused Flowbite Vue wrapper. URY POS still uses
Flowbite Vue's Badge component, so that dependency remains. Its date-picker
components still use the existing `vue-datepicker-next` implementation.

The legacy `jsrsasign` declaration selects `11.1.3`, already selected by the
unchanged modern root lock. Version 10 cannot select the published security fixes.
The actual unchanged QZ RSA-SHA512 callback is tested with a generated synthetic
RSA key against independent Node crypto verification, deterministic PKCS1-v1.5
signing and tampered-message/signature rejection. The test never loads the app's
private-key module or printer transport. DSA and RSA-decryption advisories are
not evidence of exploitation in this RSA-signing call path.

## Licences and remaining security scope

The application's existing GNU AGPLv3 LICENSE remains intact. No application MIT
relicensing is asserted. jsrsasign 11.1.3 retains its MIT grant; QZ retains its
LGPL-2.1 grant. Dependency licences and corresponding source must remain in the
normal source offer. Removing the unused calendar does not assert or manufacture
commercial entitlement.

This change does not assert a vulnerability-free dependency graph. Qualification
retains the npm audit results, including pre-existing findings in the byte-identical
modern root lock. Legacy Vite/esbuild advisories require their development server
or middleware conditions; production asset builds do not start those servers.
The retained Flowbite Vue 0.0.10 pins lodash-es exactly 4.17.21 and nanoid exactly
4.0.0. Published fixes cannot satisfy those exact upstream declarations, so no
silent override or major UI/tool migration is applied. Those runtime dependency
advisories remain open; importing Badge alone is not proof that every affected
helper is reachable or unreachable.

The native POS, legacy Vue, dashboard and self-order Vitest suites and site-less
Python workflow suite are qualified without weakening assertions. Additional
historical Node tests retain baseline failures reproduced against exact a31
source with the same held installed dependencies and built assets. This is a
source-only comparison, not an authentic old-dependency or live-tenant result.
Ad-hoc plain-Node TSX/resolution failures are recorded as harness limitations.
No framework/database, live printer, customer or production test is claimed.
