"""v1.2.5: B/R/Q paran CUALQUIER bot/PTB de esta carpeta (viejo, lanzado a mano o duplicado; nunca de otra carpeta),
aviso de versión distinta con R, salida limpia del panel (Q, Ctrl+C, SIGHUP/SIGTERM, consola de Windows simulada),
instaladores que paran lo viejo antes de actualizar, y TP libre hasta 1,00 con tope de precio 0,99."""
import asyncio
import os
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from caro5m import __version__, compat, config, launcher, procs, shutdown, status
from caro5m.tui import render
from caro5m.tui.app import ConfirmScreen
from test_bot_loop import run_script, state_dry, world  # noqa: F401  (fixture reutilizada)
from test_tui import run, svg_text, with_app, write_demo

ROOT = Path(__file__).resolve().parent.parent
LINUX = sys.platform.startswith("linux")


def raw(pid, cmd, cwd=None, home=None, env=True, cgroup="", ppid=None):
    e = ({"CARO5M_HOME": home} if home else {}) if env else None
    return procs.RawProc(pid, cmd, cwd, e, cgroup, ppid)


BOT = ["/x/.venv/bin/python", "-m", "caro5m.bot"]


# ── identificación ──
def test_kind_of_only_real_bot_and_ptb_commands():
    assert procs.kind_of(["python3", "-m", "caro5m.bot"]) == "bot"
    assert procs.kind_of(["python", "-mcaro5m.ptb"]) == "ptb"
    assert procs.kind_of(["bash", "/p/scripts/run-bot.sh"]) == "bot"
    assert procs.kind_of(["/p/scripts/run-ptb.sh"]) == "ptb"
    assert procs.kind_of(["python -m caro5m.bot", "30"]) == "bot"  # argv[0] con espacios (exec -a)
    assert procs.kind_of([r"C:\p\.venv\Scripts\python.exe", "-m", "caro5m.bot"]) == "bot"
    for other in (["tail", "-f", "caro5m.bot.log"], ["python", "-m", "caro5m"], ["python", "-m", "caro5m.tui"],
                  ["grep", "caro5m.bot"], ["vim", "caro5m/bot.py"]):
        assert procs.kind_of(other) is None, other


def test_classify_never_takes_a_process_of_another_folder(tmp_path):
    me, other = tmp_path / "caro5m", tmp_path / "otro"
    me.mkdir(), other.mkdir()
    raws = [
        raw(101, BOT, cwd="/", home=str(me)),                                   # nuestro por CARO5M_HOME
        raw(102, BOT, cwd=str(me), home=str(other)),                           # cwd aquí pero CARO5M_HOME de otra carpeta: NO
        raw(103, BOT, cwd=str(me), env=False),                                  # entorno ilegible, cwd aquí: sí
        raw(104, [str(me / ".venv/bin/python"), "-m", "caro5m.bot"], env=False),  # solo la ruta: sí
        raw(105, [str(other / ".venv/bin/python"), "-m", "caro5m.bot"], env=False),  # ruta de otra carpeta: no
        raw(106, ["python", "-m", "caro5m.bot"], cwd=str(other), env=False),   # otra carpeta: no
        raw(107, ["python", "-m", "caro5m.ptb"], cwd="/tmp", home=str(me)),    # PTB nuestro
        raw(108, ["tail", "-f", str(me / "data/caro5m.log")], cwd=str(me)),    # no es el bot
        raw(109, BOT, home=str(me), cgroup="0::/system.slice/caro5m-bot.service"),  # servicio systemd
        raw(110, BOT, home=str(me) + "-copia"),                                # prefijo parecido: no
    ]
    got = {p.pid: p for p in procs.classify(raws, me, exclude=[os.getpid()])}
    assert set(got) == {101, 103, 104, 107, 109}
    assert got[101].why == "CARO5M_HOME" and got[103].why == "cwd" and got[104].why == "ruta"
    assert got[107].kind == "ptb" and got[109].systemd and not got[101].systemd


def test_discover_uses_pid_files_and_finds_duplicates(tmp_path):
    raws = [raw(201, BOT, home=str(tmp_path)), raw(202, ["bash", "/elsewhere/run-bot.sh"], env=False),
            raw(203, BOT, home=str(tmp_path)), raw(204, ["sleep", "30"], env=False)]
    found = procs.discover(tmp_path, known_pids=[202, 204, None], scanner=lambda: raws)
    assert [p.pid for p in found] == [201, 202, 203]                      # 202 por data/bot.pid; 204 no es el bot
    assert next(p for p in found if p.pid == 202).why == "fichero de pid"


def test_psutil_scan_is_used_when_available(monkeypatch, tmp_path):
    class P:
        def __init__(self, pid, cmd, cwd, env):
            self.pid, self._c, self._w, self._e = pid, cmd, cwd, env

        def cmdline(self):
            return self._c

        def cwd(self):
            return self._w

        def environ(self):
            if self._e is None:
                raise PermissionError("ajeno")
            return self._e

        def ppid(self):
            return 1

    fake = types.SimpleNamespace(process_iter=lambda: [
        P(301, BOT, "/", {"CARO5M_HOME": str(tmp_path)}),
        P(302, BOT, str(tmp_path.parent), {"CARO5M_HOME": str(tmp_path.parent / "otro")}),
        P(303, ["bash"], "/", {}),
    ])
    monkeypatch.setattr(compat, "_psutil", lambda: fake)
    found = procs.discover(tmp_path)
    assert [p.pid for p in found] == [301]


def test_windows_powershell_scan_parses_cim_output(monkeypatch):
    monkeypatch.setattr(compat, "is_windows", lambda: True)
    monkeypatch.setattr(compat, "_psutil", lambda: None)
    out = ("4100\t900\tC:\\caro5m\\.venv\\Scripts\\python.exe -m caro5m.bot\r\n"
           "4200\t900\t\"C:\\caro5m\\.venv\\Scripts\\python.exe\" -m caro5m.ptb\r\n")
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return types.SimpleNamespace(stdout=out, returncode=0)

    monkeypatch.setattr(compat.subprocess, "run", fake_run)
    got = compat.scan_processes(procs.WORDS)
    assert seen["cmd"][0] == "powershell" and "Win32_Process" in seen["cmd"][-1]
    assert [(d["pid"], procs.kind_of(d["cmdline"])) for d in got] == [(4100, "bot"), (4200, "ptb")]


# ── parar ──
def test_stop_terms_then_kills_stubborn_skips_systemd_and_children():
    alive = {1: True, 2: True, 3: True, 9: True}
    stubborn = {2}
    log = []
    clock = {"t": 0.0}

    def term(pid):
        log.append(("TERM", pid))
        if pid not in stubborn:
            alive[pid] = False

    def kill(pid):
        log.append(("KILL", pid))
        alive[pid] = False

    def sleep(s):
        clock["t"] += s

    plist = [procs.ProcInfo(1, "bot", BOT), procs.ProcInfo(2, "bot", BOT), procs.ProcInfo(3, "bot", BOT, systemd=True)]
    rep = procs.stop(plist, wait=8, terminate=term, kill=kill, alive=lambda p: alive.get(p, False),
                     children=lambda p: [9] if p == 1 else [], sleep=sleep, clock=lambda: clock["t"])
    assert ("TERM", 1) in log and ("TERM", 2) in log and ("TERM", 9) in log and ("TERM", 3) not in log
    assert rep.killed == [2] and 7.9 <= clock["t"] <= 8.5                   # SIGKILL solo tras ~8 s
    assert rep.bots == [1, 2] and [p.pid for p in rep.skipped_systemd] == [3]
    s = rep.summary()
    assert "Había 2 bots" in s and "Forzado a cerrar" in s and "systemctl stop" in s


def _fake_bot(home, extra_env=None, ignore_term=False, kind="bot"):
    env = dict(os.environ, CARO5M_HOME=str(home), **(extra_env or {}))
    trap = "trap '' TERM; " if ignore_term else ""
    return subprocess.Popen(["bash", "-c", f"{trap}exec -a 'python -m caro5m.{kind}' sleep 60"], env=env, cwd=str(home))


@pytest.mark.skipif(not LINUX, reason="procesos reales: Linux")
def test_b_stops_an_old_bot_without_pid_file_and_duplicates_but_not_other_folder(isolated_env, tmp_path_factory, monkeypatch):
    monkeypatch.setattr(launcher, "systemd_state", lambda service=launcher.SERVICE: {"installed": False, "active": False, "enabled": False})
    other = tmp_path_factory.mktemp("otro_proyecto")
    a, b = _fake_bot(isolated_env), _fake_bot(isolated_env)           # viejo + duplicado, sin data/bot.pid
    foreign = _fake_bot(other)
    ptb = _fake_bot(isolated_env, kind="ptb")
    try:
        time.sleep(0.4)
        assert launcher.read_pid() is None
        found = launcher.project_processes()
        assert {p.pid for p in found} == {a.pid, b.pid, ptb.pid}
        with pytest.raises(launcher.LaunchError) as e:                 # no se arranca otro encima
            launcher.check_can_start(None, use_systemd=False)
        assert e.value.code == "orphan"
        pid = launcher.stop_bot(wait=5)
        assert pid in (a.pid, b.pid) and sorted(launcher.last_report.bots) == sorted([a.pid, b.pid])
        assert a.wait(timeout=5) == -signal.SIGTERM and b.wait(timeout=5) == -signal.SIGTERM
        assert foreign.poll() is None and ptb.poll() is None           # otra carpeta y PTB intactos
        assert launcher.stop_ptb(wait=5) == ptb.pid and ptb.wait(timeout=5) == -signal.SIGTERM
        assert foreign.poll() is None
    finally:
        for p in (a, b, foreign, ptb):
            if p.poll() is None:
                p.kill()
                p.wait()


@pytest.mark.skipif(not LINUX, reason="procesos reales: Linux")
def test_stubborn_bot_gets_sigkill_after_grace(isolated_env):
    p = _fake_bot(isolated_env, ignore_term=True)
    try:
        time.sleep(0.4)
        rep = launcher.stop_project(("bot",), wait=0.6)
        assert rep.killed == [p.pid] and p.wait(timeout=5) == -signal.SIGKILL
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()


@pytest.mark.skipif(not LINUX, reason="procesos reales: Linux")
def test_procs_cli_used_by_installer_stops_this_folder_only(isolated_env, tmp_path_factory):
    other = tmp_path_factory.mktemp("otro")
    mine, foreign = _fake_bot(isolated_env), _fake_bot(other)
    (isolated_env / "data").mkdir(exist_ok=True)
    (isolated_env / "data" / "bot.pid").write_text(str(mine.pid))
    try:
        time.sleep(0.4)
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        r = subprocess.run([sys.executable, "-m", "caro5m.procs", "--root", str(isolated_env), "--stop", "--yes"],
                           capture_output=True, text=True, env=env, timeout=30, stdin=subprocess.DEVNULL)
        assert r.returncode == 0, r.stderr
        assert f"bot pid {mine.pid}" in r.stdout and "Bot parado" in r.stdout
        assert mine.wait(timeout=5) == -signal.SIGTERM and foreign.poll() is None
        assert not (isolated_env / "data" / "bot.pid").exists()
    finally:
        for p in (mine, foreign):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_installers_stop_old_processes_before_installing():
    sh = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert sh.index("caro5m.procs") < sh.index('-m venv "$DIR/.venv"') and "--yes" in sh
    ps1 = (ROOT / "install.ps1").read_bytes()
    assert ps1.startswith(b"\xef\xbb\xbf")
    t = ps1.decode("utf-8")
    assert t.index("caro5m.procs") < t.index("-m venv") and "--yes" in t


def test_mark_stopped_makes_status_stale_immediately():
    st = write_demo(with_position=False)
    assert status.bot_running(status.read_status())
    status.mark_stopped()
    assert not status.bot_running(status.read_status())
    st["ts"] = time.time() + 1                                          # el bot vivo reescribe su estado sin la marca
    status.write_status(st)
    assert status.bot_running(status.read_status())


# ── panel: B con un bot no lanzado por el panel / duplicados ──
def _fake_procs(monkeypatch, plist):
    monkeypatch.setattr(launcher, "project_processes", lambda st=None, scanner=None: list(plist))


def test_b_offers_to_stop_bot_not_started_by_this_panel(monkeypatch):
    write_demo(with_position=False)
    _fake_procs(monkeypatch, [procs.ProcInfo(777, "bot", BOT), procs.ProcInfo(778, "bot", BOT)])
    calls = []
    monkeypatch.setattr(launcher, "stop_bot", lambda *a, **k: calls.append("stop") or 777)
    monkeypatch.setattr(launcher, "stop_ptb", lambda *a, **k: None)

    async def body(app, pilot):
        await pilot.pause(0.3)
        assert "Hay 2 bots de esta carpeta" in svg_text(app)
        await pilot.press("b")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        txt = svg_text(app)
        assert "Parar el bot (pid 777, 778)" in txt and "duplicados" in txt
        await pilot.press("s")
        await pilot.pause(0.5)

    run(with_app(body, size=(130, 46)))
    assert calls == ["stop"]


def test_b_on_stale_status_with_orphan_bot_offers_stop_not_start(monkeypatch):
    st = write_demo(with_position=False)
    st["ts"] = time.time() - 300
    status.write_status(st)
    _fake_procs(monkeypatch, [procs.ProcInfo(555, "bot", BOT)])

    async def body(app, pilot):
        await pilot.pause(0.3)
        assert "no actualiza su estado" in svg_text(app)
        await pilot.press("b")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen) and "Parar el bot (pid 555)" in svg_text(app)

    run(with_app(body, size=(130, 46)))


def test_b_with_systemd_bot_explains_instead_of_killing(monkeypatch):
    write_demo(with_position=False)
    _fake_procs(monkeypatch, [procs.ProcInfo(556, "bot", BOT, systemd=True)])

    async def body(app, pilot):
        await pilot.press("b")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ConfirmScreen)
        assert any("systemctl stop" in n.message for n in app._notifications)

    run(with_app(body))


# ── versión distinta ──
def test_version_banner_render():
    b = render.process_banner({"version": "1.2.2"}, True, "1.2.5", [])
    assert b is not None and "El bot en marcha es la versión 1.2.2; tienes instalada la 1.2.5. Pulsa R para reiniciarlo" in b.plain
    assert render.process_banner({"version": "1.2.5"}, True, "1.2.5", []) is None
    assert render.process_banner({"version": "1.2.2"}, False, "1.2.5", []) is None


def test_panel_shows_version_banner_and_r_restarts_keeping_live(monkeypatch):
    st = write_demo(with_position=False)
    st.update(version="1.2.2", mode="live")
    status.write_status(st)
    _fake_procs(monkeypatch, [procs.ProcInfo(4242, "bot", BOT)])
    seen = {}

    async def body(app, pilot):
        await pilot.pause(0.4)
        assert f"El bot en marcha es la versión 1.2.2; tienes instalada la {__version__}" in svg_text(app)
        assert any("versión 1.2.2" in n.message and "Pulsa R" in n.message for n in app._notifications)
        assert "[R] Reiniciar" in svg_text(app)
        monkeypatch.setattr(app, "_restart_worker", lambda was_live=False: seen.update(was_live=was_live))
        await pilot.press("r")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        txt = svg_text(app)
        assert "versión 1.2.2" in txt and "confirmación de modo REAL" in txt
        await pilot.press("s")
        await pilot.pause(0.3)

    run(with_app(body, size=(130, 46)))
    assert seen == {"was_live": True}


def test_restart_with_open_position_needs_explicit_danger_confirm(monkeypatch):
    st = write_demo(with_position=True)
    st.update(version="1.2.2")
    status.write_status(st)
    _fake_procs(monkeypatch, [procs.ProcInfo(4242, "bot", BOT)])
    seen = []

    async def body(app, pilot):
        await pilot.pause(0.3)
        monkeypatch.setattr(app, "_restart_worker", lambda was_live=False: seen.append(was_live))
        await pilot.press("r")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen) and app.screen._danger
        assert "posición abierta" in svg_text(app)
        await pilot.press("n")
        await pilot.pause(0.2)

    run(with_app(body, size=(130, 46)))
    assert seen == []


# ── salir del panel ──
def test_q_with_live_open_position_asks_danger_confirm(monkeypatch):
    st = write_demo(with_position=True)
    st.update(mode="live")
    status.write_status(st)
    _fake_procs(monkeypatch, [])
    calls = []
    monkeypatch.setattr(shutdown, "shutdown_project", lambda **k: calls.append(k))

    async def body(app, pilot):
        await pilot.pause(0.3)
        await pilot.press("q")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen) and app.screen._danger
        assert "posición abierta en REAL" in svg_text(app) and "sin gestionar" in svg_text(app)
        await pilot.press("n")
        await pilot.pause(0.2)
        assert calls == [] and not app._quitting

    run(with_app(body, size=(130, 46)))


def test_q_without_position_stops_everything_and_exits(monkeypatch):
    write_demo(with_position=False)
    _fake_procs(monkeypatch, [procs.ProcInfo(4242, "bot", BOT)])
    calls = []
    monkeypatch.setattr(shutdown, "shutdown_project", lambda **k: calls.append(k) or "Bot parado (pid 4242).")
    out = {}

    async def body(app, pilot):
        await pilot.pause(0.3)
        await pilot.press("q")
        await pilot.pause(0.5)
        out["notes"] = [n.message for n in app._notifications]
        out["exited"] = app._exit_renderables is not None or not app.is_running

    async def runner():
        from caro5m.tui.app import Caro5mApp

        app = Caro5mApp(first_run_wizard=False)
        async with app.run_test(size=(118, 40)) as pilot:
            await body(app, pilot)
        return app

    asyncio.run(runner())
    assert calls and calls[0].get("reason") == "Q"
    assert any("Cerrando bot y PTB" in n for n in out["notes"])


def test_keep_running_on_exit_skips_cleanup(monkeypatch, isolated_env):
    (isolated_env / "config").mkdir(exist_ok=True)
    (isolated_env / "config" / "settings.env").write_text("KEEP_RUNNING_ON_EXIT=1\n")
    assert shutdown.keep_running()
    shutdown.reset()
    called = []
    monkeypatch.setattr(launcher, "stop_project", lambda *a, **k: called.append(1))
    assert shutdown.shutdown_project() is None and called == []
    shutdown.reset()


def test_shutdown_project_is_idempotent_and_cleans_files(monkeypatch, isolated_env):
    shutdown.reset()
    found = [procs.ProcInfo(11, "bot", BOT), procs.ProcInfo(12, "ptb", ["python", "-m", "caro5m.ptb"])]
    monkeypatch.setattr(launcher, "project_processes", lambda st=None, scanner=None: found)
    stops = []

    def fake_stop(kinds=("bot", "ptb"), wait=8.0, include_systemd=False, sleep=None, found=None):
        stops.append((tuple(kinds), [p.pid for p in found]))
        r = procs.StopReport()
        r.stopped = list(found)
        return r

    monkeypatch.setattr(launcher, "stop_project", fake_stop)
    launcher.write_pid(11)
    launcher._write_ptb_pid(12)
    status.set_mode_request("live")
    status.request_sell([])
    status.set_paused(True)
    msg = shutdown.shutdown_project(reason="prueba")
    assert stops == [(("bot", "ptb"), [11, 12])] and "Bot parado (pid 11)" in msg and "PTB parado (pid 12)" in msg
    assert launcher.read_pid() is None and launcher.read_ptb_pid() is None
    assert status.get_mode_request() is None and not (config.control_dir() / "sell_now").exists()
    assert status.is_paused()                                            # la pausa es una preferencia: se respeta
    assert shutdown.shutdown_project() == msg and len(stops) == 1        # segunda llamada (atexit): no repite
    shutdown.reset()


def test_signal_handlers_sigterm_exits_app_sighup_exits_hard(monkeypatch):
    shutdown.reset()
    handlers = {}
    monkeypatch.setattr(compat, "install_signal_handlers",
                        lambda h, names=("SIGTERM",): [handlers.setdefault(n, h) and n for n in names] and list(names))
    monkeypatch.setattr(shutdown.atexit, "register", lambda *a, **k: None)
    calls, exits, hard = [], [], []
    monkeypatch.setattr(shutdown, "shutdown_project", lambda wait=8.0, reason="", force=False: calls.append(reason))
    monkeypatch.setattr(shutdown.os, "_exit", lambda code: hard.append(code))
    done = shutdown.install(exit_cb=lambda: exits.append(1))
    assert "atexit" in done and {"SIGTERM", "SIGHUP"} <= set(handlers)
    handlers["SIGTERM"](signal.SIGTERM, None)
    assert calls == ["SIGTERM"] and exits == [1] and hard == []
    handlers["SIGHUP"](getattr(signal, "SIGHUP", 1), None)
    assert hard == [0]                                                   # terminal cerrada: salir ya, tras parar todo
    shutdown.reset()


def test_windows_console_close_and_sigbreak_paths_simulated(monkeypatch):
    shutdown.reset()
    monkeypatch.setattr(compat, "is_windows", lambda: True)
    calls = []
    monkeypatch.setattr(shutdown, "shutdown_project", lambda wait=8.0, reason="", force=False: calls.append((wait, reason)))
    assert shutdown.console_event(shutdown.CTRL_CLOSE_EVENT) is True     # cierre de ventana: parar en < 5 s
    assert calls[-1][0] <= 3.0
    assert shutdown.console_event(shutdown.CTRL_C_EVENT) is False        # Ctrl+C: para y deja seguir la salida normal
    assert shutdown.console_event(99) is False

    registered = {}

    class K32:
        def SetConsoleCtrlHandler(self, cb, add):
            registered["cb"], registered["add"] = cb, add
            return 1

    cb = shutdown._install_console_handler(K32())
    assert registered["add"] is True and registered["cb"] is cb
    n = len(calls)
    assert registered["cb"](shutdown.CTRL_SHUTDOWN_EVENT) is True and len(calls) == n + 1
    # install() en Windows registra también SIGBREAK y SetConsoleCtrlHandler
    names = []
    monkeypatch.setattr(compat, "install_signal_handlers", lambda h, n=("SIGTERM",): names.extend(n) or list(n))
    monkeypatch.setattr(shutdown.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(shutdown, "_install_console_handler", lambda kernel32=None: "cb")
    done = shutdown.install()
    assert "SIGBREAK" in names and "SetConsoleCtrlHandler" in done
    shutdown.reset()


def test_bot_handles_sigterm_cleanly(bot, monkeypatch, capsys):
    got = {}
    monkeypatch.setattr(compat, "install_signal_handlers", lambda h, names=("SIGTERM", "SIGHUP", "SIGBREAK"): got.update(h=h) or [])
    bot._install_stop_signals()
    with pytest.raises(bot._Stop):
        got["h"](signal.SIGTERM, None)

    def boom():
        got["h"](signal.SIGTERM, None)

    monkeypatch.setattr(bot, "main", boom)
    monkeypatch.setattr(bot, "_install_stop_signals", lambda: None)
    bot.run()                                                            # sale sin excepción ni código de error
    assert "stopped by signal SIGTERM" in capsys.readouterr().out


def test_start_text_says_bot_stops_on_exit(monkeypatch):
    st = write_demo(with_position=False)
    st["ts"] = time.time() - 300
    status.write_status(st)
    _fake_procs(monkeypatch, [])
    monkeypatch.setattr(launcher, "check_can_start", lambda st=None, use_systemd=True: None)

    async def body(app, pilot):
        await pilot.press("b")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        assert "Al salir del panel (Q) se paran el bot y el PTB" in app.screen._text

    run(with_app(body, size=(130, 46)))


# ── TP hasta 1,00 y tope 0,99 ──
def test_tp_range_allows_up_to_one():
    spec = config.SPEC_BY_KEY["TP_OFFSET"]
    assert spec.minimum == 0.01 and spec.maximum == 1.0
    assert config.convert(spec, "0.75") == 0.75 and config.convert(spec, "1") == 1.0 and config.convert(spec, "0,55") == 0.55
    with pytest.raises(ValueError):
        config.convert(spec, "1.01")
    with pytest.raises(ValueError):
        config.convert(spec, "0")
    assert config.SPEC_BY_KEY["SL_OFFSET"].maximum == 0.99
    assert config.SPEC_BY_KEY["MIN_TOKEN"].minimum == 0.01 and config.SPEC_BY_KEY["MAX_TOKEN"].maximum == 0.99
    assert config.SPEC_BY_KEY["KEEP_RUNNING_ON_EXIT"].default == "0" and "KEEP_RUNNING_ON_EXIT" in config.EDITABLE_KEYS


def test_settings_nudge_tp_above_half(isolated_env, monkeypatch):
    from caro5m.tui.app import SettingsScreen

    (isolated_env / "config").mkdir(exist_ok=True)
    (isolated_env / "config" / "settings.env").write_text("TP_OFFSET=0.50\n")
    write_demo(with_position=False)

    async def body(app, pilot):
        app.push_screen(SettingsScreen())
        await pilot.pause(0.3)
        scr = app.screen
        monkeypatch.setattr(scr, "_current_key", lambda: "TP_OFFSET")
        scr.action_nudge(1)
        scr.action_nudge(1)
        await pilot.pause(0.1)

    run(with_app(body))
    assert config.load_settings()["TP_OFFSET"] == pytest.approx(0.52)


def test_tp_target_caps_at_099_and_orders_never_exceed_it(bot):
    assert bot.tp_target(0.60, 0.50) == 0.99 and bot.tp_target(0.60, 0.08) == pytest.approx(0.68)
    assert bot.tp_target(0.40, 1.0) == 0.99
    r = bot.place_fok(None, "tok", bot.SELL, 1.2, 2.0, dry=True)       # precio inválido -> 0.99
    assert float(r["takingAmount"]) == pytest.approx(2.0 * 0.99)
    assert bot.sl_target(0.30, 0.5) < 0.01                               # SL fuera de rango = nunca salta


def test_loop_with_tp_above_one_holds_then_sells_at_099(monkeypatch, capsys):
    monkeypatch.setenv("TP_OFFSET", "0.50")
    import importlib

    import caro5m.bot as b

    b = importlib.reload(b)
    (config.shm_dir()).mkdir(parents=True, exist_ok=True)
    b.WARNINGS.clear()
    w = world.__wrapped__(b, monkeypatch) if hasattr(world, "__wrapped__") else None
    if w is None:
        pytest.skip("fixture world no reutilizable")
    book = w["book"]
    seen = {}

    def after_entry():
        seen["pos"] = dict(state_dry()["positions"])
        status.set_paused(True)
        for c in b.COINS:
            book[f"{c}_up"] = (0.95, 0.97)          # entrada 0.60 + TP 0.50 = 1.10 -> objetivo 0.99: aún no

    def at_095():
        seen["still"] = dict(state_dry()["positions"])
        seen["status"] = status.read_status()
        for c in b.COINS:
            book[f"{c}_up"] = (0.99, 1.0)

    def after_tp():
        seen["trades"] = list(state_dry()["trades"])

    run_script(b, monkeypatch, w, [lambda: None, after_entry, at_095, after_tp])
    assert seen["pos"] and set(seen["still"]) == set(seen["pos"])
    p = next(iter(seen["status"]["positions"].values()))
    assert p["tp"] == 0.99 and p["tp_capped"] is True
    assert {t["result"] for t in seen["trades"]} == {"TP"}
    logtxt = capsys.readouterr().out
    assert "TP_CAP" in logtxt and "EXIT_TP" in logtxt and "tp=0.990" in logtxt
