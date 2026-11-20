"""v1.2.4: compatibilidad Windows/macOS (simulada: aquí no hay Windows real), ajustes en caliente (monedas incluidas,
sin abandonar nunca una posición abierta), avisos «reinicia para aplicarlo» con [R] que conserva el modo, y el
«¿y ahora qué?» al terminar la configuración (panel, asistente de terminal e instalador)."""
import importlib
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from caro5m import account, compat, config, launcher, setup, status
from caro5m.tui import render
from caro5m.tui.app import AccountScreen, ConfirmScreen, SettingsScreen
from test_bot_loop import run_script, state_dry, world  # noqa: F401  (fixture reutilizada)
from test_setup_cli import FakeIO
from test_tui import run, svg_text, with_app, write_demo

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def win(monkeypatch):
    """Simula Windows en compat (os.name/sys.platform) sin tocar el resto del intérprete."""
    monkeypatch.setattr(compat, "is_windows", lambda: True)
    return True


def fake_psutil(alive=(), cmdlines=None, denied=()):
    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    killed = []

    class Process:
        def __init__(self, pid):
            if pid not in alive and pid not in denied:
                raise NoSuchProcess(pid)
            self.pid = pid

        def is_running(self):
            return True

        def status(self):
            return "running"

        def cmdline(self):
            return (cmdlines or {}).get(self.pid, [])

        def terminate(self):
            if self.pid in denied:
                raise AccessDenied("no")
            killed.append(self.pid)

    return types.SimpleNamespace(Process=Process, NoSuchProcess=NoSuchProcess, AccessDenied=AccessDenied,
                                 STATUS_ZOMBIE="zombie", killed=killed)


# ── carpeta compartida (SHM) ──
def test_shm_dir_falls_back_to_data_on_windows_and_without_dev_shm(isolated_env, monkeypatch, win):
    monkeypatch.delenv("CARO5M_SHM_DIR", raising=False)
    assert config.shm_dir() == config.data_dir() / "shm"
    assert config.ptb_shm_path() == config.data_dir() / "shm" / "pm_ptb_latest.json"
    monkeypatch.setenv("CARO5M_SHM_DIR", str(isolated_env / "otra"))  # sigue siendo configurable
    assert config.shm_dir() == isolated_env / "otra"


def test_shm_dir_macos_without_dev_shm(isolated_env, monkeypatch):
    monkeypatch.delenv("CARO5M_SHM_DIR", raising=False)
    monkeypatch.setattr(compat.sys, "platform", "darwin")
    monkeypatch.setattr(compat.os, "access", lambda *a, **k: False)  # macOS: no hay /dev/shm escribible
    assert config.shm_dir() == config.data_dir() / "shm"


# ── procesos ──
def test_pid_alive_windows_never_uses_os_kill(monkeypatch, win):
    ps = fake_psutil(alive={111})
    monkeypatch.setitem(sys.modules, "psutil", ps)
    monkeypatch.setattr(compat.os, "kill", lambda *a: (_ for _ in ()).throw(AssertionError("os.kill en Windows termina el proceso")))
    assert compat.pid_alive(111) is True
    assert compat.pid_alive(222) is False
    assert status.pid_alive(111) is True and status.pid_alive(None) is False


def test_pid_alive_windows_without_psutil_uses_openprocess(monkeypatch, win):
    monkeypatch.setitem(sys.modules, "psutil", None)  # import psutil -> ImportError
    seen = []
    monkeypatch.setattr(compat, "_win_pid_alive", lambda pid: seen.append(pid) or pid == 7)
    assert compat.pid_alive(7) is True and compat.pid_alive(8) is False and seen == [7, 8]


def test_cmdline_and_terminate_windows_via_psutil(monkeypatch, win):
    ps = fake_psutil(alive={10}, cmdlines={10: ["C:\\p\\.venv\\Scripts\\python.exe", "-m", "caro5m.bot"]}, denied={11})
    monkeypatch.setitem(sys.modules, "psutil", ps)
    assert compat.process_cmdline(10).endswith("-m caro5m.bot")
    assert launcher._cmdline(10).endswith("caro5m.bot")
    compat.terminate(10)
    assert ps.killed == [10]
    with pytest.raises(ProcessLookupError):
        compat.terminate(99)
    with pytest.raises(PermissionError):
        compat.terminate(11)


def test_terminate_windows_without_psutil_uses_taskkill(monkeypatch, win):
    monkeypatch.setitem(sys.modules, "psutil", None)
    calls = []

    def fake_run(cmd, **kw):
        calls.append((cmd, kw.get("creationflags")))
        return types.SimpleNamespace(returncode=0, stdout="OK", stderr="")

    monkeypatch.setattr(compat.subprocess, "run", fake_run)
    compat.terminate(4321)
    assert calls[0][0][:3] == ["taskkill", "/PID", "4321"] and calls[0][1] == compat.CREATE_NO_WINDOW


def test_launcher_spawn_on_windows_is_detached_python_module(isolated_env, monkeypatch, win):
    seen = {}

    class FakePopen:
        def __init__(self, cmd, **kw):
            seen.setdefault("calls", []).append((cmd, kw))
            self.pid = 5000 + len(seen["calls"])

        def poll(self):
            return None

    monkeypatch.setattr(launcher.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(launcher, "check_can_start", lambda st=None, use_systemd=True: None)
    pid = launcher.start_bot(use_systemd=False)
    cmd, kw = seen["calls"][0]
    assert cmd[-2:] == ["-m", "caro5m.bot"] and "bash" not in cmd[0] and not cmd[0].endswith(".sh")
    flags = kw["creationflags"]
    assert flags & compat.DETACHED_PROCESS and flags & compat.CREATE_NEW_PROCESS_GROUP and flags & compat.CREATE_NO_WINDOW
    assert "start_new_session" not in kw
    assert kw["env"]["PYTHONUTF8"] == "1" and kw["env"]["CARO5M_LOG_TO_FILE"] == "1"
    assert launcher.read_pid() == pid
    # PTB: también python -m caro5m.ptb, con su salida al log (en Linux lo hace run-ptb.sh)
    monkeypatch.setattr(launcher, "ptb_state", lambda use_systemd=True: {"running": False})
    ppid = launcher.start_ptb(use_systemd=False)
    cmd, kw = seen["calls"][1]
    assert cmd[-2:] == ["-m", "caro5m.ptb"] and kw["creationflags"] & compat.DETACHED_PROCESS
    assert hasattr(kw["stdout"], "write") or kw["stdout"] not in (None, subprocess.DEVNULL)
    assert launcher.read_ptb_pid() == ppid and (config.data_dir() / "ptb.log").exists()
    launcher._children.clear()


def test_launcher_spawn_posix_unchanged(isolated_env, monkeypatch):
    kw = compat.detached_popen_kwargs()
    assert kw == {"start_new_session": True, "close_fds": True}
    script = ROOT / "scripts" / "run-bot.sh"
    assert launcher._spawn_cmd(script, "caro5m.bot") == [str(script)]


def test_venv_python_paths(tmp_path, monkeypatch, win):
    (tmp_path / ".venv" / "Scripts").mkdir(parents=True)
    exe = tmp_path / ".venv" / "Scripts" / "python.exe"
    exe.write_text("")
    assert compat.venv_python(tmp_path) == exe
    assert compat.module_command("caro5m.bot", tmp_path) == [str(exe), "-m", "caro5m.bot"]
    assert compat.panel_command() == r".\caro5m.cmd" and compat.bot_command() == r".\run-bot.cmd"


# ── ficheros ──
def test_atomic_replace_retries_on_windows_when_reader_holds_file(tmp_path, monkeypatch, win):
    real = os.replace
    n = {"fails": 2}

    def flaky(a, b):
        if n["fails"]:
            n["fails"] -= 1
            raise PermissionError(32, "El proceso no tiene acceso al archivo porque está siendo utilizado por otro proceso")
        real(a, b)

    monkeypatch.setattr(compat.os, "replace", flaky)
    (tmp_path / "a.tmp").write_text("x", encoding="utf-8")
    compat.replace(tmp_path / "a.tmp", tmp_path / "a", sleep=lambda s: None)
    assert (tmp_path / "a").read_text(encoding="utf-8") == "x" and n["fails"] == 0
    # status.json (bot -> panel) usa el mismo camino
    n["fails"] = 1
    monkeypatch.setattr(compat.time, "sleep", lambda s: None)
    status.write_status({"ts": 1, "texto": "señal ✔"})
    assert status.read_status()["texto"] == "señal ✔"


def test_atomic_replace_posix_does_not_hide_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(compat.os, "replace", lambda a, b: (_ for _ in ()).throw(PermissionError("no")))
    with pytest.raises(PermissionError):
        compat.replace(tmp_path / "x", tmp_path / "y", sleep=lambda s: pytest.fail("en Linux no se reintenta"))


def test_permissions_are_noop_on_windows(tmp_path, monkeypatch, win):
    monkeypatch.setattr(compat.os, "chmod", lambda *a: pytest.fail("chmod en Windows"))
    if hasattr(os, "fchmod"):
        monkeypatch.setattr(compat.os, "fchmod", lambda *a: pytest.fail("fchmod en Windows"))
    assert compat.chmod_private(tmp_path, 0o700) is False
    config.atomic_write_text(tmp_path / ".env", "POLY_PRIVATE_KEY=x\nÑ=ü\n", 0o600)  # .env sin fchmod
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "POLY_PRIVATE_KEY=x\nÑ=ü\n"


def test_env_permission_check_is_info_on_windows(isolated_env, monkeypatch, win):
    config.env_file().write_text("X=1\n", encoding="utf-8")
    cs = {c.id: c for c in account.check_local()}
    assert cs["envperm"].level == "info" and "Windows" in cs["envperm"].detail


def test_utf8_stdio_is_noop_on_linux(monkeypatch):
    called = []
    fake = types.SimpleNamespace(encoding="cp1252", reconfigure=lambda **k: called.append(k))
    monkeypatch.setattr(compat.sys, "stdout", fake)
    compat.ensure_utf8_stdio()
    assert called == []
    monkeypatch.setattr(compat, "is_windows", lambda: True)
    compat.ensure_utf8_stdio()
    assert called == [{"encoding": "utf-8", "errors": "replace"}]


def test_no_posix_only_calls_left_outside_compat():
    """Auditoría: os.kill / signal / fchmod / /proc / start_new_session solo en compat.py."""
    import re

    bad = re.compile(r"os\.kill\(|import signal|os\.fchmod|/proc/|start_new_session|os\.killpg|os\.setsid|fcntl|termios")
    for f in (ROOT / "caro5m").rglob("*.py"):
        if f.name == "compat.py":
            continue
        txt = f.read_text(encoding="utf-8")
        assert not bad.search(txt), f"{f.name}: {bad.search(txt).group(0)}"


def test_windows_launchers_present_and_crlf():
    for name in ("caro5m.cmd", "start.bat", "run-bot.cmd", "run-ptb.cmd"):
        raw = (ROOT / name).read_bytes()
        assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), name
        assert raw.decode("ascii")  # cmd.exe: sin acentos
    ps1 = (ROOT / "install.ps1").read_bytes()
    assert ps1.startswith(b"\xef\xbb\xbf")  # PowerShell 5.1 necesita BOM para leer los acentos
    t = ps1.decode("utf-8")
    for needle in ("-m venv", "requirements.lock.txt", "caro5m.setup", "caro5m.cmd", "¿Abrir el panel ahora? [S/n]", "icacls"):
        assert needle in t, needle
    assert "systemctl" not in t and "logrotate" not in t.split("#>")[1]
    assert os.access(ROOT / "start.sh", os.X_OK)


def test_requirements_have_windows_markers():
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    lock = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
    assert 'psutil>=5.9; sys_platform == "win32"' in req and "tzdata" in req
    assert 'psutil==7.2.2 ; sys_platform == "win32"' in lock and "tzdata==" in lock
    assert "uvloop" not in req + lock


# ── ajustes en caliente ──
def _set(**kv):
    config.save_settings({k: str(v) for k, v in kv.items()})
    p = config.settings_file()
    t = time.time() + 1
    os.utime(p, (t, t))  # mtime distinto aunque el sistema de ficheros tenga resolución de 1 s


def test_feedwriter_set_coins_hot(bot):
    fw = bot.FeedWriter()
    assert fw.coins == ["btc", "eth"]
    added, removed = fw.set_coins(["btc", "xrp"])
    assert added == ["xrp"] and removed == ["eth"] and fw.coins == ["btc", "xrp"]
    coins, pre = fw._snapshot()
    assert coins == ["btc", "xrp"] and pre == ["xrp"] and fw._snapshot()[1] == []
    assert fw.set_coins(["btc", "xrp"]) == ([], [])


def test_coin_added_hot_without_restart(bot, world, monkeypatch, capsys):
    book = world["book"]
    book["xrp_up"], book["xrp_dn"] = (0.58, 0.60), (0.38, 0.40)
    seen = {}

    def add():
        status.set_paused(True)
        _set(COINS="btc,eth,xrp")

    def chk():
        seen["st"] = status.read_status()
        seen["coins"] = list(bot.COINS)

    run_script(bot, monkeypatch, world, [add, lambda: None, chk])
    st = seen["st"]
    assert seen["coins"] == ["btc", "eth", "xrp"] and st["coins_enabled"] == ["btc", "eth", "xrp"]
    assert "xrp" in st["coins"] and st["coins"]["xrp"].get("market") == "xrp-updown-5m-X"
    assert st["mode"] == "dry" and st["restart_pending"] == []
    assert st["settings_reload"]["hot"] == ["COINS"]
    out = capsys.readouterr().out
    assert "SETTINGS reloaded changed=COINS hot=COINS restart=-" in out
    assert "COINS hot-reload coins=btc,eth,xrp añadidas=xrp" in out


def test_removed_coin_with_open_position_is_managed_until_closed(bot, world, monkeypatch, capsys):
    book = world["book"]
    seen = {}

    def opened():  # iteración 1: ha entrado en BTC y ETH (simulación)
        seen["pos0"] = sorted(state_dry()["positions"])
        status.set_paused(True)
        _set(COINS="btc")  # quito ETH con su posición abierta

    def managing():
        seen["st1"] = status.read_status()
        book["eth_up"] = (0.69, 0.71)  # ETH llega al take-profit: se debe cerrar aunque ya no esté en COINS

    def after_tp():
        seen["st2"] = status.read_status()
        seen["state2"] = state_dry()

    def gone():
        seen["st3"] = status.read_status()

    run_script(bot, monkeypatch, world, [lambda: None, opened, managing, after_tp, gone])
    assert seen["pos0"] == ["btc", "eth"]
    st1 = seen["st1"]
    assert st1["coins_enabled"] == ["btc"] and st1["coins_managing"] == ["eth"]
    assert "eth" in st1["coins"] and "eth" in st1["positions"]
    assert "eth" not in seen["state2"]["positions"]
    assert [t["result"] for t in seen["state2"]["trades"] if t["coin"] == "eth"] == ["TP"]
    st3 = seen["st3"]
    assert st3["coins_managing"] == [] and "eth" not in st3["coins"] and "btc" in st3["positions"]
    out = capsys.readouterr().out
    assert "COINS hot-reload coins=btc quitadas=eth (con posición abierta, se sigue gestionando hasta cerrarla: eth)" in out
    assert "COINS ETH retirada" in out
    after = out.split("COINS hot-reload")[1]
    assert "FILL ETH" not in after  # tras quitarla, ninguna compra nueva de ETH


def test_removed_coin_never_opens_new_positions(bot, world, monkeypatch):
    seen = {}

    def chk():
        seen["st"] = status.read_status()
        seen["pos"] = sorted(state_dry()["positions"])

    # se quita ETH justo antes del primer ciclo (durante la espera inicial de los feeds): nunca se vigila ni se compra
    run_script(bot, monkeypatch, world, [lambda: _set(COINS="btc"), chk])
    assert seen["pos"] == ["btc"] and "eth" not in seen["st"]["coins"]


def test_restart_only_settings_are_published_and_mode_kept(bot, world, monkeypatch, capsys):
    seen = {}

    def change():
        status.set_paused(True)
        _set(CLOB_HOST="https://clob2.example.com", DRY_RUN="0", POLY_SIGNATURE_TYPE="1")

    def chk():
        seen["st"] = status.read_status()
        seen["sig"] = bot.SIGNATURE_TYPE

    run_script(bot, monkeypatch, world, [change, chk])
    st = seen["st"]
    keys = [x["key"] for x in st["restart_pending"]]
    assert keys == ["CLOB_HOST"]  # tipo de firma: aplicado en caliente (PRUEBA, sin cliente real)
    assert seen["sig"] == 1
    assert st["mode"] == "dry"  # DRY_RUN=0 nunca cambia el modo en marcha
    out = capsys.readouterr().out
    assert "SETTINGS reloaded changed=" in out and "restart=CLOB_HOST" in out
    assert "DRY_RUN solo decide el modo del próximo arranque" in out
    assert "Reinicia el bot para aplicar: Host CLOB" in out


def test_proxy_and_keys_changes_in_env(bot, monkeypatch, capsys):
    rt = bot.Runtime("dry")
    rt.boot_proxy = ""
    rt.client_fp = bot.secrets_fingerprint({})
    config.env_file().write_text("POLYMARKET_PROXY_LIST=http://u:p@h:1\n", encoding="utf-8")
    rt.env_mtime = 0
    bot.maybe_reload_secrets(rt)
    assert rt.restart_pending == {"proxy": "Proxy"}
    out = capsys.readouterr().out
    assert "u:p" not in out and "Proxy cambiado" in out
    # claves nuevas en PRUEBA: el cliente se recrea al pasar a REAL (sin reiniciar)
    rt.client = object()
    config.env_file().write_text("POLYMARKET_PROXY_LIST=\nPOLY_PRIVATE_KEY=0xabc\n", encoding="utf-8")
    rt.env_mtime = 0
    bot.maybe_reload_secrets(rt)
    assert rt.client is None and rt.restart_pending == {}
    # en REAL con cliente: hace falta reiniciar
    rt.mode, rt.client = "live", object()
    config.env_file().write_text("POLY_PRIVATE_KEY=0xdef\n", encoding="utf-8")
    rt.env_mtime = 0
    bot.maybe_reload_secrets(rt)
    assert rt.restart_pending == {"keys": "Cartera / claves CLOB"} and rt.client is not None
    assert "0xdef" not in capsys.readouterr().out


def test_signature_type_change_needs_restart_when_live(bot):
    rt = bot.Runtime("live")
    rt.client = object()
    _set(POLY_SIGNATURE_TYPE="0")
    bot.maybe_reload_settings(rt, {})
    assert "POLY_SIGNATURE_TYPE" in rt.restart_pending and bot.SIGNATURE_TYPE == 2
    _set(POLY_SIGNATURE_TYPE="2")  # vuelve al valor de arranque: ya no hay nada pendiente
    bot.maybe_reload_settings(rt, {})
    assert rt.restart_pending == {}


def test_settings_hot_flags():
    hot = {s.key for s in config.SETTINGS if s.hot}
    assert {"COINS", "COIN_SCALE", "POLYGON_RPCS", "BINANCE_HOST", "GAMMA_HOST", "BET_SIZE", "TP_OFFSET"} <= hot
    from caro5m import bot as b

    assert set(b.RESTART_KEYS) == {s.key for s in config.SETTINGS if not s.hot}
    assert all(config.SPEC_BY_KEY[k].hot for k in config.EDITABLE_KEYS)


# ── PTB sigue a COINS ──
def test_ptb_restarts_itself_when_coins_change(isolated_env, monkeypatch):
    import caro5m.ptb as p

    p = importlib.reload(p)
    seen = {}
    p.restart_self(_execv=lambda exe, args: seen.update(exe=exe, args=args))
    assert seen["args"][1:] == ["-m", "caro5m.ptb"] and seen["exe"] == sys.executable


def test_ptb_restart_on_windows_updates_pid_file(isolated_env, monkeypatch, win):
    import caro5m.ptb as p

    p = importlib.reload(p)
    config.data_dir().mkdir(parents=True, exist_ok=True)
    (config.data_dir() / "ptb.pid").write_text(str(os.getpid()), encoding="utf-8")
    seen = {}

    def popen(cmd, **kw):
        seen.update(cmd=cmd, kw=kw)
        return types.SimpleNamespace(pid=777)

    p.restart_self(_popen=popen, _exit=lambda code: seen.update(exit=code))
    assert seen["cmd"][1:] == ["-m", "caro5m.ptb"] and seen["kw"]["creationflags"] & compat.DETACHED_PROCESS
    assert seen["exit"] == 0 and launcher.read_ptb_pid() == 777


def test_ptb_coins_watch_triggers_restart(isolated_env, monkeypatch):
    import asyncio

    import caro5m.ptb as p

    p = importlib.reload(p)
    monkeypatch.setattr(p, "COINS_WATCH_S", 0.01)
    hit = []

    class Done(Exception):
        pass

    def fake_restart():
        hit.append(True)
        raise Done()

    monkeypatch.setattr(p, "restart_self", fake_restart)

    async def go():
        t = asyncio.create_task(p.coins_watch())
        await asyncio.sleep(0.05)
        _set(COINS="btc,sol")
        with pytest.raises(Done):
            await asyncio.wait_for(t, 2)

    asyncio.run(go())
    assert hit


# ── panel: banda «reinicia», [R] conserva el modo ──
def test_restart_banner_and_footer():
    st = {"restart_pending": [{"key": "proxy", "label": "Proxy"}, {"key": "CLOB_HOST", "label": "Host CLOB"}]}
    b = render.restart_banner(st, True)
    assert b is not None and "Reinicia el bot para aplicarlo: Proxy, Host CLOB" in b.plain and "[R]" in b.plain
    assert render.restart_banner(st, False) is None and render.restart_banner({}, True) is None
    assert "[R] Reiniciar" in render.footer_text(st, False, True).plain
    assert "[R]" not in render.footer_text({}, False, True).plain


def test_panel_shows_banner_and_r_restarts_keeping_live_mode(monkeypatch):
    st = write_demo(with_position=False)
    st.update(mode="live", restart_pending=[{"key": "proxy", "label": "Proxy"}])
    status.write_status(st)
    monkeypatch.setattr(launcher, "read_pid", lambda: 4242)
    monkeypatch.setattr(launcher, "is_bot_process", lambda pid, marker="caro5m.bot": True)
    from caro5m import procs  # v1.2.5: el panel busca los procesos del proyecto (no solo data/bot.pid)
    monkeypatch.setattr(launcher, "project_processes", lambda st=None, scanner=None: [procs.ProcInfo(4242, "bot", ["python", "-m", "caro5m.bot"])])
    seen = {}

    async def body(app, pilot):
        await pilot.pause(0.4)
        txt = svg_text(app)
        assert "Reinicia el bot para aplicarlo: Proxy" in txt
        assert any("Reinicia el bot para aplicarlo: Proxy" in n.message for n in app._notifications)
        monkeypatch.setattr(app, "_restart_worker", lambda was_live=False: seen.update(was_live=was_live))
        await pilot.press("r")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        assert "confirmación de modo REAL" in svg_text(app)
        await pilot.press("s")
        await pilot.pause(0.3)

    run(with_app(body, size=(130, 46)))
    assert seen == {"was_live": True}


def test_restart_then_start_asks_live_again(monkeypatch):
    write_demo(with_position=False)
    seen = []

    async def body(app, pilot):
        monkeypatch.setattr(app, "action_mode", lambda: seen.append("mode"))
        app._start_ok(4242, True)  # tras reiniciar desde REAL, el bot vuelve en PRUEBA -> se vuelve a pedir REAL
        app._start_ok(4242, False)

    run(with_app(body))
    assert seen == ["mode"]


def test_settings_coins_saved_hot_message(monkeypatch):
    write_demo(with_position=True)

    async def body(app, pilot):
        app.push_screen(SettingsScreen())
        await pilot.pause(0.3)
        scr = app.screen
        scr._save("BET_SIZE", "1.5")
        assert "aplicado en caliente" in str(scr.query_one("#settings-msg").render())

    run(with_app(body, size=(130, 46)))


# ── «¿y ahora qué?» al terminar la configuración ──
def _ok_checks():
    return [account.Check("x", "ok", "Todo", "bien")]


def test_account_screen_final_box_and_go_to_panel(monkeypatch):
    write_demo(with_position=False)
    monkeypatch.setattr(account, "run_preflight", lambda **k: _ok_checks())
    monkeypatch.setattr(account, "read_env", lambda: {"POLY_PRIVATE_KEY": "0x" + "1" * 64})

    async def body(app, pilot):
        app.push_screen(AccountScreen(guided=True))
        await pilot.pause(0.4)
        scr = app.screen
        assert scr.query_one("#acc-done").display is False
        assert "Esc" in svg_text(app) and "volver al panel" in svg_text(app)
        await __import__("asyncio").to_thread(scr._checks)
        await pilot.pause(0.3)
        assert scr.query_one("#acc-done").display is True
        txt = svg_text(app)
        assert "Todo listo. Pulsa Esc" in txt and "Ir al panel" in txt and "[B] arranca el bot" in txt
        await pilot.click("#acc-go-panel")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, AccountScreen)
        msgs = [n.message for n in app._notifications]
        assert any("[M] modo REAL" in m for m in msgs), msgs  # bot en marcha (demo): siguiente paso, [M]

    run(with_app(body, size=(130, 50)))


def test_account_screen_no_final_box_when_checks_fail(monkeypatch):
    write_demo(with_position=False)
    monkeypatch.setattr(account, "run_preflight", lambda **k: [account.Check("x", "fail", "Clave", "falta")])

    async def body(app, pilot):
        app.push_screen(AccountScreen())
        await pilot.pause(0.3)
        await __import__("asyncio").to_thread(app.screen._checks)
        await pilot.pause(0.3)
        assert app.screen.query_one("#acc-done").display is False

    run(with_app(body, size=(130, 50)))


def test_terminal_wizard_prints_next_steps(isolated_env, monkeypatch):
    monkeypatch.setattr(account, "run_preflight", lambda **k: _ok_checks())
    io = FakeIO(["4", "0"])
    setup.menu(io)
    out = "\n".join(io.out)
    assert "Siguientes pasos" in out and "./start.sh" in out and "[B] arranca el bot" in out and "[M] pasa a modo REAL" in out
    monkeypatch.setenv("CARO5M_FROM_INSTALLER", "1")  # el instalador imprime su propio resumen: no repetir
    io2 = FakeIO(["0"])
    setup.menu(io2)
    assert "Siguientes pasos" not in "\n".join(io2.out)


def test_installer_prints_summary_and_offers_panel():
    t = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "¿Abrir el panel ahora? [S/n]" in t and "./start.sh" in t and "[B]" in t and "[M]" in t and "scripts/run-bot.sh" in t
    assert "CARO5M_FROM_INSTALLER=1" in t
    r = subprocess.run(["bash", "-n", str(ROOT / "install.sh")], capture_output=True)
    assert r.returncode == 0, r.stderr
    s = (ROOT / "start.sh").read_text(encoding="utf-8")
    assert ".venv/bin/python" in s and "-m caro5m" in s
