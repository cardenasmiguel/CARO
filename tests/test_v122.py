"""v1.2.2: arrancar/parar el bot desde el panel (M y B), pid con guardas, log del bot en vivo y copiable, y pegado de proxy con varias líneas."""
import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest
from textual.widgets import TextArea

from caro5m import account, config, launcher, setuplog, status, wizard
from caro5m.tui.app import Caro5mApp, ConfirmScreen, LogScreen, ProxyInput, ProxyScreen
from test_tui import run, svg_text, with_app, write_demo

import demo_status

# Credenciales FALSAS de pruebas (nunca un proxy real).
U, PW, IP = "usr9x", "p4ss!w0rd", "10.77.88.99"
GOOD = f"{IP}:18080:{U}:{PW}"
GOOD_NORM = f"http://{U}:p4ss%21w0rd@{IP}:18080"


# ── proxy: varias líneas / espacios => el primero válido ──
@pytest.mark.parametrize("text", [
    f"{GOOD}\nsegunda linea\n",
    f"\n  {GOOD}  \n10.0.0.1:80\n",
    f"proxy:\n{GOOD}\n",                                  # una línea de texto antes del proxy
    f"basura sin sentido {GOOD} 10.0.0.1:80",             # separado por espacios
    f"{GOOD}, 10.0.0.1:80",
    f"'{GOOD}'\r\n10.0.0.2:81\r\n",
])
def test_proxy_paste_with_several_lines_takes_first_valid(text):
    assert account.parse_proxy(text) == GOOD_NORM
    assert wizard.validate_proxy(text).startswith(GOOD_NORM.split(",")[0])
    assert PW not in account.mask_proxy(text) and U not in account.mask_proxy(text)
    lvl, t = account.describe_proxy_input(text)
    assert lvl == "ok" and PW not in t and U not in t and "primera válida" in t


def test_proxy_paste_without_any_valid_line_still_errors_in_spanish():
    with pytest.raises(ValueError) as e:
        account.parse_proxy("hola\nque tal")
    assert "formato" in str(e.value).lower() or "puerto" in str(e.value).lower()
    with pytest.raises(ValueError):
        account.parse_proxy("   \n  ")
    assert account.describe_proxy_input("hola\nque tal")[0] == "fail"


def test_proxy_input_widget_paste_keeps_first_valid_line_only():
    async def body(app, pilot):
        app.push_screen(ProxyScreen())
        await pilot.pause(0.3)
        inp = app.screen.query_one("#proxy-input", ProxyInput)
        inp._on_paste(__import__("textual.events", fromlist=["Paste"]).Paste(f"basura\n{GOOD}\n{IP}:1"))
        assert inp.value == GOOD
        assert account.parse_proxy(inp.value) == GOOD_NORM

    run(with_app(body))


# ── lanzador ──
def fake_bot_script(tmp_path, body="sleep 60"):
    d = tmp_path / "proj" / "scripts"
    d.mkdir(parents=True)
    s = d / "run-bot.sh"
    s.write_text("#!/usr/bin/env bash\n"
                 f"echo \"$CARO5M_HOME|$CARO5M_LOG_TO_FILE\" > \"{tmp_path}/env.txt\"\n"
                 f"{body}\n")
    s.chmod(0o755)
    return s


def fresh_status_writer(tmp_path):
    """Cuerpo de script falso: escribe un status.json fresco y se queda vivo."""
    import json

    st = demo_status.build()
    st["ts"] = 0
    return (f"python3 - <<'PY' &\nimport json,time\nwhile True:\n"
            f"    d=json.load(open('{tmp_path}/tpl.json')); d['ts']=time.time()\n"
            f"    open('{config.status_path()}.tmp','w').write(json.dumps(d))\n"
            f"    import os; os.replace('{config.status_path()}.tmp','{config.status_path()}')\n"
            f"    time.sleep(0.3)\nPY\nwait\n"), json.dumps(st)


@pytest.fixture
def no_systemd(monkeypatch):
    monkeypatch.setattr(launcher, "systemd_state", lambda service=launcher.SERVICE: {"installed": False, "active": False, "enabled": False})


def kill_quiet(pid):
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def test_start_bot_is_detached_writes_pid_and_passes_env(isolated_env, no_systemd):
    script = fake_bot_script(isolated_env)
    pid = launcher.start_bot(script)
    try:
        assert launcher.read_pid() == pid and launcher.pid_path() == isolated_env / "data" / "bot.pid"
        assert os.getsid(pid) == pid != os.getsid(0)                  # sesión propia: sobrevive al panel
        time.sleep(0.4)
        assert (isolated_env / "env.txt").read_text().strip() == f"{isolated_env}|1"
        assert launcher.is_bot_process(pid)
    finally:
        kill_quiet(pid)
        launcher.reap()


def test_double_start_is_refused(isolated_env, no_systemd):
    script = fake_bot_script(isolated_env)
    pid = launcher.start_bot(script)
    try:
        with pytest.raises(launcher.LaunchError) as e:
            launcher.start_bot(script)
        assert e.value.code == "pid" and str(pid) in e.value.text
        # con el estado fresco tampoco
        st = demo_status.build()
        with pytest.raises(launcher.LaunchError) as e2:
            launcher.check_can_start(st, use_systemd=False)
        assert e2.value.code == "running"
    finally:
        kill_quiet(pid)
        launcher.reap()


def test_stale_pid_file_is_cleaned_and_start_allowed(isolated_env, no_systemd):
    launcher.write_pid(2 ** 22 + 12345)       # pid que no existe
    launcher.check_can_start(None, use_systemd=False)
    assert launcher.read_pid() is None


def test_systemd_service_is_preferred(isolated_env, monkeypatch):
    monkeypatch.setattr(launcher, "systemd_state", lambda service=launcher.SERVICE: {"installed": True, "active": True, "enabled": True})
    with pytest.raises(launcher.LaunchError) as e:
        launcher.check_can_start(None)
    assert e.value.code == "systemd" and "caro5m-bot" in e.value.text
    monkeypatch.setattr(launcher, "systemd_state", lambda service=launcher.SERVICE: {"installed": True, "active": False, "enabled": True})
    with pytest.raises(launcher.LaunchError) as e:
        launcher.check_can_start(None)
    assert "systemctl start caro5m-bot" in e.value.text


def test_systemd_state_never_fails_without_systemctl(monkeypatch):
    monkeypatch.setattr(launcher.shutil, "which", lambda x: None)
    assert launcher.systemd_state() == {"installed": False, "active": False, "enabled": False}

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("systemctl", 3)

    monkeypatch.setattr(launcher.shutil, "which", lambda x: "/bin/systemctl")
    monkeypatch.setattr(launcher.subprocess, "run", boom)
    assert launcher.systemd_state()["active"] is False


def test_wait_until_fresh_and_early_death(isolated_env, no_systemd):
    body, tpl = fresh_status_writer(isolated_env)
    (isolated_env / "tpl.json").write_text(tpl)
    script = fake_bot_script(isolated_env, body)
    since = time.time()
    pid = launcher.start_bot(script)
    try:
        assert launcher.wait_until_fresh(pid, since, timeout=15)
    finally:
        kill_quiet(pid)
        launcher.reap()
    # script que muere enseguida: no espera los 30 s
    (isolated_env / "d2").mkdir()
    script2 = fake_bot_script(isolated_env / "d2", "echo boom >&2; exit 3")
    launcher.clear_pid()
    config.status_path().unlink()
    since = time.time()
    pid2 = launcher.start_bot(script2)
    t0 = time.time()
    assert not launcher.wait_until_fresh(pid2, since, timeout=20)
    assert time.time() - t0 < 10
    assert "boom" in launcher.log_tail()
    launcher.reap()


def test_stop_bot_only_kills_our_process(isolated_env, no_systemd):
    script = fake_bot_script(isolated_env)
    pid = launcher.start_bot(script)
    time.sleep(0.3)
    assert launcher.stop_bot(wait=5) == pid
    assert not launcher.is_bot_process(pid) and launcher.read_pid() is None
    launcher.reap()
    # un pid ajeno (no es el bot) se respeta aunque esté en bot.pid
    other = subprocess.Popen(["sleep", "30"])
    try:
        launcher.write_pid(other.pid)
        with pytest.raises(launcher.LaunchError) as e:
            launcher.stop_bot(wait=1)
        assert e.value.code == "notbot" and other.poll() is None
        assert launcher.read_pid() is None
        with pytest.raises(launcher.LaunchError) as e2:
            launcher.stop_bot(wait=1)
        assert e2.value.code == "nopid"
    finally:
        other.kill()
        other.wait()


def test_log_tail_is_masked(isolated_env):
    config.log_path().parent.mkdir(parents=True, exist_ok=True)
    config.log_path().write_text("a\nproxy http://usr9x:p4ss@10.77.88.99:18080 fallo\n")
    t = launcher.log_tail()
    assert "p4ss" not in t and "usr9x" not in t and "fallo" in t


# ── panel ──
def stub_launcher(monkeypatch, started):
    def fake_start(script=None, use_systemd=True, st=None):
        started.append(1)
        status.write_status(demo_status.build(with_position=False))
        launcher.write_pid(4242)
        return 4242

    monkeypatch.setattr(launcher, "check_can_start", lambda st=None, use_systemd=True: None)
    monkeypatch.setattr(launcher, "start_bot", fake_start)
    monkeypatch.setattr(launcher, "wait_until_fresh", lambda *a, **k: True)


def test_m_with_bot_stopped_offers_to_start_in_prueba_mode_then_live_flow(monkeypatch):
    started = []
    stub_launcher(monkeypatch, started)
    st = demo_status.build(with_position=False)
    st["ts"] = time.time() - 300                       # bot parado
    status.write_status(st)

    async def body(app, pilot):
        assert not app.running
        await pilot.press("m")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        s = svg_text(app)
        assert "PRUEBA" in s and "arranco" in s and "REAL" in s
        await pilot.press("n")
        await pilot.pause(0.2)
        assert not started and status.get_mode_request() is None
        await pilot.press("m")
        await pilot.pause(0.2)
        await pilot.press("s")                         # sí, arráncalo
        await pilot.pause(1.5)
        assert started
        # tras arrancar continúa el flujo normal de M: diálogo rojo de modo REAL; aún sin petición de modo
        assert isinstance(app.screen, ConfirmScreen)
        assert "Pasar a modo REAL" in svg_text(app) and status.get_mode_request() is None
        await pilot.press("s")
        await pilot.pause(0.2)
        assert status.get_mode_request() == "live"

    run(with_app(body))


def test_b_starts_without_mode_flow_and_footer_shows_keys(monkeypatch):
    started = []
    stub_launcher(monkeypatch, started)

    async def body(app, pilot):
        s = svg_text(app)
        assert "[B] Arrancar bot" in s and "[L] Log del bot" in s
        await pilot.press("b")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen) and "PRUEBA" in svg_text(app)
        await pilot.press("s")
        await pilot.pause(1.5)
        assert started and not isinstance(app.screen, ConfirmScreen) and status.get_mode_request() is None
        assert "Parar bot" in svg_text(app)

    run(with_app(body))


def test_start_failure_shows_log_tail_and_pointers(monkeypatch):
    monkeypatch.setattr(launcher, "check_can_start", lambda st=None, use_systemd=True: None)
    monkeypatch.setattr(launcher, "start_bot", lambda *a, **k: 4242)
    monkeypatch.setattr(launcher, "wait_until_fresh", lambda *a, **k: False)
    config.log_path().parent.mkdir(parents=True, exist_ok=True)
    config.log_path().write_text("[t] FATAL algo se rompió con http://usr9x:p4ss@10.77.88.99:1\n")
    notes = []

    async def body(app, pilot):
        orig = app.notify
        app.notify = lambda msg, **k: (notes.append(str(msg)), orig(msg, **k))[1]
        await pilot.press("b")
        await pilot.pause(0.2)
        await pilot.press("s")
        await pilot.pause(1.5)
        joined = "\n".join(notes)
        assert "no llegó a ponerse en marcha" in joined and "FATAL" in joined and "[L]" in joined and "[E]" in joined
        assert "p4ss" not in joined and "usr9x" not in joined
        assert "no llegó a arrancar" in setuplog.report("setup", 50)

    run(with_app(body))


def test_systemd_installed_is_reported_instead_of_confirm(monkeypatch):
    monkeypatch.setattr(launcher, "systemd_state", lambda service=launcher.SERVICE: {"installed": True, "active": False, "enabled": True})

    async def body(app, pilot):
        await pilot.press("m")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ConfirmScreen)

    run(with_app(body))


def test_b_stops_only_a_bot_started_by_the_panel(isolated_env, monkeypatch):
    write_demo(with_position=False)

    async def body(app, pilot):
        await pilot.press("b")                          # corre pero sin data/bot.pid: no se ofrece parar
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ConfirmScreen)

    run(with_app(body))

    sleeper = subprocess.Popen(["bash", "-c", "exec -a 'python -m caro5m.bot' sleep 30"])
    try:
        time.sleep(0.3)
        launcher.write_pid(sleeper.pid)
        assert launcher.is_bot_process(sleeper.pid)
        write_demo(with_position=False)

        async def body2(app, pilot):
            await pilot.press("b")
            await pilot.pause(0.2)
            assert isinstance(app.screen, ConfirmScreen) and "Parar el bot" in svg_text(app)
            await pilot.press("s")
            await pilot.pause(1.5)

        run(with_app(body2))
        assert sleeper.wait(timeout=5) == -signal.SIGTERM
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


# ── log del bot ──
def test_log_screen_follows_new_lines_and_copies_selection(monkeypatch):
    write_demo()
    config.log_path().parent.mkdir(parents=True, exist_ok=True)
    config.log_path().write_text("[t] SCAN BTC primera\n")
    copied = []
    monkeypatch.setattr(setuplog, "copy_text", lambda text, app=None: (copied.append(text), "copiado")[1])

    async def body(app, pilot):
        await pilot.press("l")
        await pilot.pause(0.5)
        assert isinstance(app.screen, LogScreen)
        assert "primera" in svg_text(app) and "siguiendo en vivo" in svg_text(app)
        with open(config.log_path(), "a") as f:
            f.write("[t] FILL BTC UP segunda\n")
        await pilot.pause(1.6)
        assert "segunda" in svg_text(app)
        await pilot.press("c")
        await pilot.pause(0.2)
        assert copied and "primera" in copied[0] and "segunda" in copied[0]
        await pilot.press("f")                          # parar el seguimiento
        await pilot.pause(0.2)
        with open(config.log_path(), "a") as f:
            f.write("[t] SCAN BTC tercera\n")
        await pilot.pause(1.6)
        assert "tercera" not in svg_text(app) and "en pausa" in svg_text(app)
        await pilot.press("f")
        await pilot.pause(0.3)
        assert "tercera" in svg_text(app)

    run(with_app(body))


def test_log_screen_missing_file_has_clear_message_and_picks_up_the_file_later():
    write_demo()

    async def body(app, pilot):
        await pilot.press("l")
        await pilot.pause(0.4)
        s = svg_text(app)
        assert "Todavía no hay log del bot" in s and "[B]" in s
        config.log_path().parent.mkdir(parents=True, exist_ok=True)
        config.log_path().write_text("[t] SCAN ETH ya existe\n")
        await pilot.pause(1.6)
        assert "ya existe" in svg_text(app)

    run(with_app(body))
