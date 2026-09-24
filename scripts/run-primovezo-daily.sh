#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

notify_failure() {
  local detail="$1"
  local python_bin
  python_bin="$(command -v python3 || true)"
  if [[ -z "$python_bin" ]]; then
    echo "Cannot send failure alert: python3 was not found." >&2
    return 0
  fi
  "$python_bin" "$ROOT/scripts/send-primovezo-failure-alert.py" --detail "$detail" || true
}

UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "$UV_BIN" ]]; then
  echo "uv was not found in PATH" >&2
  notify_failure "daily job could not start because uv was not found in PATH"
  exit 1
fi

LOCK_FILE="${HARKEN_DAILY_LOCK:-$ROOT/.primovezo-social-radar.lock}"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "Primovezo Social Radar scan is already running; skipping overlap."
  exit 0
fi

LIMIT="${HARKEN_DAILY_LIMIT:-50}"
MAX_RUNTIME="${HARKEN_DAILY_TIMEOUT:-90m}"

set +e
if command -v timeout >/dev/null 2>&1; then
  timeout --signal=TERM "$MAX_RUNTIME" "$UV_BIN" run harken leads primovezo --limit "$LIMIT"
  status=$?
else
  "$UV_BIN" run harken leads primovezo --limit "$LIMIT"
  status=$?
fi
set -e

if [[ "$status" -ne 0 ]]; then
  if [[ "$status" -eq 124 ]]; then
    detail="daily scan exceeded the maximum runtime of $MAX_RUNTIME"
  else
    detail="daily scan exited with status $status"
  fi
  echo "Primovezo Social Radar failed: $detail" >&2
  notify_failure "$detail"
  exit "$status"
fi
