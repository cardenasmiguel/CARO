"""Arrancar y parar el bot desde el panel (tecla B, o M cuando el bot está parado).

Todo es local y sin red: lanza el bot como proceso independiente (Linux/macOS: ``scripts/run-bot.sh`` en sesión propia;
Windows: ``python -m caro5m.bot`` desacoplado, sin ventana) y apunta su pid en ``data/bot.pid``. Para parar busca TODOS los
bots/PTB de esta carpeta (``caro5m.procs``: status.json, ficheros de pid y búsqueda de procesos por cwd/CARO5M_HOME), nunca de
otra carpeta. Lo dependiente del sistema está en ``caro5m.compat``.
No toca claves: el bot lee su ``.env`` él mismo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import compat, config, procs, status as status_mod

SERVICE = "caro5m-bot"
PTB_SERVICE = "caro5m-ptb"
START_TIMEOUT = 30.0
_children: List["subprocess.Popen[bytes]"] = []  # procesos lanzados por este panel (se recogen para no dejar zombis)


class LaunchError(Exception):
    """El arranque no se puede hacer (o no hace falta); el texto va ya en español para mostrarlo tal cual."""

    def __init__(self, code: str, text: str) -> None:
        super().__init__(text)
        self.code = code
        self.text = text


def pid_path() -> Path:
    return config.data_dir() / "bot.pid"


def launch_err_path() -> Path:
    return config.data_dir() / "bot-launch.err"


def default_script() -> Path:
    for base in (config.PROJECT_ROOT, config.home_dir()):
        p = base / "scripts" / "run-bot.sh"
        if p.is_file():
            return p
    return config.PROJECT_ROOT / "scripts" / "run-bot.sh"


# ── pid ─────────────────────────────────────────────────────────────
def read_pid() -> Optional[int]:
    try:
        v = int(pid_path().read_text(encoding="utf-8").strip())
        return v if v > 1 else None
    except (OSError, ValueError):
        return None


def write_pid(pid: int) -> None:
    p = pid_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(str(int(pid)), encoding="utf-8")
    compat.replace(tmp, p)


def clear_pid() -> None:
    try:
        pid_path().unlink()
    except FileNotFoundError:
        pass


def _cmdline(pid: int) -> str:
    return compat.process_cmdline(int(pid))  # Linux /proc · macOS ps · Windows psutil/PowerShell


def _spawn_cmd(script: Path, module: str) -> List[str]:
    """Qué se lanza: el script bash en Linux/macOS; en Windows (sin bash) ``python -m <module>`` del venv."""
    if compat.is_windows():
        return compat.module_command(module, script.parent.parent)
    return ["bash", str(script)] if not os.access(script, os.X_OK) else [str(script)]


def _child_env() -> Dict[str, str]:
    env = dict(os.environ)
    env["CARO5M_HOME"] = str(config.home_dir())
    env["PYTHONUTF8"] = "1"  # Windows: consola y ficheros en UTF-8
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def is_bot_process(pid: Optional[int], marker: str = "caro5m.bot") -> bool:
    """¿Es ese pid nuestro bot? (``python -m caro5m.bot`` o, un instante antes del exec, ``run-bot.sh``)."""
    if not pid or not status_mod.pid_alive(pid):
        return False
    cmd = _cmdline(int(pid))
    return bool(cmd) and (marker in cmd or "run-bot.sh" in cmd)


def reap() -> None:
    """Recoge los procesos hijo ya terminados (el bot sigue vivo aunque se cierre el panel; esto solo evita zombis)."""
    for c in list(_children):
        try:
            if c.poll() is not None:
                _children.remove(c)
        except Exception:
            _children.remove(c)


# ── systemd ─────────────────────────────────────────────────────────
def _systemctl(*args: str) -> str:
    exe = shutil.which("systemctl")
    if not exe:
        return ""
    try:
        r = subprocess.run([exe, *args], capture_output=True, text=True, timeout=3)
        return (r.stdout or "").strip()
    except Exception:
        return ""


def systemd_state(service: str = SERVICE) -> Dict[str, Any]:
    """{'installed': bool, 'active': bool, 'enabled': bool}; nunca falla (sin systemctl => todo False)."""
    active = _systemctl("is-active", service) in ("active", "activating")
    en = _systemctl("is-enabled", service)
    enabled = en in ("enabled", "enabled-runtime", "static", "alias")
    return {"installed": active or enabled or bool(_systemctl("cat", service)), "active": active, "enabled": enabled}


# ── arrancar ────────────────────────────────────────────────────────
def check_can_start(st: Optional[Dict[str, Any]] = None, use_systemd: bool = True) -> None:
    """Lanza LaunchError si arrancar ahora duplicaría el bot. ``st`` = status.json leído."""
    if status_mod.bot_running(st):
        raise LaunchError("running", "El bot ya está en marcha.")
    pid = read_pid()
    if pid and (is_bot_process(pid) or _our_live_child(pid)):  # hijo recién lanzado: puede no haber hecho exec aún
        raise LaunchError("pid", f"Ya hay un bot lanzado desde el panel (pid {pid}) que no actualiza su estado. "
                                 "Mira el log con [L]; si está colgado, páralo con [B] y arráncalo de nuevo.")
    if pid:
        clear_pid()  # pid antiguo que ya no es nuestro bot
    others = [p for p in project_processes(st) if p.kind == "bot" and not p.systemd]
    if others:
        pids = ", ".join(str(p.pid) for p in others)
        raise LaunchError("orphan", f"Hay un bot de esta carpeta en marcha (pid {pids}) que no actualiza su estado "
                                    "(¿versión antigua o colgado?). Páralo con [B] y arráncalo de nuevo.")
    if use_systemd:
        sd = systemd_state()
        if sd["active"]:
            raise LaunchError("systemd", f"El bot está instalado como servicio ({SERVICE}) y aparece activo: espera unos segundos o mira "
                                         f"«sudo journalctl -u {SERVICE} -n 50». No lo arranco otra vez para no duplicarlo.")
        if sd["enabled"]:
            raise LaunchError("systemd", f"El bot está instalado como servicio ({SERVICE}). Arráncalo con «sudo systemctl start {SERVICE}» "
                                         "para no tener dos copias.")


def start_bot(script: Optional[Path] = None, use_systemd: bool = True, st: Optional[Dict[str, Any]] = None) -> int:
    """Lanza el bot desacoplado (no muere con la terminal; al salir del panel lo para shutdown.py salvo KEEP_RUNNING_ON_EXIT=1). Devuelve el pid."""
    check_can_start(st if st is not None else status_mod.read_status(), use_systemd)
    script = Path(script) if script else default_script()
    if not script.is_file() and not compat.is_windows():
        raise LaunchError("noscript", f"No encuentro {script}. Arranca el bot a mano: {compat.bot_command()}")
    config.data_dir().mkdir(parents=True, exist_ok=True)
    env = _child_env()
    env["CARO5M_LOG_TO_FILE"] = "1"  # el bot escribe data/caro5m.log (lo que sigue la pantalla [L])
    # stdout se descarta (el bot ya escribe su log en el fichero); stderr va a un fichero pequeño por si falla al importar.
    err = open(launch_err_path(), "wb")
    compat.chmod_private(launch_err_path(), 0o600)
    try:
        proc = subprocess.Popen(_spawn_cmd(script, "caro5m.bot"), cwd=str(script.parent.parent), env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=err, **compat.detached_popen_kwargs())
    except OSError as e:
        raise LaunchError("spawn", f"No se pudo lanzar el bot: {e.strerror or e}") from e
    finally:
        err.close()
    _children.append(proc)
    write_pid(proc.pid)
    return proc.pid


def wait_until_fresh(pid: int, since: float, timeout: float = START_TIMEOUT, interval: float = 0.5,
                     sleep: Any = time.sleep, clock: Any = time.time) -> bool:
    """Espera (hasta ``timeout`` s) a que status.json lo escriba un bot recién arrancado. Falso si el proceso muere antes."""
    end = clock() + timeout
    while clock() < end:
        st = status_mod.read_status()
        if st and status_mod.bot_running(st) and float(st.get("ts") or 0) >= since - 1:
            return True
        reap()
        if not status_mod.pid_alive(pid) or _exited(pid):
            return False
        sleep(interval)
    return False


def _our_live_child(pid: int) -> bool:
    """¿Es un proceso lanzado por este panel y aún vivo? (justo tras Popen la línea de comandos aún no es la del bot)."""
    for c in _children:
        if c.pid == pid:
            return c.poll() is None
    return False


def _exited(pid: int) -> bool:
    for c in _children:
        if c.pid == pid:
            return c.poll() is not None
    return False


# ── procesos de ESTE proyecto (aunque no los lanzara este panel) ──
def _alive(pid: int) -> bool:
    return status_mod.pid_alive(pid) and not _exited(pid) and not compat.is_zombie(pid)


def known_pids(st: Optional[Dict[str, Any]] = None) -> List[int]:
    """pids conocidos por nuestros ficheros: status.json (campo pid), data/bot.pid y data/ptb.pid."""
    st = st if st is not None else status_mod.read_status()
    out: List[int] = []
    for v in (read_pid(), read_ptb_pid(), (st or {}).get("pid")):
        try:
            if v and int(v) > 1:
                out.append(int(v))
        except (TypeError, ValueError):
            pass
    return out


def project_processes(st: Optional[Dict[str, Any]] = None, scanner: Any = None) -> List["procs.ProcInfo"]:
    """Todos los bots/PTB de esta carpeta: pid de status.json, de los ficheros de pid y búsqueda en el sistema
    (psutil · /proc · ps · PowerShell) con cmdline caro5m.bot/caro5m.ptb/run-bot.sh/run-ptb.sh y cwd o CARO5M_HOME = esta carpeta."""
    try:
        return procs.discover(config.home_dir(), known_pids(st), scanner)
    except Exception as e:  # nunca romper el panel por no poder listar procesos
        from . import setuplog

        setuplog.error("buscar procesos del proyecto", e)
        return []


def bot_processes(st: Optional[Dict[str, Any]] = None) -> List["procs.ProcInfo"]:
    return [p for p in project_processes(st) if p.kind == "bot"]


def stop_project(kinds: Any = ("bot", "ptb"), wait: float = 8.0, include_systemd: bool = False, sleep: Any = time.sleep,
                 found: Optional[List["procs.ProcInfo"]] = None) -> "procs.StopReport":
    """Para TODOS los procesos de este proyecto de esos tipos (SIGTERM, espera ``wait`` s, SIGKILL) y limpia los ficheros."""
    lst = found if found is not None else project_processes()
    lst = [p for p in lst if p.kind in kinds]
    rep = procs.stop(lst, wait=wait, include_systemd=include_systemd, alive=_alive, poll=reap, sleep=sleep)
    if "bot" in kinds and not any(p.kind == "bot" and _alive(p.pid) for p in lst):
        clear_pid()
        status_mod.mark_stopped()
    if "ptb" in kinds and not any(p.kind == "ptb" and _alive(p.pid) for p in lst):
        clear_ptb_pid()
    return rep


def cleanup_control_files() -> None:
    """Al salir: peticiones pendientes (modo, vender) que ya no tienen sentido sin bot. La pausa se respeta."""
    status_mod.clear_mode_request()
    try:
        (config.control_dir() / "sell_now").unlink()
    except OSError:
        pass
    for f in (pid_path(), ptb_pid_path()):
        try:
            f.unlink()
        except OSError:
            pass


# ── parar ───────────────────────────────────────────────────────────
def stop_bot(wait: float = 8.0, sleep: Any = time.sleep) -> int:
    """Para TODOS los bots de este proyecto (también uno viejo o lanzado a mano en otra terminal; nunca de otra carpeta).
    Devuelve el pid (el primero si había duplicados; el informe completo queda en ``last_report``)."""
    global last_report
    had_pid = read_pid()
    bots = [p for p in project_processes() if p.kind == "bot"]
    if not bots:
        if had_pid:
            clear_pid()
            raise LaunchError("notbot", "El pid guardado ya no es el bot (ya se había parado). No toco nada.")
        sd = systemd_state() if shutil.which("systemctl") else {"active": False}
        if sd.get("active"):
            raise LaunchError("systemd", f"El bot corre como servicio: «sudo systemctl stop {SERVICE}».")
        raise LaunchError("nopid", "No encuentro ningún proceso del bot de esta carpeta en marcha.")
    live = [p for p in bots if not p.systemd]
    if not live:
        raise LaunchError("systemd", f"El bot corre como servicio systemd: páralo con «sudo systemctl stop {SERVICE}».")
    rep = stop_project(("bot",), wait=wait, sleep=sleep, found=bots)
    last_report = rep
    if rep.failed and not rep.stopped:
        raise LaunchError("perm", "No se pudo parar el bot: " + "; ".join(rep.failed))
    return live[0].pid


last_report: Optional["procs.StopReport"] = None


# ── PTB temprano (caro5m.ptb): se arranca y se para junto con el bot si AUTO_START_PTB=1 ──
def ptb_pid_path() -> Path:
    return config.data_dir() / "ptb.pid"


def ptb_script() -> Path:
    for base in (config.PROJECT_ROOT, config.home_dir()):
        p = base / "scripts" / "run-ptb.sh"
        if p.is_file():
            return p
    return config.PROJECT_ROOT / "scripts" / "run-ptb.sh"


def read_ptb_pid() -> Optional[int]:
    try:
        v = int(ptb_pid_path().read_text(encoding="utf-8").strip())
        return v if v > 1 else None
    except (OSError, ValueError):
        return None


def _write_ptb_pid(pid: int) -> None:
    p = ptb_pid_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(str(int(pid)), encoding="utf-8")
    compat.replace(tmp, p)


def clear_ptb_pid() -> None:
    try:
        ptb_pid_path().unlink()
    except FileNotFoundError:
        pass


def is_ptb_process(pid: Optional[int]) -> bool:
    """¿Es ese pid nuestro PTB? (``python -m caro5m.ptb`` o ``run-ptb.sh``) — se comprueba su línea de comandos."""
    if not pid or not status_mod.pid_alive(pid):
        return False
    cmd = _cmdline(int(pid))
    return bool(cmd) and ("caro5m.ptb" in cmd or "run-ptb.sh" in cmd)


def ptb_file_age(now: Optional[float] = None) -> Optional[float]:
    """Segundos desde la última escritura de pm_ptb_latest.json (None si no existe). Lo escribe cualquier PTB en marcha."""
    try:
        m = config.ptb_shm_path().stat().st_mtime
    except OSError:
        return None
    return max(0.0, (time.time() if now is None else now) - m)


def ptb_state(use_systemd: bool = True) -> Dict[str, Any]:
    """{'running': bool, 'ours': bool, 'pid': int|None, 'how': 'panel'|'systemd'|'externo'|'parado', 'file_age': s}."""
    pid = read_ptb_pid()
    ours = bool(pid and is_ptb_process(pid))
    age = ptb_file_age()
    if ours:
        return {"running": True, "ours": True, "pid": pid, "how": "panel", "file_age": age}
    if use_systemd and systemd_state(PTB_SERVICE)["active"]:
        return {"running": True, "ours": False, "pid": None, "how": "systemd", "file_age": age}
    if age is not None and age <= 330:  # otro PTB lo reescribe en cada ventana de 5 min (terminal aparte)
        return {"running": True, "ours": False, "pid": None, "how": "externo", "file_age": age}
    return {"running": False, "ours": False, "pid": None, "how": "parado", "file_age": age}


def start_ptb(script: Optional[Path] = None, use_systemd: bool = True) -> Optional[int]:
    """Lanza run-ptb.sh desacoplado (pid en data/ptb.pid). None si ya había uno en marcha (no se duplica)."""
    stt = ptb_state(use_systemd)
    if stt["running"] or _our_live_child(read_ptb_pid() or 0):
        return None
    if read_ptb_pid():
        clear_ptb_pid()  # pid antiguo que ya no es nuestro PTB
    script = Path(script) if script else ptb_script()
    if not script.is_file() and not compat.is_windows():
        raise LaunchError("noscript", f"No encuentro {script}. Arranca el PTB a mano: ./scripts/run-ptb.sh")
    config.data_dir().mkdir(parents=True, exist_ok=True)
    env = _child_env()
    out: Any = subprocess.DEVNULL
    if compat.is_windows():  # run-ptb.sh redirige al log; en Windows lo hace el panel
        out = open(config.data_dir() / "ptb.log", "ab")
    try:
        proc = subprocess.Popen(_spawn_cmd(script, "caro5m.ptb"), cwd=str(script.parent.parent), env=env, stdin=subprocess.DEVNULL,
                                stdout=out, stderr=out if out is not subprocess.DEVNULL else subprocess.DEVNULL,
                                **compat.detached_popen_kwargs())
    except OSError as e:
        raise LaunchError("spawn", f"No se pudo lanzar el PTB: {e.strerror or e}") from e
    finally:
        if out is not subprocess.DEVNULL:
            out.close()
    _children.append(proc)
    _write_ptb_pid(proc.pid)
    return proc.pid


def stop_ptb(wait: float = 5.0, sleep: Any = time.sleep) -> Optional[int]:
    """Para TODOS los PTB de este proyecto (nunca un pid ajeno ni de otra carpeta). None si no había ninguno."""
    ptbs = [p for p in project_processes() if p.kind == "ptb" and not p.systemd]
    if not ptbs:
        clear_ptb_pid()
        return None
    rep = stop_project(("ptb",), wait=wait, sleep=sleep, found=ptbs)
    if rep.failed and not rep.stopped:
        raise LaunchError("perm", "No tengo permiso para parar el PTB.")
    return ptbs[0].pid


def log_tail(lines: int = 6, max_bytes: int = 20000) -> str:
    """Últimas líneas del log del bot (y del stderr del arranque si hay), ya enmascaradas."""
    from . import setuplog

    parts: List[str] = []
    for p in (config.log_path(), launch_err_path()):
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - max_bytes))
                txt = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        got = [ln for ln in txt.splitlines() if ln.strip()][-lines:]
        if got:
            parts.append("\n".join(got))
    return setuplog.mask_text("\n".join(parts))
