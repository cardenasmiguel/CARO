#!/usr/bin/env bash
# Arranque manual del bot (sin systemd): escribe también en data/caro5m.log para que lo vea la pantalla [L] del panel.
#   ./scripts/run-bot.sh            # en una terminal aparte (o dentro de tmux/screen)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export CARO5M_HOME="$DIR"
export CARO5M_LOG_TO_FILE=1
mkdir -p "$DIR/data"
PY="$DIR/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"
exec "$PY" -m caro5m.bot
