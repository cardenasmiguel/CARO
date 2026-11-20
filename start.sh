#!/usr/bin/env bash
# Abre el panel de caro5m (TUI) con el Python del entorno virtual del proyecto.
#   ./start.sh            # desde el panel: [B] arranca el bot · [M] modo REAL · [L] log · [A] cuenta
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export CARO5M_HOME="$DIR"
PY="$DIR/.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "No encuentro $PY. Ejecuta primero ./install.sh" >&2
  exit 1
fi
exec "$PY" -m caro5m "$@"
