"""Canal bot <-> TUI por ficheros: estado atómico (bot -> TUI) y órdenes de control (TUI -> bot).

El TUI nunca habla con la red de trading ni lee claves: solo lee ``status.json`` y escribe ficheros en ``control/``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import compat, config

STALE_AFTER = 15.0  # s sin actualizar status.json => bot detenido


def atomic_write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), default=str)
    compat.replace(tmp, path)  # Windows: reintenta si el panel tiene el fichero abierto


def write_status(obj: Dict[str, Any]) -> None:
    atomic_write_json(config.status_path(), obj)


def read_status() -> Optional[Dict[str, Any]]:
    try:
        with open(config.status_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def pid_alive(pid: Optional[int]) -> bool:
    """¿Sigue vivo ese pid? (Linux/macOS: señal 0 · Windows: OpenProcess/psutil, nunca os.kill)."""
    return compat.pid_alive(pid)


def bot_running(st: Optional[Dict[str, Any]], now: Optional[float] = None) -> bool:
    if not st:
        return False
    now = time.time() if now is None else now
    ts = float(st.get("ts") or 0)
    if float(st.get("stopped_at") or 0) >= ts > 0:  # el panel lo paró: deja de contar como «en marcha» al momento
        return False
    return (now - ts) <= STALE_AFTER


def mark_stopped(now: Optional[float] = None) -> None:
    """Tras parar el bot: marca status.json como detenido (el bot vivo lo reescribe sin esta marca en su siguiente ciclo)."""
    st = read_status()
    if not st:
        return
    st["stopped_at"] = time.time() if now is None else now
    try:
        write_status(st)
    except Exception:
        pass


# ── control (TUI -> bot) ───────────────────────────────────────────
def _cdir() -> Path:
    d = config.control_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_paused() -> bool:
    return (config.control_dir() / "pause").exists()


def set_paused(paused: bool) -> None:
    f = _cdir() / "pause"
    if paused:
        f.write_text(str(int(time.time())), encoding="utf-8")
    else:
        try:
            f.unlink()
        except FileNotFoundError:
            pass


def get_mode_request() -> Optional[str]:
    try:
        v = (config.control_dir() / "mode").read_text(encoding="utf-8").strip().lower()
        return v if v in ("dry", "live") else None
    except OSError:
        return None


def set_mode_request(mode: str) -> None:
    if mode not in ("dry", "live"):
        raise ValueError("modo debe ser dry o live")
    f = _cdir() / "mode"
    tmp = f.with_name("mode.tmp")
    tmp.write_text(mode, encoding="utf-8")
    compat.replace(tmp, f)


def clear_mode_request() -> None:
    try:
        (config.control_dir() / "mode").unlink()
    except FileNotFoundError:
        pass


def request_sell(coins: Optional[List[str]] = None) -> None:
    atomic_write_json(_cdir() / "sell_now", {"ts": time.time(), "coins": coins or []})  # [] = todas las monedas del bot


def consume_sell() -> Optional[Dict[str, Any]]:
    f = config.control_dir() / "sell_now"
    try:
        req = json.loads(f.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception:
        req = {"coins": []}  # ilegible -> todas las monedas del bot
    try:
        f.unlink()
    except FileNotFoundError:
        pass
    if time.time() - float(req.get("ts") or 0) > 120:  # petición caducada
        return None
    return req
