#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "$UV_BIN" ]]; then
  echo "uv was not found in PATH" >&2
  exit 1
fi

LOCK_FILE="${HARKEN_DAILY_LOCK:-$ROOT/.primovezo-social-radar.lock}"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Primovezo Social Radar scan is already running; skipping overlap."
  exit 0
fi

LIMIT="${HARKEN_DAILY_LIMIT:-50}"

exec "$UV_BIN" run harken leads primovezo --limit "$LIMIT"
