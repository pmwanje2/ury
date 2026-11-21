# Frozen frontend build closure (version-16)

Ported from the version-2 fork (`docs/frontend-dependency-closure-20261114.md` at
`maximus/v2-final`). Base: upstream `version-16` at `100c43fe`.

Every frontend installs from a committed npm v3 lock:

| Lock | Covers |
|---|---|
| `package-lock.json` | the root workspaces `packages/*`, `pos`, `frontend`, `self-order`, `serve` |
| `urypos/package-lock.json` | the legacy Vue POS (not a workspace) |
| `mosaic/package-lock.json` | the Vue kitchen display (not a workspace) |

`brief_app/` is not part of the root build and keeps its own upstream lock. It is not built.

```sh
npm ci --ignore-scripts --no-audit --no-fund
npm --prefix urypos ci --workspaces=false --ignore-scripts --no-audit --no-fund
npm --prefix mosaic ci --workspaces=false --ignore-scripts --no-audit --no-fund
yarn build
npm run test:closure
```

The root `postinstall` still calls the two legacy install aliases. Those aliases now run
`npm ci` against the legacy locks instead of `yarn install`. Builds that must not run
lifecycle scripts pass `--ignore-scripts` and call the aliases explicitly. The legacy Vite
configs read `../../../sites/common_site_config.json`, so `yarn build` runs inside a normal
bench layout.

## Changes to declared dependencies

- `urypos`: removed `@syncfusion/ej2-vue-calendars` (restricted licence, no consumer) and
  `pnpm` (no consumer). `jsrsasign` is pinned to exactly `11.1.5`, the version the root lock
  selects for the React apps (`^11.1.0`). Version 10 cannot reach the published fixes.
- `mosaic`: removed `flowbite-vue` (no consumer). `urypos` still imports Flowbite Vue's
  `Badge`, so it keeps that dependency.

`tests/qz-signing-compatibility.test.cjs` runs the unchanged QZ RSA-SHA512 callback
(`urypos/src/stores/utils/PrintWithQz.js`, Git blob `ba560454`) against the locked
`jsrsasign` with a generated synthetic key. It never loads the app's private-key module.

## Licences and known advisories

The application's AGPLv3 `LICENSE` is unchanged. `npm audit` on 2026-11-20:

- root lock: 0 advisories;
- `urypos`: 5 (1 moderate `esbuild` dev-server, 4 high via Flowbite Vue 0.0.10's exact pins
  of `lodash-es` 4.17.21 and `nanoid` 4.0.0);
- `mosaic`: 5 moderate (`esbuild` dev-server and `glob`/`minimatch` in build tooling).

No override or forced major upgrade is applied. The `esbuild` advisory needs the dev server,
which production asset builds do not start.
