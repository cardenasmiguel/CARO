#!/usr/bin/env bash
# caro5m — instalador. Idempotente: puedes repetirlo sin perder tu .env ni tus ajustes.
#
#   ./install.sh                 crea .venv, instala dependencias (lock), prepara .env (0600) y config/settings.env
#   ./install.sh --systemd       además instala los servicios caro5m-bot y caro5m-ptb (necesita sudo)
#   ./install.sh --logrotate     además instala la rotación de logs (necesita sudo)
#   ./install.sh --start         con --systemd: los habilita y arranca (el bot arranca en modo PRUEBA salvo que cambies DRY_RUN)
#   ./install.sh --no-lock       usa requirements.txt en lugar de requirements.lock.txt
#   ./install.sh --python /ruta/python3.12
#   ./install.sh --panel         al terminar abre el panel con la configuración guiada de cuenta (RECOMENDADO, por defecto)
#   ./install.sh --cli           configura la cuenta aquí, en la terminal, con preguntas (python -m caro5m.setup --guided)
#   ./install.sh --no-wizard     no configura la cuenta ahora (luego: panel, tecla [A])
#   ./install.sh --non-interactive   sin preguntas: lee CARO5M_SETUP_* del entorno (ver `python -m caro5m.setup -h`)
#
# En una terminal interactiva el instalador pregunta cómo quieres configurar la cuenta (por defecto, en el panel):
# cómo entras en Polymarket (MetaMask/Rabby, email/Google o cartera propia), clave privada (campo oculto; la guarda
# SOLO el asistente en .env 0600), generación de las claves CLOB, proxy opcional HTTP/SOCKS (para redes u operadores que bloquean el DNS o restringen la salida) y comprobaciones.
# Todo es saltable y repetible desde el panel (tecla [A]). El modo PRUEBA (DRY_RUN) sigue activado: este script nunca
# activa el dinero real.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYBIN="${PYTHON:-python3}"
WITH_SYSTEMD=0; WITH_LOGROTATE=0; START=0; USE_LOCK=1; WIZARD=auto

while [ $# -gt 0 ]; do
  case "$1" in
    --systemd) WITH_SYSTEMD=1 ;;
    --logrotate) WITH_LOGROTATE=1 ;;
    --start) START=1 ;;
    --no-lock) USE_LOCK=0 ;;
    --python) shift; PYBIN="$1" ;;
    --no-wizard) WIZARD=no ;;
    --wizard|--cli) WIZARD=guided ;;
    --panel) WIZARD=panel ;;
    --non-interactive) WIZARD=noninteractive ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "Opción desconocida: $1" >&2; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

command -v "$PYBIN" >/dev/null 2>&1 || die "no encuentro $PYBIN (hace falta Python 3.10 o superior)"
"$PYBIN" - <<'PY' || die "se necesita Python >= 3.10"
import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

# ── Antes de instalar/actualizar: parar el bot y el PTB de ESTA carpeta (si no, seguiría corriendo la versión vieja).
#    Solo procesos de esta carpeta (cwd o CARO5M_HOME); nunca de otros proyectos. Con terminal pregunta (por defecto sí).
say "Comprobando si hay un bot o PTB de esta carpeta en marcha"
STOP_PY="$PYBIN"; [ -x "$DIR/.venv/bin/python" ] && STOP_PY="$DIR/.venv/bin/python"
STOP_ARGS=(--stop)
if [ "$WIZARD" = noninteractive ] || ! { [ -t 0 ] && [ -t 1 ]; }; then STOP_ARGS+=(--yes); fi
PYTHONPATH="$DIR" "$STOP_PY" -m caro5m.procs --root "$DIR" "${STOP_ARGS[@]}" \
  || echo "(aviso: no se pudo parar todo lo de esta carpeta; revisa lo anterior)"

say "Entorno virtual en $DIR/.venv"
[ -d "$DIR/.venv" ] || "$PYBIN" -m venv "$DIR/.venv" || die "no se pudo crear el venv (en Debian/Ubuntu: sudo apt install python3-venv)"
"$DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
REQ="$DIR/requirements.lock.txt"; [ "$USE_LOCK" = 1 ] || REQ="$DIR/requirements.txt"
say "Instalando dependencias ($(basename "$REQ"))"
"$DIR/.venv/bin/pip" install --quiet -r "$REQ"
"$DIR/.venv/bin/pip" install --quiet --no-deps -e "$DIR"

say "Carpetas y ficheros de configuración"
mkdir -p "$DIR/data/control" "$DIR/config"
chmod 700 "$DIR/data" 2>/dev/null || true
if [ ! -f "$DIR/.env" ]; then
  ( umask 177; cp "$DIR/.env.example" "$DIR/.env" )
  say ".env creado vacío (permisos 600). Lo rellena el asistente de cuenta (siguiente paso) o el panel [A]."
else
  chmod 600 "$DIR/.env"
  say ".env ya existe: no se toca (permisos reafirmados a 600)."
fi
[ -f "$DIR/config/settings.env" ] || cp "$DIR/config/settings.env.example" "$DIR/config/settings.env"

PY="$DIR/.venv/bin/python"

# ── Asistente de cuenta: cartera, claves CLOB, proxy y comprobaciones previas (nunca activa dinero real) ──
if [ "$WIZARD" = auto ]; then
  if [ -n "$(env | grep -m1 '^CARO5M_SETUP_')" ]; then WIZARD=noninteractive
  elif [ -t 0 ] && [ -t 1 ]; then
    echo
    echo "¿Cómo quieres configurar tu cuenta de Polymarket (cartera, claves CLOB, proxy)?"
    echo "  1) En el panel (recomendado): se abre al terminar la instalación, con ayuda paso a paso"
    echo "  2) Aquí, en la terminal (asistente por preguntas)"
    echo "  3) Más tarde (desde el panel, tecla A)"
    read -r -p "Elige 1, 2 o 3 [1]: " CHOICE || CHOICE=1
    case "${CHOICE:-1}" in 2) WIZARD=guided ;; 3) WIZARD=no ;; *) WIZARD=panel ;; esac
  else WIZARD=no; fi
fi
LAUNCH_PANEL=0
case "$WIZARD" in
  panel)
    if [ -t 0 ] && [ -t 1 ]; then LAUNCH_PANEL=1; say "La configuración de cuenta se abrirá en el panel al terminar."
    else say "Sin terminal interactiva: abre el panel después ($PY -m caro5m) y se abrirá la configuración de cuenta."; fi ;;
  guided)
    say "Asistente de cuenta (puedes saltarte cada paso; se repite con: $PY -m caro5m.setup  o en el panel con [A])"
    CARO5M_FROM_INSTALLER=1 "$PY" -m caro5m.setup --guided || echo "(el asistente terminó con avisos: revisa lo anterior; puedes repetirlo cuando quieras)" ;;
  noninteractive)
    say "Asistente de cuenta (sin preguntas, variables CARO5M_SETUP_*)"
    CARO5M_FROM_INSTALLER=1 "$PY" -m caro5m.setup --non-interactive || echo "(el asistente terminó con avisos: revisa lo anterior)" ;;
  *)
    say "Asistente de cuenta omitido. Cuando quieras: $PY -m caro5m.setup  (o en el panel, tecla [A] Cuenta)" ;;
esac

if [ "$WITH_SYSTEMD" = 1 ]; then
  command -v systemctl >/dev/null 2>&1 || die "systemd no disponible en este sistema"
  SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
  RUN_USER="${SUDO_USER:-$(id -un)}"
  say "Instalando servicios systemd (usuario: $RUN_USER)"
  for unit in caro5m-bot caro5m-ptb; do
    sed -e "s|@DIR@|$DIR|g" -e "s|@USER@|$RUN_USER|g" -e "s|@PY@|$PY|g" "$DIR/systemd/$unit.service" | $SUDO tee "/etc/systemd/system/$unit.service" >/dev/null
  done
  $SUDO systemctl daemon-reload
  if [ "$START" = 1 ]; then
    $SUDO systemctl enable --now caro5m-ptb caro5m-bot
    say "Servicios arrancados. Estado: systemctl status caro5m-bot"
  else
    say "Servicios instalados pero NO arrancados. Para arrancarlos: sudo systemctl enable --now caro5m-ptb caro5m-bot"
  fi
fi

if [ "$WITH_LOGROTATE" = 1 ]; then
  SUDO=""; [ "$(id -u)" = 0 ] || SUDO="sudo"
  say "Instalando logrotate"
  sed -e "s|@DIR@|$DIR|g" "$DIR/logrotate/caro5m" | $SUDO tee /etc/logrotate.d/caro5m >/dev/null
fi

G=$'\033[1;32m'; B=$'\033[1m'; N=$'\033[0m'
[ -t 1 ] || { G=""; B=""; N=""; }
cat <<EOF

${G}════════════════════════════════════════════════════════════════${N}
${G} ✔ Instalación terminada.${N} ¿Y ahora qué?
${G}════════════════════════════════════════════════════════════════${N}
  1) Abre el panel del bot:   ${B}./start.sh${N}
       (equivale a: $DIR/.venv/bin/caro5m)
  2) En el panel:
       ${B}[A]${N} cuenta (cartera, claves CLOB, proxy y comprobaciones)
       ${B}[B]${N} arranca el bot   (siempre empieza en modo PRUEBA, sin dinero real)
       ${B}[M]${N} pasa a modo REAL (pide confirmación; antes comprueba saldo y permisos)
       ${B}[L]${N} log · ${B}[C]${N} ajustes (en caliente) · ${B}[R]${N} reiniciar si algo lo pide · ${B}[Q]${N} salir
  3) Sin panel (otra terminal o tmux):  ./scripts/run-bot.sh     (PTB opcional: ./scripts/run-ptb.sh)
  4) Cuenta desde la terminal:          $PY -m caro5m.setup --menu   ·  solo comprobar: $PY -m caro5m.setup --check
EOF
if [ -t 0 ] && [ -t 1 ] && [ "$WIZARD" != noninteractive ]; then
  echo
  if [ "$LAUNCH_PANEL" = 1 ]; then echo "El panel se abrirá con la configuración de cuenta. Al terminar, pulsa Esc para ir al panel del bot."; fi
  read -r -p "¿Abrir el panel ahora? [S/n] " OPEN || OPEN=n
  case "${OPEN:-s}" in
    [nN]*) echo "Vale. Cuando quieras: ./start.sh" ;;
    *) exec "$DIR/start.sh" ;;
  esac
fi
