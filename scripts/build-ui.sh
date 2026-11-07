#!/usr/bin/env sh
# Build the React UI with Node in Docker (nothing to install on the host). Output: src/inbox_to_order/web/dist
# Usage: scripts/build-ui.sh            (install from package-lock.json, type-check, build)
set -eu
ROOT="$(cd "$(dirname "$0")/.." && (pwd -W 2>/dev/null || pwd))"
export MSYS_NO_PATHCONV=1  # Git Bash on Windows: keep container paths as written
docker run --rm \
  -v "$ROOT:/work" \
  -v inbox_to_order_node_modules:/work/frontend/node_modules \
  -w /work/frontend \
  node:22-alpine \
  sh -c 'if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi && npm run build'
