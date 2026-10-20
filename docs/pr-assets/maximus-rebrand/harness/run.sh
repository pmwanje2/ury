#!/usr/bin/env bash
# Recreates docs/pr-assets/maximus-rebrand/*.png from a checkout with
# node_modules installed (npm ci --ignore-scripts). No network or live site.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../../../.." && pwd)
here="$repo/docs/pr-assets/maximus-rebrand/harness"
out="$repo/docs/pr-assets/maximus-rebrand"
vite="$repo/node_modules/.bin/vite"
(cd "$repo/pos" && HARNESS_APP=pos "$vite" --config "$here/vite.config.mjs" --port 5181) & pos=$!
(cd "$repo/frontend" && HARNESS_APP=frontend "$vite" --config "$here/vite.config.mjs" --port 5182) & front=$!
trap 'kill $pos $front 2>/dev/null || true' EXIT
for port in 5181 5182; do
  for _ in $(seq 1 50); do curl -fsS -o /dev/null "http://127.0.0.1:$port/@vite/client" && break; sleep 0.2; done
done
node "$here/capture.mjs" "$out" 5181 5182
