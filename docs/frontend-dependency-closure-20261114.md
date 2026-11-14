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

## Closure checks and lifecycle scripts

After the three frozen installs, run `npm run test:closure`. This entrypoint runs
the Python source contracts and the real QZ callback controls. The latter use only
synthetically generated keys, covering both PKCS#8 and PKCS#1 PEM encodings. The
real private-key module is not inspected; its format is not asserted.

There is no tracked or on-disk `urypos/yarn.lock` or `mosaic/yarn.lock` at either
the exact a31 base or this carrier. The source contracts reject a stale legacy
Yarn lock and require exact `https://registry.npmjs.org/` resolution hosts. The
unused-dependency scans include both legacy `index.html` entries as well as
source, CSS, scripts and build configuration, without reading private-key modules.

The complete legacy `hasInstallScript: true` list is:

| Closure | Package | Selected version | Why `--ignore-scripts` works on the qualified target |
| --- | --- | --- | --- |
| urypos | esbuild | 0.18.20 | npm installs the matching integrity-locked optional `@esbuild/linux-x64` binary; real native transform and all five builds succeed without its installer/fallback download. |
| urypos | vue-demi | 0.14.10 | The published CommonJS/ESM default is already Vue 3 (`isVue3=true`, `isVue2=false`); the selected Vue is 3.x and the existing Pinia/Vue suites pass. |
| urypos | fsevents | 2.3.3 | Optional Darwin-only dependency, not installed or used on the qualified Linux target. |
| mosaic | esbuild | 0.18.20 | Same locked platform-binary mechanism and actual native transform/build proof. |
| mosaic | fsevents | 2.3.3 | Optional Darwin-only dependency, not installed or used on Linux. |

This qualifies Linux x64, not every architecture. Other architectures must prove
their matching optional esbuild binary and rerun native builds. If the binary is
missing, suppressing its installer does not authorize a download fallback or a
successful build claim. The supported consumer uses the three frozen npm installs
and one unchanged native build chain; it does not run a floating root Yarn install.

The two entries without `license` metadata in each legacy lock are the root
application entry (covered by the unchanged repository AGPLv3 LICENSE) and
`xmlhttprequest-ssl@2.1.2` (the published installed LICENSE contains the MIT grant).
They are identified rather than silently treated as unknown or MIT application
relicensing. The exact grant hashes remain in the qualification receipt.

## Historical failure diagnostics

All six changed-closure-scope failure IDs have exact first errors and stacks in
the review receipt. Four logout assertions expect `/urypos/login`, while the
unchanged application source redirects to `/urypos`; all four reproduce with
`urypos/node_modules` physically moved aside and restored in `finally`. Two Header
branding cases fail because the existing test's dependency map does not provide
`@/stores/invoiceData.js`: its harness returns `{}` for that unknown alias and the
real setup calls the missing `useInvoiceDataStore` function. That occurs before a
branding assertion. Without the entire legacy directory, the branding test cannot
load Vue at all; this separate harness limitation is retained, not relabelled as
six identical failures or as old-dependency qualification. No historical test,
application handler or assertion was altered.
