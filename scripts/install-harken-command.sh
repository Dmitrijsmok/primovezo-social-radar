#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UV_BIN="${UV_BIN:-$(command -v uv || true)}"
if [[ -z "$UV_BIN" ]]; then
  echo "uv was not found in PATH" >&2
  exit 1
fi

BIN_DIR="$HOME/.local/bin"
TARGET="$BIN_DIR/harken"
mkdir -p "$BIN_DIR"

cat >"$TARGET" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$ROOT"
exec "$UV_BIN" run harken "\$@"
EOF

chmod +x "$TARGET"

echo "Installed: $TARGET"
echo "Try: harken logs"
echo "If harken is not found in this shell, run:"
echo '  export PATH="$HOME/.local/bin:$PATH"'
