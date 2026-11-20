#!/usr/bin/env bash
# Arranque manual del PTB temprano (opcional; solo lectura, sin claves).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CARO5M_HOME="$DIR"
mkdir -p "$DIR/data"
PY="$DIR/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" -m caro5m.ptb >>"$DIR/data/ptb.log" 2>&1
