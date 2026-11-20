"""Compatibilidad Linux / macOS / Windows en un solo sitio.

Todo lo que depende del sistema operativo (procesos, permisos, renombrados atómicos, carpeta compartida, consola UTF-8)
pasa por aquí para que el resto del código sea igual en los tres sistemas. Sin dependencias obligatorias: si está
``psutil`` (se instala en Windows) se usa; si no, hay alternativas con la biblioteca estándar.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
# Windows: proceso desacoplado del panel (sin consola, en su propio grupo: sobrevive al cerrar el panel)
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000


def is_windows() -> bool:
    return os.name == "nt" or sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def _psutil() -> Any:
    try:
        import psutil  # type: ignore

        return psutil
    except Exception:
        return None


# ── procesos ───────────────────────────────────────────────────────
def _win_pid_alive(pid: int) -> bool:
    import ctypes  # solo Windows
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: existe pero no es nuestro
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def pid_alive(pid: Optional[int]) -> bool:
    """¿Existe ese proceso? En Windows NUNCA se usa ``os.kill(pid, 0)`` (allí terminaría el proceso)."""
    if not pid:
        return False
    pid = int(pid)
    ps = _psutil()
    if is_windows():
        if ps is not None:
            try:
                p = ps.Process(pid)
                return p.is_running() and p.status() != getattr(ps, "STATUS_ZOMBIE", "zombie")
            except Exception:
                return False
        try:
            return _win_pid_alive(pid)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def process_cmdline(pid: int) -> str:
    """Línea de comandos de un proceso ('' si no se puede leer). Linux: /proc · macOS: ps · Windows: psutil o PowerShell."""
    pid = int(pid)
    ps = _psutil()
    if ps is not None:
        try:
            return " ".join(ps.Process(pid).cmdline()).strip()
        except Exception:
            if is_windows():
                return ""
    if is_windows():
        try:
            r = subprocess.run(["powershell", "-NoProfile", "-Command",
                                f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                               capture_output=True, text=True, timeout=6, creationflags=CREATE_NO_WINDOW)
            return (r.stdout or "").strip()
        except Exception:
            return ""
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError:
        pass
    try:  # macOS/BSD: sin /proc
        r = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, timeout=3)
        return r.stdout.strip()
    except Exception:
        return ""


def terminate(pid: int) -> None:
    """Pide al proceso que termine. POSIX: SIGTERM. Windows: TerminateProcess (psutil o ``taskkill``).

    Lanza ProcessLookupError si ya no existe y PermissionError si no es nuestro."""
    pid = int(pid)
    if not is_windows():
        import signal

        os.kill(pid, signal.SIGTERM)
        return
    ps = _psutil()
    if ps is not None:
        try:
            ps.Process(pid).terminate()
            return
        except ps.NoSuchProcess as e:
            raise ProcessLookupError(pid) from e
        except ps.AccessDenied as e:
            raise PermissionError(str(e)) from e
    r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=10,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode != 0:
        out = (r.stdout or "") + (r.stderr or "")
        if "not found" in out.lower() or "no se encontr" in out.lower() or r.returncode == 128:
            raise ProcessLookupError(pid)
        raise PermissionError(out.strip()[:200] or f"taskkill {r.returncode}")


def detached_popen_kwargs() -> Dict[str, Any]:
    """Argumentos de ``subprocess.Popen`` para lanzar un proceso que siga vivo aunque se cierre el panel."""
    if is_windows():
        return {"creationflags": DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, "close_fds": True}
    return {"start_new_session": True, "close_fds": True}


def venv_python(root: Path) -> Path:
    """Python del entorno virtual del proyecto (.venv/Scripts/python.exe en Windows, .venv/bin/python en el resto)."""
    root = Path(root)
    cands = [root / ".venv" / "Scripts" / "python.exe", root / ".venv" / "bin" / "python"] if is_windows() else \
            [root / ".venv" / "bin" / "python", root / ".venv" / "Scripts" / "python.exe"]
    for c in cands:
        if c.is_file():
            return c
    return Path(sys.executable)


def module_command(module: str, root: Optional[Path] = None) -> List[str]:
    """``[python, -m, module]`` con el Python del venv del proyecto si existe (si no, el que ejecuta el panel)."""
    py = venv_python(root) if root is not None else Path(sys.executable)
    return [str(py), "-m", module]


def panel_command() -> str:
    """Cómo abrir el panel en este sistema (para los mensajes)."""
    return r".\caro5m.cmd" if is_windows() else "./start.sh"


def bot_command() -> str:
    """Cómo arrancar el bot sin panel en este sistema (para los mensajes)."""
    return r".\run-bot.cmd" if is_windows() else "./scripts/run-bot.sh"


# ── ficheros ───────────────────────────────────────────────────────
def replace(src: Any, dst: Any, retries: int = 20, delay: float = 0.05, sleep: Any = time.sleep) -> None:
    """``os.replace`` atómico. En Windows reintenta si otro proceso (p. ej. el panel) tiene el destino abierto."""
    last: Optional[BaseException] = None
    for i in range(max(1, retries)):
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:  # Windows: WinError 5/32 mientras un lector tiene el fichero abierto
            last = e
            if not is_windows() or i == retries - 1:
                break
            sleep(delay)
    assert last is not None
    raise last


def chmod_private(path: Any, mode: int = 0o600) -> bool:
    """Permisos privados (0600/0700). En Windows no hay permisos POSIX: no hace nada y devuelve False
    (el .env queda protegido por los permisos de tu carpeta de usuario)."""
    if is_windows():
        return False
    try:
        os.chmod(path, mode)
        return True
    except OSError:
        return False


def fchmod_private(fd: int, mode: int) -> None:
    if is_windows() or not hasattr(os, "fchmod"):
        return
    os.fchmod(fd, mode)


def permissions_meaningful() -> bool:
    """¿Tiene sentido comprobar 0600 en este sistema? (no en Windows)."""
    return not is_windows()


def default_shm_dir(data_dir: Path) -> Path:
    """Carpeta compartida bot <-> PTB: /dev/shm en Linux (RAM); en macOS/Windows (o sin /dev/shm) ``data/shm``."""
    if not is_windows():
        dev_shm = Path("/dev/shm")
        if dev_shm.is_dir() and os.access(dev_shm, os.W_OK):
            return dev_shm
    return Path(data_dir) / "shm"


def ensure_utf8_stdio() -> None:
    """Consola de Windows (cp1252/cp850): que ✔, ñ, €… no rompan el bot. En Linux/macOS no cambia nada."""
    if not is_windows():
        return
    for s in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(s, "encoding", "") or "").lower().replace("-", "")
            if enc != "utf8" and hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ── listado de procesos (lo usa caro5m.procs para encontrar el bot/PTB de esta carpeta) ──────────────────────────
def _match(cmd: Any, words: Any) -> bool:
    txt = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd or "")
    return any(w in txt for w in words)


def _linux_extra(pid: int) -> Dict[str, Any]:
    """cwd, entorno, cgroup y ppid de /proc (lo que se pueda leer: de otros usuarios no se lee el entorno)."""
    d = Path(f"/proc/{int(pid)}")
    out: Dict[str, Any] = {"cwd": None, "environ": None, "cgroup": "", "ppid": None}
    try:
        out["cwd"] = os.readlink(d / "cwd")
    except OSError:
        pass
    try:
        env: Dict[str, str] = {}
        for kv in (d / "environ").read_bytes().split(b"\0"):
            if b"=" in kv:
                k, v = kv.split(b"=", 1)
                env[k.decode("utf-8", "replace")] = v.decode("utf-8", "replace")
        out["environ"] = env
    except OSError:
        pass
    try:
        out["cgroup"] = (d / "cgroup").read_text(errors="replace")
    except OSError:
        pass
    try:
        out["ppid"] = int((d / "stat").read_text(errors="replace").rsplit(")", 1)[1].split()[1])
    except Exception:
        pass
    return out


def _scan_psutil(ps: Any, words: Any) -> List[Dict[str, Any]]:
    res: List[Dict[str, Any]] = []
    for p in ps.process_iter():
        try:
            cmd = p.cmdline()
        except Exception:
            continue
        if not cmd or not _match(cmd, words):
            continue
        d: Dict[str, Any] = {"pid": int(p.pid), "cmdline": list(cmd), "cwd": None, "environ": None, "cgroup": "", "ppid": None}
        for key, fn in (("cwd", p.cwd), ("environ", p.environ), ("ppid", p.ppid)):
            try:
                v = fn()
                d[key] = dict(v) if key == "environ" else v
            except Exception:
                pass
        if sys.platform.startswith("linux"):
            try:
                d["cgroup"] = Path(f"/proc/{p.pid}/cgroup").read_text(errors="replace")
            except OSError:
                pass
        res.append(d)
    return res


def _scan_proc(words: Any, proc_dir: str = "/proc") -> List[Dict[str, Any]]:
    res: List[Dict[str, Any]] = []
    bwords = [w.encode() for w in words]
    for d in Path(proc_dir).iterdir():
        if not d.name.isdigit():
            continue
        try:
            raw = (d / "cmdline").read_bytes()
        except OSError:
            continue
        if not any(w in raw for w in bwords):
            continue
        cmd = [x.decode("utf-8", "replace") for x in raw.split(b"\0") if x]
        res.append({"pid": int(d.name), "cmdline": cmd, **_linux_extra(int(d.name))})
    return res


def _split(cmd: str) -> List[str]:
    import shlex

    try:
        return shlex.split(cmd, posix=not is_windows())
    except ValueError:
        return cmd.split()


def _scan_ps(words: Any) -> List[Dict[str, Any]]:
    """macOS/BSD: ``ps`` para la lista y ``lsof`` para la carpeta de trabajo (el entorno de otros procesos no se lee)."""
    res: List[Dict[str, Any]] = []
    try:
        r = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="], capture_output=True, text=True, timeout=5)
    except Exception:
        return res
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not _match(parts[2], words):
            continue
        cwd = None
        try:
            lr = subprocess.run(["lsof", "-a", "-p", parts[0], "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=3)
            for ln in lr.stdout.splitlines():
                if ln.startswith("n"):
                    cwd = ln[1:]
        except Exception:
            pass
        res.append({"pid": int(parts[0]), "cmdline": _split(parts[2]), "cwd": cwd, "environ": None, "cgroup": "",
                    "ppid": int(parts[1]) if parts[1].isdigit() else None})
    return res


def _scan_powershell(words: Any) -> List[Dict[str, Any]]:
    """Windows sin psutil: CIM (sin cwd ni entorno: se identifica por la ruta del .venv/script en la línea de comandos)."""
    res: List[Dict[str, Any]] = []
    pat = "|".join(words)
    ps_cmd = (f"Get-CimInstance Win32_Process | Where-Object {{ $_.CommandLine -match '{pat}' }} | "
              "ForEach-Object { \"$($_.ProcessId)`t$($_.ParentProcessId)`t$($_.CommandLine)\" }")
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps_cmd], capture_output=True, text=True, timeout=15,
                           creationflags=CREATE_NO_WINDOW)
    except Exception:
        return res
    for line in (r.stdout or "").splitlines():
        parts = line.strip().split("\t", 2)
        if len(parts) == 3 and parts[0].isdigit():
            res.append({"pid": int(parts[0]), "cmdline": _split(parts[2]), "cwd": None, "environ": None, "cgroup": "",
                        "ppid": int(parts[1]) if parts[1].isdigit() else None})
    return res


def scan_processes(words: Any = ("caro5m",)) -> List[Dict[str, Any]]:
    """Procesos cuya línea de comandos contiene alguna de ``words``: {pid, cmdline[], cwd, environ, cgroup, ppid}.
    psutil (si está) · /proc (Linux) · ps+lsof (macOS) · PowerShell CIM (Windows sin psutil). Nunca falla."""
    ps = _psutil()
    if ps is not None:
        try:
            return _scan_psutil(ps, words)
        except Exception:
            pass
    try:
        if is_windows():
            return _scan_powershell(words)
        if Path("/proc/self/cmdline").exists():
            return _scan_proc(words)
        return _scan_ps(words)
    except Exception:
        return []


def process_snapshot(pid: int) -> Optional[Dict[str, Any]]:
    """Datos de un pid concreto (None si no vive o no se puede leer su línea de comandos)."""
    if not pid_alive(pid):
        return None
    pid = int(pid)
    if not is_windows() and Path(f"/proc/{pid}/cmdline").exists():
        try:
            cmd = [x.decode("utf-8", "replace") for x in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0") if x]
        except OSError:
            cmd = []
        if not cmd:
            return None
        return {"pid": pid, "cmdline": cmd, **_linux_extra(pid)}
    line = process_cmdline(pid)
    if not line:
        return None
    d: Dict[str, Any] = {"pid": pid, "cmdline": _split(line), "cwd": None, "environ": None, "cgroup": "", "ppid": None}
    ps = _psutil()
    if ps is not None:
        try:
            pr = ps.Process(pid)
            d["cmdline"] = pr.cmdline() or d["cmdline"]
            d["cwd"] = pr.cwd()
            d["environ"] = dict(pr.environ())
        except Exception:
            pass
    return d


def is_zombie(pid: int) -> bool:
    """Linux: proceso terminado pendiente de recoger (cuenta como parado)."""
    try:
        return Path(f"/proc/{int(pid)}/stat").read_text(errors="replace").rsplit(")", 1)[1].split()[0] == "Z"
    except (OSError, IndexError):
        return False


def child_pids(pid: int) -> List[int]:
    """Hijos (recursivo) de un proceso: psutil o /proc. [] si no se puede saber."""
    ps = _psutil()
    if ps is not None:
        try:
            return [c.pid for c in ps.Process(int(pid)).children(recursive=True)]
        except Exception:
            return []
    if is_windows() or not Path("/proc/self").exists():
        return []
    kids: Dict[int, List[int]] = {}
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                pp = int((d / "stat").read_text(errors="replace").rsplit(")", 1)[1].split()[1])
                kids.setdefault(pp, []).append(int(d.name))
            except Exception:
                pass
    out: List[int] = []
    todo = [int(pid)]
    while todo:
        for c in kids.get(todo.pop(), []):
            if c not in out and c != pid:
                out.append(c)
                todo.append(c)
    return out


def force_kill(pid: int) -> None:
    """Último recurso: SIGKILL (POSIX) · TerminateProcess/taskkill /F (Windows)."""
    pid = int(pid)
    if not is_windows():
        import signal

        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        return
    ps = _psutil()
    if ps is not None:
        try:
            ps.Process(pid).kill()
            return
        except ps.NoSuchProcess as e:
            raise ProcessLookupError(pid) from e
        except ps.AccessDenied as e:
            raise PermissionError(str(e)) from e
    r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, timeout=10,
                       creationflags=CREATE_NO_WINDOW)
    if r.returncode not in (0, 128):
        raise PermissionError((r.stdout or r.stderr or "").strip()[:200])


# ── señales (salida limpia) ────────────────────────────────────────
def install_signal_handlers(handler: Any, names: Any = ("SIGTERM", "SIGHUP", "SIGBREAK")) -> List[str]:
    """Registra ``handler(signum, frame)`` para las señales que existan en este sistema (SIGHUP no existe en Windows;
    SIGBREAK solo en Windows). Devuelve las registradas. Fuera del hilo principal no registra nada."""
    import signal

    done: List[str] = []
    for name in names:
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
            done.append(name)
        except (ValueError, OSError):
            pass
    return done


def signal_name(signum: int) -> str:
    import signal

    try:
        return signal.Signals(signum).name
    except Exception:
        return str(signum)
