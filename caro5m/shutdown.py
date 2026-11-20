"""Salida limpia del panel: al cerrarlo (Q, Ctrl+C, Ctrl+Q, cerrar la terminal = SIGHUP, SIGTERM, cierre de la consola
en Windows) se paran el bot y el PTB de ESTA carpeta y se borran los ficheros de pid/peticiones, salvo
``KEEP_RUNNING_ON_EXIT=1`` o si el bot es el servicio systemd (ese no se toca: se avisa).

Todo pasa por ``shutdown_project()``, que es idempotente (se puede llamar desde la tecla Q, un manejador de señal y
``atexit`` sin parar dos veces). En Windows se registra ``SetConsoleCtrlHandler`` (ctypes) para el cierre de la ventana
(CTRL_CLOSE_EVENT: Windows da ~5 s) además de ``signal.SIGBREAK``.
"""

from __future__ import annotations

import atexit
import os
import sys
import threading
from typing import Any, Callable, List, Optional

from . import compat, config

_lock = threading.Lock()
_done = False
_result: Optional[str] = None
_installed: List[Any] = []
_win_handler: Any = None  # referencia viva al callback de ctypes (si se recolecta, Windows se cuelga al cerrar)

CTRL_C_EVENT, CTRL_BREAK_EVENT, CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT = 0, 1, 2, 5, 6


def keep_running() -> bool:
    try:
        return bool(config.load_settings().get("KEEP_RUNNING_ON_EXIT"))
    except Exception:
        return False


def reset() -> None:
    """Solo para pruebas."""
    global _done, _result
    with _lock:
        _done = False
        _result = None


def shutdown_project(wait: float = 8.0, reason: str = "salir", force: bool = False) -> Optional[str]:
    """Para bot + PTB (+ hijos) de esta carpeta y limpia ficheros. Devuelve el resumen (None si no hizo nada)."""
    global _done, _result
    with _lock:
        if _done:
            return _result
        _done = True
    if keep_running() and not force:
        _result = None
        return None
    from . import launcher, setuplog

    try:
        found = launcher.project_processes()
        rep = launcher.stop_project(("bot", "ptb"), wait=wait, found=found)
        launcher.cleanup_control_files()
        _result = rep.summary() if (found or rep.failed) else None
        if found:
            setuplog.event(f"panel cerrado ({reason}): {rep.summary()}")
    except Exception as e:  # nunca bloquear la salida
        _result = f"No se pudo parar todo al salir: {e}"
        try:
            setuplog.error("parar bot/PTB al salir", e)
        except Exception:
            pass
    return _result


def _on_signal(exit_cb: Optional[Callable[[], None]], hard_exit: bool) -> Callable[[int, Any], None]:
    def handler(signum: int, frame: Any) -> None:  # noqa: ARG001
        name = compat.signal_name(signum)
        # SIGHUP: la terminal ya no existe -> parar rápido. SIGTERM: igual de rápido (el sistema puede matar después).
        shutdown_project(wait=4.0, reason=name)
        if hard_exit:
            os._exit(0)
        if exit_cb:
            try:
                exit_cb()
            except Exception:
                os._exit(0)
    return handler


def install(exit_cb: Optional[Callable[[], None]] = None) -> List[str]:
    """Registra atexit + SIGTERM/SIGHUP (POSIX) o SIGBREAK + SetConsoleCtrlHandler (Windows). Devuelve lo registrado."""
    global _win_handler
    done: List[str] = []
    atexit.register(shutdown_project, 8.0, "atexit")
    done.append("atexit")
    done += compat.install_signal_handlers(_on_signal(exit_cb, False), ("SIGTERM",))
    done += compat.install_signal_handlers(_on_signal(exit_cb, True), ("SIGHUP", "SIGBREAK"))  # sin terminal: salir ya
    if compat.is_windows():
        try:
            _win_handler = _install_console_handler()
            done.append("SetConsoleCtrlHandler")
        except Exception:
            pass
    _installed[:] = done
    return done


def console_event(event: int) -> bool:
    """Lo que hace el manejador de consola de Windows con cada evento (separado para poder probarlo sin Windows)."""
    if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
        shutdown_project(wait=3.0, reason=f"consola {event}")  # Windows mata el proceso ~5 s después
        return True
    if event in (CTRL_C_EVENT, CTRL_BREAK_EVENT):
        shutdown_project(wait=4.0, reason=f"Ctrl {event}")
        return False  # que siga el tratamiento normal (KeyboardInterrupt / salida)
    return False


def _install_console_handler(kernel32: Any = None) -> Any:
    import ctypes

    k32 = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    proto = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(ctypes.c_bool, ctypes.c_uint)
    cb = proto(lambda ev: bool(console_event(int(ev))))
    if not k32.SetConsoleCtrlHandler(cb, True):
        raise OSError("SetConsoleCtrlHandler falló")
    return cb


def result() -> Optional[str]:
    return _result


def print_result(stream: Any = None) -> None:
    if _result:
        try:
            print(f"caro5m: {_result}", file=stream or sys.stdout, flush=True)
        except Exception:
            pass
