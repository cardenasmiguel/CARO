"""Registro de la configuración de cuenta (cartera, claves CLOB, proxy, comprobaciones) para poder COPIAR los errores.

Escribe en ``data/logs/setup.log`` (todo lo que pasa) y ``data/logs/errors.log`` (solo errores, con el traceback completo).
Antes de escribir se ENMASCARA todo lo sensible: claves privadas, credenciales CLOB, usuario/contraseña/IP de los proxies,
direcciones (forma corta) y cualquier IPv4. Los ficheros se crean con permisos 0600 (carpeta 0700) y rotan a 1 MB.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import subprocess
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

from . import __version__, config

MAX_BYTES = 1_000_000
SECRET_ENV = ("POLY_PRIVATE_KEY", "POLY_CLOB_API_KEY", "POLY_CLOB_API_SECRET", "POLY_CLOB_API_PASSPHRASE")


def log_dir() -> Path:
    return config.data_dir() / "logs"


def setup_log() -> Path:
    return log_dir() / "setup.log"


def errors_log() -> Path:
    return log_dir() / "errors.log"


def short_path(p: Path) -> str:
    """Ruta para mostrar en pantalla (relativa a la carpeta del proyecto si se puede)."""
    try:
        return str(p.relative_to(config.home_dir()))
    except ValueError:
        return str(p)


# ── enmascarado ────────────────────────────────────────────────────
_URL_CREDS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)(?P<user>[^\s:@/]+):(?P<pw>[^\s@/]*)@(?P<host>[^\s/:?#]+)")
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_HEX64 = re.compile(r"\b(?:0x)?[0-9a-fA-F]{64}\b")
_ADDR = re.compile(r"\b0x[0-9a-fA-F]{40}\b")


def _mask_ip(m: "re.Match[str]") -> str:
    ip = m.group(0)
    if ip.startswith(("127.", "0.0.0.0")):
        return ip
    a, b, *_ = ip.split(".")
    return f"{a}.{b}.•••.•••"


def _known_secrets() -> List[str]:
    """Valores sensibles del .env (claves, credenciales CLOB y las partes del proxy) para borrarlos literalmente del texto."""
    out: List[str] = []
    try:
        env = config.parse_env_text(config.env_file().read_text(encoding="utf-8"))
    except Exception:
        return out
    for k in SECRET_ENV:
        v = env.get(k, "")
        if len(v) >= 6:
            out.append(v)
            if v.lower().startswith("0x"):
                out.append(v[2:])
    for item in (env.get("POLYMARKET_PROXY_LIST", "") or "").replace(";", ",").split(","):
        m = _URL_CREDS.search(item.strip() if "://" in item else "http://" + item.strip())
        if m:
            for part in (m.group("user"), m.group("pw")):
                if len(part) >= 3:
                    out.append(part)
            out.append(m.group("host"))
    return sorted(set(out), key=len, reverse=True)


def mask_text(text: Any, extra: Iterable[str] = ()) -> str:
    s = str(text)
    for sec in list(extra) + _known_secrets():
        if sec:
            s = s.replace(sec, "••••")
    s = _URL_CREDS.sub(lambda m: f"{m.group('scheme')}••••:••••@{_IPV4.sub(_mask_ip, m.group('host'))}", s)
    s = _HEX64.sub("0x••••(clave)", s)
    s = _ADDR.sub(lambda m: f"{m.group(0)[:6]}…{m.group(0)[-4:]}", s)
    s = _IPV4.sub(_mask_ip, s)
    return s


# ── escritura ──────────────────────────────────────────────────────
def _append(path: Path, text: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        from . import compat

        compat.chmod_private(path.parent, 0o700)  # Windows: no-op
        if path.exists() and path.stat().st_size > MAX_BYTES:
            compat.replace(path, path.with_suffix(path.suffix + ".1"))
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(text)
        compat.chmod_private(path, 0o600)
    except OSError:
        pass  # un fallo de registro nunca debe romper la configuración


def _ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def event(msg: str, level: str = "INFO") -> None:
    _append(setup_log(), f"[{_ts()}] {level} {mask_text(msg)}\n")


def error(context: str, exc: Optional[BaseException] = None, detail: str = "") -> None:
    """Registra un error con su traceback COMPLETO (enmascarado) en errors.log y una línea en setup.log."""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc is not None else ""
    head = f"[{_ts()}] ERROR {context}" + (f" — {detail}" if detail else "")
    block = mask_text(head + ("\n" + tb.rstrip() if tb else "")) + "\n" + "-" * 60 + "\n"
    _append(errors_log(), block)
    _append(setup_log(), mask_text(head) + f"  (traceback en {errors_log().name})\n" if exc is not None else mask_text(head) + "\n")


def record_check(c: Any) -> None:
    """Una línea por comprobación (incluye el nivel); los fallos y avisos también se copian a errors.log."""
    line = f"CHECK {c.level.upper():4} {c.title}: {c.detail}" + (f" → {c.fix}" if c.fix else "")
    event(line, "INFO" if c.level in ("ok", "info") else "WARN")
    if c.level in ("fail", "warn"):
        _append(errors_log(), mask_text(f"[{_ts()}] {c.level.upper()} comprobación «{c.title}»: {c.detail}" + (f"\n    → {c.fix}" if c.fix else "")) + "\n" + "-" * 60 + "\n")


# ── lectura / informe copiable ─────────────────────────────────────
def tail(path: Path, max_lines: int = 250, max_bytes: int = 120_000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    lines = data.splitlines()
    return "\n".join(lines[-max_lines:])


def header() -> str:
    try:
        s = config.load_settings()
        env = config.parse_env_text(config.env_file().read_text(encoding="utf-8")) if config.env_file().exists() else {}
        proxy = "sí" if env.get("POLYMARKET_PROXY_LIST") else "no"
        bits = f"firma={s['POLY_SIGNATURE_TYPE']} proxy={proxy} lecturas_por_proxy={'sí' if s['CARO5M_PROXY_SCAN'] else 'no'} DRY_RUN={'sí' if s['DRY_RUN'] else 'no'}"
    except Exception:
        bits = "(ajustes no disponibles)"
    return f"caro5m {__version__} · Python {platform.python_version()} · {platform.system()} {platform.release()} · {bits}"


def report(which: str = "errors", max_lines: int = 250) -> str:
    """Texto listo para copiar y pegar (cabecera + log enmascarado)."""
    p = errors_log() if which == "errors" else setup_log()
    body = tail(p, max_lines)
    if not body:
        body = "(sin errores registrados)" if which == "errors" else "(registro vacío)"
    return f"{header()}\nFichero: {short_path(p)}\n\n{mask_text(body)}\n"


# ── capturar los mensajes de la librería de Polymarket (si no, salen por stderr y ensucian el panel) ──
class _LibHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            _append(setup_log(), f"[{_ts()}] LIB {record.levelname} {mask_text(record.getMessage())}\n")
        except Exception:
            pass


_installed = False


def install_library_capture() -> None:
    global _installed
    if _installed:
        return
    lg = logging.getLogger("py_clob_client_v2")
    lg.addHandler(_LibHandler())
    lg.propagate = False
    _installed = True


# ── portapapeles ───────────────────────────────────────────────────
def _run_clip(cmd: List[str], text: str) -> bool:
    try:
        r = subprocess.run(cmd, input=text.encode(), capture_output=True, timeout=4)
        return r.returncode == 0
    except Exception:
        return False


def native_copy(text: str) -> Optional[str]:
    """Copia al portapapeles del sistema con lo que haya (pyperclip, wl-copy, xclip, xsel, pbcopy, clip.exe). Devuelve el nombre o None."""
    try:
        import pyperclip  # type: ignore

        pyperclip.copy(text)
        return "pyperclip"
    except Exception:
        pass
    for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"], ["pbcopy"], ["clip.exe"]):
        if _run_clip(cmd, text):
            return cmd[0]
    return None


def copy_text(text: str, app: Any = None) -> str:
    """Copia ``text``: portapapeles del sistema si se puede y además OSC52 (terminal). Devuelve una frase en español."""
    how = native_copy(text)
    osc = False
    if app is not None:
        try:
            app.copy_to_clipboard(text)
            osc = True
        except Exception:
            osc = False
    if how:
        return f"Copiado al portapapeles ({how})."
    if osc:
        return "Enviado al portapapeles del terminal (OSC52). Si no se pega, selecciona el texto con el ratón o abre el fichero indicado."
    return "No pude acceder al portapapeles: selecciona el texto con el ratón o abre el fichero indicado."
