"""Encontrar y parar TODOS los procesos de caro5m (bot y PTB) de ESTE proyecto, no solo el del fichero de pid.

Por qué: si actualizas el .zip con un bot viejo en marcha, o lo arrancaste a mano con ``run-bot.sh``, o el panel que lo
lanzó se cerró, ``data/bot.pid`` puede no existir o no coincidir. Aquí se buscan los procesos por su línea de comandos
(``-m caro5m.bot`` / ``-m caro5m.ptb`` / ``run-bot.sh`` / ``run-ptb.sh``) y se comprueba que son de esta carpeta:

1. si se puede leer su entorno y tiene ``CARO5M_HOME``: es nuestro solo si apunta a esta carpeta (decisivo);
2. si no, si su carpeta de trabajo (cwd) es esta carpeta;
3. si no se puede leer nada de lo anterior: si su línea de comandos contiene una ruta dentro de esta carpeta
   (el Python de ``.venv`` o el script de ``scripts/``).

Nunca se toca un proceso de otra carpeta/proyecto. Fuentes: ``psutil`` si está (Windows siempre), ``/proc`` en Linux,
``ps``/``lsof`` en macOS y PowerShell (CIM) en Windows sin psutil. Solo biblioteca estándar (lo usa también el
instalador antes de crear el venv: ``python -m caro5m.procs --root DIR --stop``).
"""

from __future__ import annotations

import os
import shlex
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from . import compat

BOT_SERVICE_CGROUP = "caro5m-bot.service"
PTB_SERVICE_CGROUP = "caro5m-ptb.service"


@dataclass
class ProcInfo:
    pid: int
    kind: str  # "bot" | "ptb"
    cmdline: List[str]
    cwd: Optional[str] = None
    home: Optional[str] = None  # CARO5M_HOME de su entorno (None si no se pudo leer o no lo tiene)
    systemd: bool = False  # lo gestiona el servicio systemd caro5m-bot/caro5m-ptb
    why: str = ""  # por qué se considera de este proyecto

    def short(self) -> str:
        return f"{self.kind} pid {self.pid}"


@dataclass
class RawProc:
    """Lo que se sabe de un proceso cualquiera (para poder simularlo en las pruebas)."""

    pid: int
    cmdline: List[str]
    cwd: Optional[str] = None
    environ: Optional[Dict[str, str]] = None
    cgroup: str = ""
    ppid: Optional[int] = None


@dataclass
class StopReport:
    stopped: List[ProcInfo] = field(default_factory=list)  # terminaron (con SIGTERM o, si no, con SIGKILL)
    killed: List[int] = field(default_factory=list)  # pids que hubo que matar a la fuerza
    skipped_systemd: List[ProcInfo] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)

    @property
    def bots(self) -> List[int]:
        return [p.pid for p in self.stopped if p.kind == "bot"]

    @property
    def ptbs(self) -> List[int]:
        return [p.pid for p in self.stopped if p.kind == "ptb"]

    def summary(self) -> str:
        parts: List[str] = []
        b, p = self.bots, self.ptbs
        if len(b) > 1:
            parts.append(f"Había {len(b)} bots de este proyecto en marcha (duplicados); parados todos (pid {', '.join(map(str, b))}).")
        elif b:
            parts.append(f"Bot parado (pid {b[0]}).")
        if p:
            parts.append(f"PTB parado (pid {', '.join(map(str, p))}).")
        if self.killed:
            parts.append(f"Forzado a cerrar (no respondía): pid {', '.join(map(str, self.killed))}.")
        if self.skipped_systemd:
            parts.append("Hay un servicio systemd del bot en marcha: no lo toco (para pararlo: «sudo systemctl stop caro5m-bot caro5m-ptb»).")
        if self.failed:
            parts.append("No se pudo parar: " + "; ".join(self.failed))
        return " ".join(parts) or "No había ningún bot ni PTB de este proyecto en marcha."


# ── identificación ─────────────────────────────────────────────────
def _norm(p: Any) -> str:
    try:
        s = str(Path(str(p)).expanduser().resolve())
    except Exception:
        s = str(p)
    if compat.is_windows():
        s = s.replace("/", "\\").rstrip("\\").lower()
    else:
        s = s.rstrip("/") or "/"
    return s


def _tokens(cmd: Any) -> List[str]:
    if isinstance(cmd, (list, tuple)):
        return [str(t) for t in cmd if str(t)]
    s = str(cmd or "").strip()
    if not s:
        return []
    try:
        return shlex.split(s, posix=not compat.is_windows())
    except ValueError:
        return s.split()


def kind_of(cmdline: Sequence[str]) -> Optional[str]:
    """'bot' / 'ptb' si la línea de comandos es la de nuestro bot/PTB; None si no (p. ej. ``tail caro5m.bot.log``)."""
    toks = [x.strip('"') for t in cmdline for x in (str(t).split() or [""])]  # argv[0] con espacios (exec -a) también
    for i, t in enumerate(toks):
        base = t.replace("\\", "/").rsplit("/", 1)[-1]
        if base in ("run-bot.sh", "run-bot.cmd"):
            return "bot"
        if base in ("run-ptb.sh", "run-ptb.cmd"):
            return "ptb"
        mod = None
        if t == "-m" and i + 1 < len(toks):
            mod = toks[i + 1]
        elif t.startswith("-m") and len(t) > 2 and not t.startswith("--"):
            mod = t[2:]
        if mod == "caro5m.bot":
            return "bot"
        if mod == "caro5m.ptb":
            return "ptb"
    return None


def belongs(raw: RawProc, root: Any) -> Optional[str]:
    """Motivo (texto) si el proceso es de la carpeta ``root``; None si no lo es o no se puede asegurar."""
    r = _norm(root)
    env = raw.environ
    if env is not None and env.get("CARO5M_HOME"):
        return "CARO5M_HOME" if _norm(env["CARO5M_HOME"]) == r else None  # decisivo: es de otra carpeta
    if raw.cwd:
        if _norm(raw.cwd) == r:
            return "cwd"
    sep = "\\" if compat.is_windows() else "/"
    for t in raw.cmdline:
        t = t.strip('"')
        if (sep in t or "/" in t) and (os.path.isabs(t) or (len(t) > 2 and t[1] == ":")):
            n = _norm(t)
            if n.startswith(r + sep):
                return "ruta"
    return None


def classify(raws: Iterable[RawProc], root: Any, exclude: Iterable[int] = ()) -> List[ProcInfo]:
    excl = {int(x) for x in exclude if x}
    out: List[ProcInfo] = []
    for raw in raws:
        if raw.pid in excl or raw.pid <= 1:
            continue
        k = kind_of(raw.cmdline)
        if not k:
            continue
        why = belongs(raw, root)
        if not why:
            continue
        cg = raw.cgroup or ""
        sysd = (BOT_SERVICE_CGROUP in cg) if k == "bot" else (PTB_SERVICE_CGROUP in cg)
        out.append(ProcInfo(raw.pid, k, list(raw.cmdline), raw.cwd, (raw.environ or {}).get("CARO5M_HOME"), sysd, why))
    out.sort(key=lambda p: p.pid)
    return out


# ── lectura de procesos del sistema (lo dependiente del SO está en compat) ──
WORDS = ("caro5m", "run-bot", "run-ptb")


def _raw(d: Dict[str, Any]) -> RawProc:
    return RawProc(int(d["pid"]), list(d.get("cmdline") or []), d.get("cwd"), d.get("environ"), d.get("cgroup") or "", d.get("ppid"))


def scan_raw() -> List[RawProc]:
    return [_raw(d) for d in compat.scan_processes(WORDS)]


def _raw_for_pid(pid: int) -> Optional[RawProc]:
    d = compat.process_snapshot(pid)
    return _raw(d) if d else None


def discover(root: Any, known_pids: Iterable[Optional[int]] = (), scanner: Optional[Callable[[], List[RawProc]]] = None,
             exclude: Iterable[int] = ()) -> List[ProcInfo]:
    """Todos los procesos bot/PTB de la carpeta ``root``.

    ``known_pids``: pids de status.json / data/bot.pid / data/ptb.pid. Están escritos en NUESTRA carpeta de datos, así que
    basta con que su línea de comandos sea la del bot/PTB (aunque no se pueda leer su cwd/entorno)."""
    excl = {os.getpid(), *(int(x) for x in exclude if x)}
    raws = (scanner or scan_raw)()
    found = {p.pid: p for p in classify(raws, root, excl)}
    by_pid = {r.pid: r for r in raws}
    for pid in {int(x) for x in known_pids if x}:
        if pid in found or pid in excl or pid <= 1:
            continue
        raw = by_pid.get(pid) or (None if scanner else _raw_for_pid(pid))
        if raw is None:
            continue
        k = kind_of(raw.cmdline)
        if not k:
            continue
        env = raw.environ or {}
        if env.get("CARO5M_HOME") and _norm(env["CARO5M_HOME"]) != _norm(root):
            continue  # pid reutilizado por un bot de otra carpeta
        cg = raw.cgroup or ""
        found[pid] = ProcInfo(pid, k, list(raw.cmdline), raw.cwd, env.get("CARO5M_HOME"),
                              (BOT_SERVICE_CGROUP if k == "bot" else PTB_SERVICE_CGROUP) in cg, "fichero de pid")
    # run-bot.sh (bash, antes del exec) con su python hijo: se para el padre y el hijo; ambos aparecen y está bien.
    return sorted(found.values(), key=lambda p: (p.kind, p.pid))


# ── parar ──────────────────────────────────────────────────────────
def descendants(pid: int, raws: Optional[List[RawProc]] = None) -> List[int]:
    """Hijos (recursivo) de un proceso. Con ``raws`` se calcula de esa lista (pruebas); si no, lo da compat."""
    if raws is None:
        return compat.child_pids(pid)
    kids: Dict[int, List[int]] = {}
    for r in raws:
        if r.ppid:
            kids.setdefault(r.ppid, []).append(r.pid)
    out: List[int] = []
    todo = [pid]
    while todo:
        cur = todo.pop()
        for c in kids.get(cur, []):
            if c not in out and c != pid:
                out.append(c)
                todo.append(c)
    return out


force_kill = compat.force_kill


def stop(procs: Sequence[ProcInfo], wait: float = 8.0, include_systemd: bool = False,
         terminate: Callable[[int], None] = compat.terminate, kill: Callable[[int], None] = compat.force_kill,
         alive: Callable[[int], bool] = compat.pid_alive, children: Callable[[int], List[int]] = descendants,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time,
         poll: Optional[Callable[[], None]] = None) -> StopReport:
    """SIGTERM a todos (y a sus hijos), espera hasta ``wait`` s y SIGKILL a los que sigan vivos."""
    rep = StopReport()
    targets: List[ProcInfo] = []
    for p in procs:
        if p.systemd and not include_systemd:
            rep.skipped_systemd.append(p)
        else:
            targets.append(p)
    extra: Dict[int, int] = {}  # hijo -> padre
    for p in targets:
        try:
            for c in children(p.pid):
                extra.setdefault(c, p.pid)
        except Exception:
            pass
    pids = [p.pid for p in targets] + [c for c in extra if c not in {p.pid for p in targets}]
    gone: set = set()
    for pid in pids:
        try:
            terminate(pid)
        except ProcessLookupError:
            gone.add(pid)
        except PermissionError as e:
            rep.failed.append(f"pid {pid}: sin permiso ({e})")
            gone.add(pid)
        except Exception as e:
            rep.failed.append(f"pid {pid}: {e}")
    end = clock() + max(0.0, wait)
    while True:
        if poll:
            try:
                poll()
            except Exception:
                pass
        left = [x for x in pids if x not in gone and alive(x)]
        if not left or clock() >= end:
            break
        sleep(0.2)
    for pid in [x for x in pids if x not in gone and alive(x)]:
        try:
            kill(pid)
            rep.killed.append(pid)
        except ProcessLookupError:
            pass
        except Exception as e:
            rep.failed.append(f"pid {pid}: {e}")
    failed_pids = {int(s.split()[1].rstrip(":")) for s in rep.failed if s.startswith("pid ")}
    rep.stopped = [p for p in targets if p.pid not in failed_pids]
    return rep


# ── CLI (instalador: python -m caro5m.procs --root DIR --stop [--yes]) ─────────────────────────────────────────
def _cli(argv: Optional[List[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m caro5m.procs", description="Lista o para los procesos bot/PTB de una carpeta de caro5m.")
    ap.add_argument("--root", default=os.environ.get("CARO5M_HOME") or str(Path(__file__).resolve().parent.parent))
    ap.add_argument("--stop", action="store_true", help="parar los que haya (pregunta si hay terminal, salvo --yes)")
    ap.add_argument("--yes", action="store_true", help="no preguntar")
    ap.add_argument("--include-systemd", action="store_true")
    a = ap.parse_args(argv)
    root = Path(a.root)
    known: List[int] = []
    for name in ("bot.pid", "ptb.pid"):
        try:
            known.append(int((root / "data" / name).read_text().strip()))
        except Exception:
            pass
    try:
        import json

        known.append(int(json.loads((root / "data" / "status.json").read_text(encoding="utf-8")).get("pid") or 0))
    except Exception:
        pass
    procs = discover(root, known)
    if not procs:
        print("caro5m: no hay ningún bot ni PTB de esta carpeta en marcha.")
        return 0
    for p in procs:
        print(f"caro5m: en marcha {p.short()}{' (servicio systemd)' if p.systemd else ''}: {' '.join(p.cmdline)[:120]}")
    if not a.stop:
        return 0
    if not a.yes and sys.stdin.isatty() and sys.stdout.isatty():
        try:
            ans = input("¿Los paro para que se use la versión nueva? [S/n] ").strip().lower()
        except EOFError:
            ans = ""
        if ans.startswith("n"):
            print("caro5m: no se para nada. Ojo: seguirá corriendo la versión anterior hasta que lo reinicies (panel: R).")
            return 0
    rep = stop(procs, include_systemd=a.include_systemd)
    for name in ("bot.pid", "ptb.pid"):
        try:
            (root / "data" / name).unlink()
        except OSError:
            pass
    print("caro5m: " + rep.summary())
    return 1 if rep.failed else 0


if __name__ == "__main__":
    sys.exit(_cli())
