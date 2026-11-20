"""Configuración centralizada de caro5m.

Capas (de mayor a menor prioridad):
  1. variables de entorno del proceso            (p. ej. BET_SIZE=2 python -m caro5m.bot)
  2. ``config/settings.env``                      (ajustes NO secretos; lo edita el TUI con [C])
  3. valores por defecto de ``SETTINGS``

Los secretos (clave privada, direcciones, proxy, credenciales CLOB) viven SOLO en ``.env`` (modo 0600).
Este módulo no lee nunca ``.env``: lo hace únicamente el bot (``bot.load_env``) y el asistente del TUI.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def home_dir() -> Path:
    return Path(os.environ.get("CARO5M_HOME") or PROJECT_ROOT).expanduser().resolve()


def env_file() -> Path:
    return Path(os.environ.get("CARO5M_ENV_FILE") or home_dir() / ".env").expanduser()


def settings_file() -> Path:
    return Path(os.environ.get("CARO5M_SETTINGS_FILE") or home_dir() / "config" / "settings.env").expanduser()


def data_dir() -> Path:
    return Path(os.environ.get("CARO5M_DATA_DIR") or home_dir() / "data").expanduser()


def shm_dir() -> Path:
    """Carpeta compartida bot <-> PTB (ficheros pequeños que se reescriben cada ~2 s).

    CARO5M_SHM_DIR si está definida; si no /dev/shm (Linux) o ``data/shm`` (macOS, Windows o sin /dev/shm escribible)."""
    explicit = os.environ.get("CARO5M_SHM_DIR")
    if explicit:
        return Path(explicit).expanduser()
    from . import compat

    return compat.default_shm_dir(data_dir())  # /dev/shm en Linux; data/shm en macOS y Windows


def log_path() -> Path:
    return Path(os.environ.get("CARO5M_LOG_FILE") or data_dir() / "caro5m.log").expanduser()


def status_path() -> Path:
    return Path(os.environ.get("CARO5M_STATUS_FILE") or data_dir() / "status.json").expanduser()


def control_dir() -> Path:
    return Path(os.environ.get("CARO5M_CONTROL_DIR") or data_dir() / "control").expanduser()


def state_path(mode: str) -> Path:
    """Un estado por modo: la simulación nunca contamina la racha ni las posiciones reales."""
    return data_dir() / ("state_dry.json" if mode == "dry" else "state_live.json")


def ptb_out_dir() -> Path:
    return Path(os.environ.get("CARO5M_PTB_OUT") or data_dir() / "ptb").expanduser()


def ptb_shm_path() -> Path:
    return Path(os.environ.get("PM_PTB_SHM") or shm_dir() / "pm_ptb_latest.json")


def ptb_kill_path() -> Path:
    return shm_dir() / "pm_ptb_disable"


# ── ajustes ────────────────────────────────────────────────────────
def _bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "si", "sí", "on")


def _csv(v: str) -> List[str]:
    return [x.strip() for x in str(v).replace(";", ",").split(",") if x.strip()]


@dataclass(frozen=True)
class Spec:
    key: str
    default: str
    kind: str  # float | int | bool | csv | str | tz | coins | scale
    label: str  # etiqueta en español para el TUI
    hot: bool = True  # True = el bot lo aplica sin reiniciar
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    help: str = ""


SETTINGS: List[Spec] = [
    Spec("DRY_RUN", "1", "bool", "Modo prueba al arrancar (no envía órdenes)", True,
         help="1 = arranca en simulación (por defecto). 0 = arranca con ÓRDENES REALES. Con el bot en marcha el modo se cambia con [M]."),
    Spec("BET_SIZE", "1.00", "float", "Apuesta por operación (USDC)", True, 1.0, 1000.0, "Mínimo 1,00 (mínimo de Polymarket)."),
    Spec("TP_OFFSET", "0.08", "float", "Take-profit (+ sobre entrada)", True, 0.01, 1.0,
         "0,01–1,00. Si entrada + TP ≥ 0,99 el objetivo se limita a 0,99 (precio máximo de Polymarket): vende a 0,99 "
         "o mantiene hasta la resolución/tiempo máximo."),
    Spec("SL_OFFSET", "0.20", "float", "Stop-loss (− sobre entrada)", True, 0.01, 0.99,
         "0,01–0,99. Si entrada − SL queda por debajo de 0,01 el SL no salta (equivale a desactivarlo)."),
    Spec("CB_LOSSES", "2", "int", "Pérdidas seguidas para el freno (CB)", True, 1, 100),
    Spec("CB_COOLDOWN", "3600", "int", "Pausa del freno (segundos)", True, 60, 86400),
    Spec("MIN_TOKEN", "0.45", "float", "Precio mínimo de compra", True, 0.01, 0.98, "Rango de precio del lado que se compra (0,01–0,99)."),
    Spec("MAX_TOKEN", "0.70", "float", "Precio máximo de compra", True, 0.02, 0.99),
    Spec("POSITION_TIMEOUT", "360", "int", "Tiempo máx. de posición (s); al vencer intenta vender y cobrar", True, 30, 3600,
         help="Pasado este tiempo se intenta una venta FOK al mejor bid. Si el mercado ya está resuelto y la posición gana, "
              "en una cartera EOA se cobra on-chain (AUTO_REDEEM); en Safe/email el panel avisa de cobrar en polymarket.com."),
    Spec("AUTO_REDEEM", "1", "bool", "Cobrar automático (solo cartera EOA) al resolver a favor", True,
         help="1 = en carteras EOA (clave propia, tipo 0), si un mercado se resuelve a favor el bot llama a redeemPositions "
              "en el CTF de Polymarket (hace falta POL para el gas). En modo PRUEBA solo se simula, no se envía nada. "
              "En carteras Safe o email no se envía ninguna transacción: el panel avisa de cobrar en polymarket.com."),
    Spec(
        "POLYGON_RPCS",
        "https://polygon-bor-rpc.publicnode.com,https://polygon.drpc.org,https://1rpc.io/matic",
        "csv",
        "RPC de Polygon (lista, separada por comas)",
        True,
        help="Se usan en rotación para leer Chainlink on-chain.",
    ),
    Spec("BINANCE_HOST", "https://api.binance.com", "str", "Host REST de Binance", True, help="En EE. UU. usa https://api.binance.us"),
    Spec("CARO5M_TZ", "Europe/Madrid", "tz", "Zona horaria del reloj", True),
    Spec("CARO5M_USE_PTB", "1", "bool", "Usar PTB temprano (c2) si el proceso PTB corre", True),
    Spec("POLY_SIGNATURE_TYPE", "2", "int", "Tipo de firma (0 EOA · 1 proxy · 2 Safe)", False, 0, 2),
    Spec("CARO5M_PROXY_SCAN", "0", "bool", "Proxy también para Gamma/libro de órdenes", False,
         help="0 = el proxy solo se usa para órdenes y claves (recomendado). 1 = también lecturas de mercado."),
    Spec("COINS", "btc,eth", "coins", "Monedas a operar", True,
         help="Mercados de 5 min: btc, eth, xrp, sol, doge, bnb, hype, zec. Se aplica en caliente; una moneda quitada con "
              "posición abierta se sigue gestionando hasta cerrarla."),
    Spec("COIN_SCALE", "", "scale", "Escala de umbrales por moneda (avanzado)", True,
         help="Opcional, p. ej. «xrp:1.5,doge:2»: exige movimientos X veces mayores a esa moneda. Vacío = 1 en todas."),
    Spec("AUTO_START_PTB", "1", "bool", "Arrancar también el PTB al arrancar el bot desde el panel", True,
         help="1 = al pulsar [B] o [M] el panel lanza también scripts/run-ptb.sh (y lo para con el bot)."),
    Spec("KEEP_RUNNING_ON_EXIT", "0", "bool", "Dejar el bot en marcha al salir del panel", True,
         help="0 (por defecto) = al salir del panel (Q, Ctrl+C, cerrar la terminal) se paran el bot y el PTB de esta carpeta. "
              "1 = siguen en marcha (servidores/systemd)."),
    Spec("CHAIN_ID", "137", "int", "Chain ID", False, 1, 999999),
    Spec("CLOB_HOST", "https://clob.polymarket.com", "str", "Host CLOB", False),
    Spec("GAMMA_HOST", "https://gamma-api.polymarket.com", "str", "Host Gamma", True),
]
SPEC_BY_KEY: Dict[str, Spec] = {s.key: s for s in SETTINGS}
# Solo estos se muestran/editan en el TUI (el resto se cambia a mano en config/settings.env)
EDITABLE_KEYS = [
    "BET_SIZE", "TP_OFFSET", "SL_OFFSET", "CB_LOSSES", "CB_COOLDOWN", "MIN_TOKEN", "MAX_TOKEN",
    "POSITION_TIMEOUT", "AUTO_REDEEM", "COINS", "POLYGON_RPCS", "BINANCE_HOST", "CARO5M_TZ", "CARO5M_USE_PTB", "AUTO_START_PTB",
    "KEEP_RUNNING_ON_EXIT",
]

# nombres antiguos aceptados como alias
ALIASES = {"CARO5M_USE_PTB": ["GROK_PM_PTB"]}


def parse_env_text(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def read_settings_file(path: Optional[Path] = None) -> Dict[str, str]:
    p = path or settings_file()
    try:
        raw = parse_env_text(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    # comentarios al final de línea («BET_SIZE=1.00   # USDC»): solo en settings.env, nunca en .env (una clave puede llevar #)
    return {k: re.split(r"\s+#", v, maxsplit=1)[0].strip() for k, v in raw.items()}


def convert(spec: Spec, raw: str) -> Any:
    """Convierte y valida. Lanza ValueError con mensaje en español."""
    raw = str(raw).strip()
    if spec.kind in ("float", "int"):
        try:
            val: Any = float(raw.replace(",", ".")) if spec.kind == "float" else int(float(raw.replace(",", ".")))
        except ValueError:
            raise ValueError(f"{spec.label}: «{raw}» no es un número")
        if spec.minimum is not None and val < spec.minimum:
            raise ValueError(f"{spec.label}: mínimo {spec.minimum:g}")
        if spec.maximum is not None and val > spec.maximum:
            raise ValueError(f"{spec.label}: máximo {spec.maximum:g}")
        return val
    if spec.kind == "bool":
        return _bool(raw)
    if spec.kind == "csv":
        items = _csv(raw)
        if not items:
            raise ValueError(f"{spec.label}: la lista no puede estar vacía")
        for it in items:
            if not it.startswith(("http://", "https://")):
                raise ValueError(f"{spec.label}: «{it}» debe empezar por http:// o https://")
        return items
    if spec.kind == "coins":
        from . import coins as coins_mod

        try:
            return coins_mod.parse_coins(raw)
        except ValueError as e:
            raise ValueError(f"{spec.label}: {e}")
    if spec.kind == "scale":
        from . import coins as coins_mod

        try:
            return coins_mod.parse_scales(raw)
        except ValueError as e:
            raise ValueError(f"{spec.label}: {e}")
    if spec.kind == "tz":
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(raw)
        except Exception:
            raise ValueError(f"{spec.label}: zona «{raw}» no válida (ej. Europe/Madrid)")
        return raw
    if spec.kind == "str" and spec.key.endswith("_HOST"):
        if not raw.startswith(("http://", "https://")):
            raise ValueError(f"{spec.label}: debe empezar por http:// o https://")
        return raw.rstrip("/")
    return raw


def load_settings(path: Optional[Path] = None, environ: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Valores efectivos. Un valor inválido cae al anterior capa (nunca rompe el arranque)."""
    environ = os.environ if environ is None else environ
    file_vals = read_settings_file(path)
    out: Dict[str, Any] = {}
    for s in SETTINGS:
        candidates: List[str] = []
        for k in [s.key] + ALIASES.get(s.key, []):
            if k in environ:
                candidates.append(environ[k])
        for k in [s.key] + ALIASES.get(s.key, []):
            if k in file_vals:
                candidates.append(file_vals[k])
        candidates.append(s.default)
        for c in candidates:
            try:
                out[s.key] = convert(s, c)
                break
            except ValueError:
                continue
    if out["MIN_TOKEN"] >= out["MAX_TOKEN"]:
        out["MIN_TOKEN"], out["MAX_TOKEN"] = 0.45, 0.70
    return out


def settings_mtime() -> float:
    try:
        return settings_file().stat().st_mtime
    except OSError:
        return 0.0


def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    """Escritura atómica (tmp + rename) con permisos fijados ANTES de escribir el contenido."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        from . import compat

        compat.fchmod_private(fd, mode)  # Windows: sin permisos POSIX (no-op)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        compat.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def save_settings(changes: Dict[str, str], path: Optional[Path] = None) -> Dict[str, Any]:
    """Valida y guarda SOLO claves conocidas de ajustes. Nunca toca secretos ni otras claves."""
    p = path or settings_file()
    cleaned: Dict[str, str] = {}
    for k, v in changes.items():
        spec = SPEC_BY_KEY.get(k)
        if spec is None:
            raise ValueError(f"Ajuste desconocido: {k}")
        convert(spec, v)  # lanza ValueError si es inválido
        cleaned[k] = str(v).strip()
    mn = float(cleaned.get("MIN_TOKEN", load_settings(p)["MIN_TOKEN"]))
    mx = float(cleaned.get("MAX_TOKEN", load_settings(p)["MAX_TOKEN"]))
    if mn >= mx:
        raise ValueError("El precio mínimo debe ser menor que el máximo")
    lines: List[str] = []
    seen = set()
    try:
        old = p.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        old = ["# Ajustes de caro5m (NO secretos). Editable desde el TUI con [C]."]
    for raw in old:
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k = line.split("=", 1)[0].strip()
            if k in cleaned:
                lines.append(f"{k}={cleaned[k]}")
                seen.add(k)
                continue
        lines.append(raw)
    for k, v in cleaned.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    atomic_write_text(p, "\n".join(lines) + "\n", 0o644)
    return load_settings(p, environ={})
