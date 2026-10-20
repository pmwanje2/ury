#!/usr/bin/env bash
# Capture the real Captain (waiter) actions menu from a pos/ checkout.
# Usage: run.sh <checkout-with-node_modules> <out.png> [port]
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
checkout=$(cd "$1" && pwd)
out=$2
port=${3:-5187}

# vite.config.mjs imports vite/react from the target checkout, so stage the
# harness inside it (a throwaway, untracked .harness-menu directory).
stage="$checkout/.harness-menu"
mkdir -p "$stage"
cp "$here"/menu-entry.tsx "$here"/menu.html "$here"/vite.config.mjs "$here"/visual.css "$stage"/
HARNESS_REPO="$checkout" "$checkout/node_modules/.bin/vite" --config "$stage/vite.config.mjs" --port "$port" > /tmp/ury-menu-harness-vite.log 2>&1 &
vite_pid=$!
trap 'kill "$vite_pid" 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "http://127.0.0.1:$port/@vite/client" 2>/dev/null; then break; fi
  sleep 0.2
done
node "$here/capture.mjs" "$out" "$port"
