#!/usr/bin/env bash
set -euo pipefail

repo=$(cd "$(dirname "$0")/../../../.." && pwd)
here="$repo/docs/pr-assets/one-staff-app/harness"
out="$repo/docs/pr-assets/one-staff-app"
vite="$repo/node_modules/.bin/vite"

cd "$repo"
"$vite" --config "$here/vite.config.mjs" --port 5186 > /tmp/ury-one-staff-app-vite.log 2>&1 &
vite_pid=$!
trap 'kill "$vite_pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "http://127.0.0.1:5186/@vite/client" 2>/dev/null; then break; fi
  sleep 0.2
done
node "$here/capture.mjs" "$out" 5186
