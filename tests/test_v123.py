"""v1.2.3: todas las monedas de 5 min (tabla, slugs, escala, ajustes, panel con N monedas), resultado visible de la
petición de modo REAL, «ARRANCANDO…», PTB arrancado/parado con el bot, y redibujado (Ctrl+L / cambio de tamaño)."""
import importlib
import json
import os
import signal
import subprocess
import time

import pytest

from caro5m import coins, config, launcher, status
from caro5m.tui import render
from caro5m.tui.app import Caro5mApp, CoinsScreen, ConfirmScreen, SettingsScreen
from test_tui import run, svg_text, with_app, write_demo

import demo_status


# ── tabla de monedas ──
def test_coin_table_matches_verified_markets_and_feeds():
    assert coins.SUPPORTED == ["btc", "eth", "xrp", "sol", "doge", "bnb", "hype", "zec"]
    t = coins.COIN_TABLE
    # BTC/ETH: exactamente las direcciones de siempre
    assert t["btc"].chainlink_polygon == "0xc907E116054Ad103354f2D350FD2514433D57F6f"
    assert t["eth"].chainlink_polygon == "0xF9680D99D6C9589e2a93a78A04A279e509205945"
    assert t["hype"].chainlink_polygon is None and coins.degraded_coins(coins.SUPPORTED) == ["hype", "zec"]
    assert t["zec"].chainlink_polygon is None and t["zec"].chainlink_polygon_slow  # existe pero heartbeat 24 h
    for c, spec in t.items():
        assert spec.binance == c.upper() + "USDT" and spec.rtds == f"{c}/usd" and spec.chainlink_decimals == 8
        if spec.chainlink_polygon:
            a = spec.chainlink_polygon
            assert a.startswith("0x") and len(a) == 42 and int(a, 16)


def test_slug_per_coin():
    iv = 1795189800
    for c in coins.SUPPORTED:
        assert coins.slug(c, iv) == f"{c}-updown-5m-{iv}"
    assert coins.slug("xrp", 1795189800.0) == "xrp-updown-5m-1795189800"


def test_parse_coins_order_dedup_and_errors():
    assert coins.parse_coins("XRP, btc;eth eth") == ["btc", "eth", "xrp"]
    assert coins.parse_coins(["hype", "doge"]) == ["doge", "hype"]
    with pytest.raises(ValueError):
        coins.parse_coins("btc,ada")
    with pytest.raises(ValueError):
        coins.parse_coins("  ")


def test_scale_parse_and_feed_scaling():
    assert coins.parse_scales("xrp:1.5, doge:2") == {"xrp": 1.5, "doge": 2.0}
    assert coins.parse_scales("") == {}
    for bad in ("xrp", "ada:2", "xrp:abc", "xrp:50"):
        with pytest.raises(ValueError):
            coins.parse_scales(bad)
    cl = {"delta_pct": 0.3, "delta_twap_pct": 0.24, "price": 0.05}
    bn = {"change_1m": 0.08, "rsi14": 55}
    a, b = coins.scale_feeds(cl, bn, 1.0)
    assert a is cl and b is bn                                   # 1.0: mismos objetos (BTC/ETH sin cambios)
    a, b = coins.scale_feeds(cl, bn, 2.0)
    assert a["delta_twap_pct"] == pytest.approx(0.12) and b["change_1m"] == pytest.approx(0.04)
    assert cl["delta_twap_pct"] == 0.24 and a["price"] == 0.05 and b["rsi14"] == 55


def test_rounding_and_display_keep_btc_eth_and_help_cheap_coins():
    assert coins.round_px("btc", 62000.123456, 4) == 62000.1235
    assert coins.round_px("eth", 1700.126, 2) == 1700.13
    assert coins.round_px("doge", 0.0536663129, 2) == 0.05366631
    assert coins.display_decimals("btc", 1) == 2 and coins.display_decimals("eth", 1) == 3
    assert coins.display_decimals("doge", 0.0537) == 5 and coins.display_decimals("xrp", 0.9) == 4
    assert coins.display_decimals("sol", 68.2) == 3 and coins.display_decimals("bnb", 546.1) == 2
    assert coins.fmt_px("btc", 1.0) == "1.00" and coins.fmt_px("doge", 0.0536663) == "0.0536663"


# ── umbrales relativos ──
def _feeds(price, d_twap, bin1m, now):
    iv = int(now) - int(now) % 300
    cl = {"price": price, "delta_pct": d_twap, "delta_twap_pct": d_twap, "direction_twap": "UP" if d_twap > 0 else "DOWN",
          "updated_at": now, "stale_secs": 1, "secs_into": 60, "secs_remaining": 240, "interval_start": iv}
    bn = {"ts": now, "change_1m": bin1m, "rsi14": 55.0}
    return cl, bn


def test_thresholds_are_percent_based_same_decision_for_btc_and_doge(bot):
    now = time.time()
    for coin, price in (("btc", 62000.0), ("doge", 0.0537), ("xrp", 0.908)):
        cl, bn = _feeds(price, 0.16, 0.05, now)
        side, reason, meta = bot.evaluate_signal(coin, cl, bn, now)
        assert side == "UP", (coin, reason)
        cl, bn = _feeds(price, 0.04, 0.05, now)
        assert bot.evaluate_signal(coin, cl, bn, now)[0] is None
    # con escala 2 la misma señal de DOGE ya no basta (Δ efectivo 0.08 < 0.10)
    cl, bn = coins.scale_feeds(*_feeds(0.0537, 0.16, 0.05, now), 2.0)
    assert bot.evaluate_signal("doge", cl, bn, now)[0] is None


# ── ajustes ──
def test_settings_coins_default_save_and_invalid(isolated_env):
    cfg = config.load_settings()
    assert cfg["COINS"] == ["btc", "eth"] and cfg["COIN_SCALE"] == {} and cfg["AUTO_START_PTB"] is False  # conftest: 0
    assert config.SPEC_BY_KEY["AUTO_START_PTB"].default == "1"
    assert "COINS" in config.EDITABLE_KEYS and "AUTO_START_PTB" in config.EDITABLE_KEYS
    assert config.SPEC_BY_KEY["COINS"].hot is True  # v1.2.4: en caliente
    out = config.save_settings({"COINS": "xrp,btc,hype"})
    assert out["COINS"] == ["btc", "xrp", "hype"]
    assert "COINS=xrp,btc,hype" in config.settings_file().read_text()
    with pytest.raises(ValueError):
        config.save_settings({"COINS": "btc,ada"})
    with pytest.raises(ValueError):
        config.save_settings({"COINS": ""})
    # un valor inválido a mano no rompe el arranque: cae al defecto
    config.settings_file().write_text("COINS=foo\nCOIN_SCALE=xrp:1.5\n")
    cfg = config.load_settings(environ={})
    assert cfg["COINS"] == ["btc", "eth"] and cfg["COIN_SCALE"] == {"xrp": 1.5}


def test_bot_uses_configured_coins_and_hype_falls_back_to_binance(isolated_env, monkeypatch):
    monkeypatch.setenv("COINS", "btc,eth,xrp,hype")
    import caro5m.bot as b

    b = importlib.reload(b)
    try:
        assert b.COINS == ("btc", "eth", "xrp", "hype")
        assert set(b._COIN_HIST) == {"btc", "eth", "xrp", "hype"}
        assert "hype" not in b.CHAINLINK_CONTRACTS and b.CHAINLINK_CONTRACTS["xrp"] == coins.COIN_TABLE["xrp"].chainlink_polygon
        assert b.BINANCE_SYMBOL["hype"] == "HYPEUSDT"
        fw = b.FeedWriter()
        assert fw.chainlink_price("hype") is None                 # aún sin precio de Binance
        fw.bn_spot["hype"] = (61.5, int(time.time()))
        assert fw.chainlink_price("hype") == (61.5, fw.bn_spot["hype"][1])
        fw.bn_spot["hype"] = (61.5, int(time.time()) - 60)        # viejo: no se usa
        assert fw.chainlink_price("hype") is None
        # escribe chainlink_hype.json marcado como binance y con precisión completa
        (isolated_env / "shm").mkdir(exist_ok=True)
        fw.bn_spot["doge"] = (0.0536663129, int(time.time()))
        monkeypatch.setattr(fw, "chainlink_price", lambda c: (0.0536663129, int(time.time())))
        fw.write_chainlink("hype")
        d = json.loads((isolated_env / "shm" / "chainlink_hype.json").read_text())
        assert d["price_src"] == "binance" and d["price"] == 0.05366631
        fw.write_chainlink("btc")
        d = json.loads((isolated_env / "shm" / "chainlink_btc.json").read_text())
        assert "price_src" not in d                               # BTC: mismo fichero de siempre
        rt = b.Runtime("dry")
        st = b.build_status(rt, b.default_state(), time.time(), 0)
        assert st["coins_enabled"] == ["btc", "eth", "xrp", "hype"] and st["coins_degraded"] == ["hype"]
        assert list(st["coins"]) == ["btc", "eth", "xrp", "hype"]
    finally:
        monkeypatch.delenv("COINS")
        importlib.reload(b)


def test_discover_market_uses_coin_slug(bot, monkeypatch):
    seen = []

    class R:
        def raise_for_status(self):
            pass

        def json(self):
            return []

    class S:
        def get(self, url, params=None, timeout=None):
            seen.append(params["slug"])
            return R()

    assert bot.discover_market(S(), "doge", 1795189812) is None
    assert seen == ["doge-updown-5m-1795189800", "doge-updown-5m-1795190100", "doge-updown-5m-1795189500"]


def test_ptb_module_follows_coins_setting(isolated_env, monkeypatch):
    monkeypatch.setenv("COINS", "btc,doge,hype")
    import caro5m.ptb as p

    p = importlib.reload(p)
    assert p.COINS == ["btc", "doge", "hype"]
    assert p.RTDS_SYMBOLS == {"btc/usd": "btc", "doge/usd": "doge", "hype/usd": "hype"}   # antes s[:3] => «dog»
    assert set(p.SEED_FB_BIAS) == {"btc", "doge", "hype"}


# ── petición de modo: resultado publicado ──
def test_mode_request_result_ok_refused_and_logged(bot, monkeypatch, capsys):
    from caro5m import checks

    rt = bot.Runtime("dry")
    bot.set_state_mode("dry")
    st = bot.load_state()
    monkeypatch.setattr(bot, "read_secrets", lambda quiet=False: {"POLY_PRIVATE_KEY": "x", "POLY_SAFE_ADDRESS": "0x" + "1" * 40})
    monkeypatch.setattr(bot, "BET_SIZE", 2.0)
    monkeypatch.setattr(checks, "check_wallet", lambda *a, **k: {"configured": True, "balance_usdc": 0.91, "allowance_ok": True,
                                                                  "error": None, "checked_ts": time.time()})
    status.set_mode_request("live")
    st = bot.handle_controls(rt, {}, st)
    out = capsys.readouterr().out
    assert "CONTROL mode request received -> live" in out
    mr = rt.mode_request
    assert rt.mode == "dry" and mr["target"] == "live" and mr["result"] == "refused"
    assert "saldo insuficiente (0.91 USDC; hacen falta ≥ 2.06)" in mr["reason"]
    w = list(bot.WARNINGS)[-1]
    assert w["level"] == "error" and w["text"] == "No se activó el modo REAL: saldo insuficiente (0.91 USDC; hacen falta ≥ 2.06)"
    s = bot.build_status(rt, st, time.time(), 0)
    assert s["mode_request"]["result"] == "refused" and s["warnings"][0]["text"].startswith("No se activó el modo REAL")
    # mismo modo: respuesta ok (y log), sin cambiar nada
    status.set_mode_request("dry")
    st = bot.handle_controls(rt, {}, st)
    assert rt.mode_request["result"] == "ok" and "ya estaba" in rt.mode_request["reason"]
    assert "CONTROL mode request received -> dry" in capsys.readouterr().out
    # aceptado
    monkeypatch.setattr(bot, "ensure_live", lambda rt, env: (True, "ok"))
    status.set_mode_request("live")
    st = bot.handle_controls(rt, {}, st)
    assert rt.mode == "live" and rt.mode_request == {**rt.mode_request, "target": "live", "result": "ok"}
    # con posición: rechazado y con motivo
    st["positions"]["xrp"] = {"coin": "xrp"}
    status.set_mode_request("dry")
    st = bot.handle_controls(rt, {}, st)
    assert rt.mode == "live" and rt.mode_request["result"] == "refused" and "posición" in rt.mode_request["reason"]


def test_keys_present_in_status(bot):
    rt = bot.Runtime("dry")
    assert bot.build_status(rt, bot.default_state(), time.time(), 0)["keys_present"] is False
    rt.keys_present = True
    assert bot.build_status(rt, bot.default_state(), time.time(), 0)["keys_present"] is True
    assert bot.keys_in_env({"POLY_PRIVATE_KEY": "x"}) is (bot.SIGNATURE_TYPE == 0)
    assert bot.keys_in_env({"POLY_PRIVATE_KEY": "x", "POLY_SAFE_ADDRESS": "0xabc"}) is True


# ── panel: N monedas ──
def multi_status(**kw):
    st = demo_status.build(with_position=False)
    st["coins"]["xrp"] = {"ptb": 0.90812, "chainlink": 0.90833, "delta_twap": 0.021, "up_bid": 0.5, "up_ask": 0.52,
                          "dn_bid": 0.47, "dn_ask": 0.49, "signal": {"ok": False, "word": "delta"}}
    st["coins"]["doge"] = {"ptb": 0.053661, "chainlink": 0.053702, "delta_twap": 0.076, "up_bid": 0.55, "up_ask": 0.57,
                           "dn_bid": 0.42, "dn_ask": 0.44, "signal": {"ok": False, "word": "momento", "side": "UP"}}
    st["coins"]["hype"] = {"ptb": 61.48, "chainlink": 61.55, "delta_twap": 0.11, "feed_src": "binance",
                           "signal": {"ok": False, "word": "datos"}}
    st["coins_enabled"] = ["btc", "eth", "xrp", "doge", "hype"]
    st["coins_degraded"] = ["hype"]
    st.update(kw)
    return st


def test_render_window_panel_and_header_with_five_coins():
    from rich.console import Console

    st = multi_status()
    con = Console(width=140, height=40, record=True)
    con.print(render.window_panel(st, time.time(), "Europe/Madrid", True))
    out = con.export_text()
    for needle in ("BTC", "ETH", "XRP", "DOGE", "HYPE*", "0,05366", "0,9081", "62.268,57", "modo degradado"):
        assert needle in out, needle
    h = render.header(st, True, time.time(), "Europe/Madrid", 140).plain
    assert "Polymarket 5m · BTC/ETH/XRP/DOGE/HYPE" in h
    # bot antiguo (sin coins_enabled): usa las claves de coins
    old = demo_status.build()
    assert "BTC/ETH" in render.header(old, True, time.time(), "UTC", 120).plain
    assert render.coin_list(None) == ["btc", "eth"]


def test_tui_renders_many_coins():
    status.write_status(multi_status())

    async def body(app, pilot):
        s = svg_text(app)
        for needle in ("BTC/ETH/XRP/DOGE/HYPE", "XRP", "DOGE", "HYPE*"):
            assert needle in s, needle

    run(with_app(body, size=(150, 48)))


# ── panel: [M] con resultado ──
def test_m_shows_refused_result_from_bot(monkeypatch):
    st = write_demo(with_position=False)
    st["keys_present"] = True
    st["bet_size"] = 2.0
    st["wallet"]["balance_usdc"] = 0.91
    status.write_status(st)

    async def body(app, pilot):
        await pilot.press("m")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        txt = svg_text(app)
        assert "Saldo insuficiente" in txt and "0.91" in txt and "2.06" in txt
        await pilot.press("s")
        await pilot.pause(0.2)
        assert status.get_mode_request() == "live" and app._mode_pending
        # el bot responde
        st2 = dict(st, ts=time.time(), mode_request={"target": "live", "result": "refused",
                                                     "reason": "saldo insuficiente (0.91 USDC; hacen falta ≥ 2.06)", "ts": time.time()})
        status.write_status(st2)
        await pilot.pause(1.4)
        assert app._mode_pending is None
        msgs = [n.message for n in app._notifications]
        assert any("No se activó el modo REAL" in m and "0.91" in m for m in msgs), msgs

    run(with_app(body))


def test_m_without_answer_points_to_log(monkeypatch):
    st = write_demo(with_position=False)
    st["keys_present"] = True
    status.write_status(st)

    async def body(app, pilot):
        app.MODE_WAIT = 0.5
        await pilot.press("m")
        await pilot.pause(0.2)
        await pilot.press("s")
        await pilot.pause(0.1)
        status.write_status(dict(st, ts=time.time()))
        await pilot.pause(1.6)
        msgs = [n.message for n in app._notifications]
        assert any("no ha respondido" in m and "[L]" in m for m in msgs), msgs
        assert app._mode_pending is None

    run(with_app(body))


def test_m_allowed_when_keys_present_even_if_wallet_not_checked_yet():
    st = write_demo(with_position=False)
    st["wallet"] = {"configured": False, "balance_usdc": None}
    st["keys_present"] = True
    status.write_status(st)

    async def body(app, pilot):
        await pilot.press("m")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen) and "aún no se ha leído" in svg_text(app)

    run(with_app(body))
    st["keys_present"] = False
    status.write_status(dict(st, ts=time.time()))

    async def body2(app, pilot):
        await pilot.press("m")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ConfirmScreen)
        assert any("falta la clave privada" in n.message for n in app._notifications)

    run(with_app(body2))


# ── ARRANCANDO… ──
def test_starting_badge_and_need_bot_message():
    assert render.mode_badge(None, False, starting=True).plain == "● ARRANCANDO…"
    assert render.mode_badge(None, False).plain == "● DETENIDO"
    st = demo_status.build()
    st["ts"] = time.time() - 300
    status.write_status(st)

    async def body(app, pilot):
        app._starting = True
        app.refresh_view()
        await pilot.pause(0.1)
        s = svg_text(app)
        assert "ARRANCANDO" in s and "DETENIDO" not in s.split("\n")[0:3].__str__()
        await pilot.press("p")
        await pilot.pause(0.1)
        assert any("se está arrancando" in n.message for n in app._notifications)

    run(with_app(body))


# ── PTB con el bot ──
def fake_ptb_script(tmp_path):
    d = tmp_path / "proj" / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    s = d / "run-ptb.sh"
    s.write_text("#!/usr/bin/env bash\nexec -a 'python -m caro5m.ptb' sleep 60\n")
    s.chmod(0o755)
    return s


def kill_quiet(pid):
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def test_start_ptb_detached_no_double_start_and_stop_only_ours(isolated_env):
    script = fake_ptb_script(isolated_env)
    pid = launcher.start_ptb(script, use_systemd=False)
    try:
        assert pid and launcher.read_ptb_pid() == pid and launcher.ptb_pid_path() == isolated_env / "data" / "ptb.pid"
        time.sleep(0.4)
        assert launcher.is_ptb_process(pid) and not launcher.is_bot_process(pid)
        assert os.getsid(pid) == pid != os.getsid(0)
        assert launcher.start_ptb(script, use_systemd=False) is None          # no se duplica
        stt = launcher.ptb_state(use_systemd=False)
        assert stt["running"] and stt["ours"] and stt["how"] == "panel"
        assert launcher.stop_ptb() == pid
        assert not status.pid_alive(pid) or not launcher.is_ptb_process(pid)
        assert launcher.read_ptb_pid() is None
    finally:
        kill_quiet(pid)
        launcher.reap()


def test_stop_ptb_never_kills_a_foreign_pid(isolated_env):
    other = subprocess.Popen(["sleep", "30"])
    try:
        launcher._write_ptb_pid(other.pid)
        assert launcher.stop_ptb() is None and other.poll() is None    # no es caro5m.ptb: no se toca
        assert launcher.read_ptb_pid() is None
    finally:
        other.kill()
        other.wait()
    launcher._write_ptb_pid(2 ** 22 + 777)
    assert launcher.stop_ptb() is None and launcher.read_ptb_pid() is None


def test_ptb_state_detects_external_ptb_by_fresh_file(isolated_env):
    assert launcher.ptb_state(use_systemd=False)["how"] == "parado"
    p = config.ptb_shm_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}")
    stt = launcher.ptb_state(use_systemd=False)
    assert stt["running"] and stt["how"] == "externo" and not stt["ours"]
    assert launcher.start_ptb(use_systemd=False) is None                   # ya hay uno: no se lanza otro
    old = time.time() - 900
    os.utime(p, (old, old))
    assert launcher.ptb_state(use_systemd=False)["how"] == "parado"


def test_panel_starts_and_stops_ptb_with_bot(monkeypatch, isolated_env):
    calls = []
    monkeypatch.setenv("AUTO_START_PTB", "1")
    monkeypatch.setattr(launcher, "check_can_start", lambda st=None, use_systemd=True: None)

    def fake_start(script=None, use_systemd=True, st=None):
        status.write_status(demo_status.build(with_position=False))
        launcher.write_pid(4242)
        return 4242

    monkeypatch.setattr(launcher, "start_bot", fake_start)
    monkeypatch.setattr(launcher, "wait_until_fresh", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "start_ptb", lambda *a, **k: calls.append("start_ptb") or 5151)
    monkeypatch.setattr(launcher, "stop_ptb", lambda *a, **k: calls.append("stop_ptb") or 5151)
    monkeypatch.setattr(launcher, "stop_bot", lambda *a, **k: calls.append("stop_bot") or 4242)
    st = demo_status.build(with_position=False)
    st["ts"] = time.time() - 300
    status.write_status(st)

    async def body(app, pilot):
        await pilot.press("b")
        await pilot.pause(0.2)
        await pilot.press("s")
        await pilot.pause(1.2)
        assert calls == ["start_ptb"]
        await __import__("asyncio").to_thread(app._stop_worker)
        await pilot.pause(0.3)
        assert calls == ["start_ptb", "stop_bot", "stop_ptb"]
        assert any("PTB parado" in n.message for n in app._notifications)

    run(with_app(body))
    # AUTO_START_PTB=0: no se lanza
    calls.clear()
    monkeypatch.setenv("AUTO_START_PTB", "0")
    status.write_status(st)

    async def body2(app, pilot):
        await pilot.press("b")
        await pilot.pause(0.2)
        await pilot.press("s")
        await pilot.pause(1.2)
        assert "start_ptb" not in calls

    run(with_app(body2))


def test_system_panel_shows_ptb_process():
    from rich.console import Console

    st = demo_status.build()
    for pp, needle in (({"running": True, "how": "panel", "pid": 77}, "en marcha (panel, pid 77)"),
                       ({"running": True, "how": "externo"}, "otra terminal"),
                       ({"running": False, "how": "parado"}, "parado")):
        con = Console(width=100, height=40, record=True)
        con.print(render.system_panel(st, time.time(), True, 10.0, pp))
        out = con.export_text()
        assert "PTB proceso" in out and needle in out, out
    con = Console(width=100, height=40, record=True)
    con.print(render.system_panel(None, time.time(), False, 10.0, {"running": False, "how": "parado"}))
    assert "PTB proceso" in con.export_text()


# ── ajustes: monedas con casillas ──
def test_settings_coins_checkbox_screen_saves(monkeypatch):
    write_demo(with_position=False)

    async def body(app, pilot):
        app.push_screen(SettingsScreen())
        await pilot.pause(0.3)
        from textual.widgets import Checkbox, DataTable

        t = app.screen.query_one("#settings-table", DataTable)
        t.move_cursor(row=config.EDITABLE_KEYS.index("COINS"))
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert isinstance(app.screen, CoinsScreen)
        app.screen.query_one("#coin-xrp", Checkbox).value = True
        app.screen.query_one("#coin-hype", Checkbox).value = True
        app.screen.query_one("#coin-eth", Checkbox).value = False
        assert app.screen.selected() == ["btc", "xrp", "hype"]
        await pilot.click("#coins-save")
        await pilot.pause(0.4)
        assert config.load_settings()["COINS"] == ["btc", "xrp", "hype"]
        # v1.2.4: las monedas se aplican en caliente (sin reiniciar)
        assert any("en caliente" in n.message for n in app._notifications)
        assert not any("Reinicia el bot" in n.message for n in app._notifications)

    run(with_app(body, size=(130, 50)))


def test_coins_screen_requires_one_coin():
    write_demo(with_position=False)

    async def body(app, pilot):
        from textual.widgets import Checkbox, Static

        app.push_screen(CoinsScreen(["btc"]))
        await pilot.pause(0.3)
        app.screen.query_one("#coin-btc", Checkbox).value = False
        await pilot.click("#coins-save")
        await pilot.pause(0.2)
        assert isinstance(app.screen, CoinsScreen)
        assert "al menos una" in str(app.screen.query_one("#coins-msg", Static).render())

    run(with_app(body, size=(130, 50)))


# ── redibujado ──
def test_resize_and_ctrl_l_redraw_fill_terminal():
    write_demo()

    async def body(app, pilot):
        await pilot.resize_terminal(80, 30)
        await pilot.pause(0.3)
        await pilot.resize_terminal(200, 55)
        await pilot.pause(0.4)
        assert app.screen.query_one("#header").outer_size.width == 200
        assert app.screen.query_one("#panel_window").outer_size.width == 200
        await pilot.press("ctrl+l")
        await pilot.pause(0.2)
        s = svg_text(app)
        assert "VENTANA ACTUAL" in s and "Redibujar" in s

    run(with_app(body))
