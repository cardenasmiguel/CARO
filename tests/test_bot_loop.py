"""Bucle principal del bot en PRUEBA con red simulada: entrada -> TP, pausa, venta manual. Sin red, sin claves."""
import json
import time

import pytest

from caro5m import config, status


REAL_SLEEP = time.sleep  # antes de que los tests lo sustituyan


class Stop(BaseException):
    pass


class DummyFeeds:
    def start(self):
        pass


@pytest.fixture
def world(bot, monkeypatch):
    shm = config.shm_dir()
    now = time.time()
    book = {}

    def feed():
        t = time.time()
        for coin in bot.COINS:
            (shm / f"chainlink_{coin}.json").write_text(json.dumps({
                "price": 100.0, "open_twap_5m": 99.8, "delta_pct": 0.15, "delta_twap_pct": 0.15, "direction_twap": "UP",
                "secs_into": 60, "secs_remaining": 240, "stale_secs": 0, "updated_at": t, "ts": t, "interval_start": 0}))
            (shm / f"binance_{coin}.json").write_text(json.dumps({"price": 100.0, "ts": t, "change_1m": 0.05, "rsi14": 55.0}))
    for coin in bot.COINS:
        book[f"{coin}_up"] = (0.58, 0.60)
        book[f"{coin}_dn"] = (0.38, 0.40)

    monkeypatch.setattr(bot, "FeedWriter", DummyFeeds)
    monkeypatch.setattr(bot, "_background_probes", lambda *a, **k: None)
    monkeypatch.setattr(bot, "discover_market", lambda s, coin, n: {"slug": f"{coin}-updown-5m-X", "interval": 0, "up_token": f"{coin}_up", "down_token": f"{coin}_dn"})
    monkeypatch.setattr(bot, "best_bid_ask", lambda s, tok: book[tok])
    monkeypatch.setattr(bot, "LOOP_SLEEP", 0)
    feed()
    return {"book": book, "feed": feed}


def run_script(bot, monkeypatch, world, script):
    calls = {"n": 0}

    def fake_sleep(_s):
        n = calls["n"]
        calls["n"] += 1
        world["feed"]()
        if n >= len(script):
            raise Stop()
        script[n]()

    monkeypatch.setattr(bot.time, "sleep", fake_sleep)
    with pytest.raises(Stop):
        bot.main()


def state_dry():
    return json.loads(config.state_path("dry").read_text())


def test_dry_run_entry_then_take_profit(bot, world, monkeypatch):
    assert bot.CFG["DRY_RUN"] is True
    book = world["book"]
    seen = {}

    def after_entry():  # iteración 1: debe haber entrado en simulación
        seen["pos"] = dict(state_dry()["positions"])
        status.set_paused(True)  # sin reentradas
        for c in bot.COINS:
            book[f"{c}_up"] = (0.69, 0.71)  # bid >= entrada + TP

    def after_tp():
        st = state_dry()
        seen["trades"] = list(st["trades"])
        seen["pos_after"] = dict(st["positions"])
        seen["status"] = status.read_status()

    run_script(bot, monkeypatch, world, [lambda: None, after_entry, after_tp])
    assert set(seen["pos"]) == set(bot.COINS) and all(p["dry"] for p in seen["pos"].values())
    assert len(seen["trades"]) == len(bot.COINS) and {t["result"] for t in seen["trades"]} == {"TP"}
    assert all(t["dry"] and t["pnl"] > 0 for t in seen["trades"]) and not seen["pos_after"]
    st = seen["status"]
    assert st["mode"] == "dry" and st["paused"] is True and not st["positions"]
    assert st["today"]["wins"] == len(bot.COINS)
    assert not config.state_path("live").exists()  # jamás se tocó el estado real


def test_dry_run_sell_now_is_manual_and_only_that_coin(bot, world, monkeypatch):
    book = world["book"]
    seen = {}

    def sell():
        status.request_sell(["btc"])
        status.set_paused(True)
        for c in bot.COINS:
            book[f"{c}_up"] = (0.62, 0.63)

    def after_sell():
        seen["trades"] = list(state_dry()["trades"])
        seen["pos"] = dict(state_dry()["positions"])
        seen["status"] = status.read_status()

    run_script(bot, monkeypatch, world, [lambda: None, sell, after_sell])
    assert [t["result"] for t in seen["trades"]] == ["MANUAL"] and seen["trades"][0]["coin"] == "btc"
    assert set(seen["pos"]) == {"eth"}  # la otra posición sigue abierta y gestionada
    assert set(seen["status"]["positions"]) == {"eth"}
    assert not (config.control_dir() / "sell_now").exists()  # consumido
    assert state_dry().get("loss_streak", 0) == 0  # una venta manual no cuenta como racha


def test_pause_blocks_new_entries(bot, world, monkeypatch):
    status.set_paused(True)
    seen = {}

    def chk():
        seen["pos"] = {}
        seen["sig"] = status.read_status()["coins"]["btc"]["signal"]

    run_script(bot, monkeypatch, world, [lambda: None, chk])
    assert seen["pos"] == {}
    assert seen["sig"]["word"] == "pausa"


def test_stop_loss_counts_streak_and_trips_breaker(bot, world, monkeypatch):
    monkeypatch.setattr(bot, "SL_MIN_HOLD_SECS", 0)
    book = world["book"]
    seen = {}

    def drop():
        status.set_paused(True)
        for c in bot.COINS:
            book[f"{c}_up"] = (0.30, 0.32)  # bid <= entrada - SL

    def after():
        seen.update(state_dry())

    run_script(bot, monkeypatch, world, [lambda: None, drop, after])
    assert {t["result"] for t in seen["trades"]} == {"SL"} and all(t["pnl"] < 0 for t in seen["trades"])
    assert seen["loss_streak"] == len(bot.COINS) >= bot.CB_LOSSES
    assert seen["cooldown_until"] > time.time()


# ── regresión: el freno (CB) solo bloquea ENTRADAS nuevas; las posiciones abiertas se siguen gestionando ──
@pytest.fixture
def cb_world(bot, world, monkeypatch):
    monkeypatch.setattr(bot, "SL_MIN_HOLD_SECS", 0)
    monkeypatch.setattr(bot, "CB_LOSSES", 1)       # una sola pérdida activa el freno
    monkeypatch.setattr(bot, "CB_COOLDOWN", 3600)
    return world


def _trip_cb_on_btc(bot, book):
    """BTC cae hasta su SL (el ask no cambia: sigue 'tentando' a entrar); ETH se queda dentro de TP/SL."""
    book["btc_up"] = (0.30, 0.60)


def test_cb_on_btc_sl_does_not_unmanage_eth_tp(bot, cb_world, monkeypatch):
    book, seen = cb_world["book"], {}

    def trip():
        _trip_cb_on_btc(bot, book)

    def during_cooldown_1():
        st = state_dry()
        seen["after_trip"] = st
        book["eth_up"] = (0.69, 0.71)  # ETH alcanza TP estando en freno

    def after_tp():
        seen["after_tp"] = state_dry()
        seen["status"] = status.read_status()

    run_script(bot, monkeypatch, cb_world, [lambda: None, trip, during_cooldown_1, after_tp])
    t = seen["after_trip"]
    assert t["cooldown_until"] > time.time() and [x["result"] for x in t["trades"]] == ["SL"] and t["trades"][0]["coin"] == "btc"
    assert set(t["positions"]) == {"eth"}  # ETH sigue abierta al activarse el freno
    f = seen["after_tp"]
    assert [(x["coin"], x["result"]) for x in f["trades"]] == [("btc", "SL"), ("eth", "TP")]  # ETH salió por TP EN freno
    assert f["positions"] == {} and f["loss_streak"] == 0 and f["cooldown_until"] > time.time()  # el TP no levanta el freno
    assert seen["status"]["cooldown_until"] > time.time()


def test_cb_on_btc_sl_eth_still_hits_sl_in_cooldown(bot, cb_world, monkeypatch):
    book, seen = cb_world["book"], {}

    def trip():
        _trip_cb_on_btc(bot, book)

    def eth_crash():
        book["eth_up"] = (0.30, 0.60)

    def after():
        seen["st"] = state_dry()

    run_script(bot, monkeypatch, cb_world, [lambda: None, trip, eth_crash, after])
    st = seen["st"]
    assert [(x["coin"], x["result"]) for x in st["trades"]] == [("btc", "SL"), ("eth", "SL")]
    assert st["positions"] == {} and st["loss_streak"] == 2


def test_timeout_still_applies_during_cooldown(bot, cb_world, monkeypatch):
    book, seen = cb_world["book"], {}
    monkeypatch.setattr(bot, "POSITION_TIMEOUT", 1)

    def trip():
        _trip_cb_on_btc(bot, book)

    def wait():
        REAL_SLEEP(1.2)  # la posición de ETH supera POSITION_TIMEOUT

    def after():
        seen["st"] = state_dry()

    run_script(bot, monkeypatch, cb_world, [lambda: None, trip, wait, after])
    st = seen["st"]
    results = {(x["coin"], x["result"]) for x in st["trades"]}
    assert ("eth", "TIMEOUT") in results and "eth" not in st["positions"] and st["cooldown_until"] > time.time()


def test_no_new_entries_during_cooldown(bot, cb_world, monkeypatch):
    book, seen = cb_world["book"], {}

    def trip():
        _trip_cb_on_btc(bot, book)

    def eth_tp_and_btc_tempting():
        book["eth_up"] = (0.69, 0.71)
        book["btc_up"] = (0.58, 0.60)  # señal y precio válidos para BTC: sin freno entraría

    def idle():
        pass

    def check():
        seen["st"] = state_dry()
        seen["status"] = status.read_status()

    run_script(bot, monkeypatch, cb_world, [lambda: None, trip, eth_tp_and_btc_tempting, idle, idle, check])
    st = seen["st"]
    assert st["positions"] == {}                       # ni BTC ni ETH reabrieron
    assert sum(1 for o in st["last_orders"] if o.get("side") and "price" in o) == 2  # solo las 2 compras iniciales
    assert seen["status"]["coins"]["btc"]["signal"]["word"] == "cooldown"
    assert seen["status"]["coins"]["eth"]["signal"]["word"] == "cooldown"


def test_entries_resume_after_cooldown(bot, cb_world, monkeypatch):
    book, seen = cb_world["book"], {}
    monkeypatch.setattr(bot, "CB_COOLDOWN", 1)

    def trip():
        _trip_cb_on_btc(bot, book)

    def tempting_in_cooldown():
        book["btc_up"] = (0.58, 0.60)
        book["eth_up"] = (0.69, 0.71)

    def still_blocked():
        seen["blocked"] = dict(state_dry()["positions"])
        REAL_SLEEP(1.3)  # el freno de 1 s expira

    def resumed():
        seen["resumed"] = dict(state_dry()["positions"])
        seen["status"] = status.read_status()

    run_script(bot, monkeypatch, cb_world, [lambda: None, trip, tempting_in_cooldown, still_blocked, resumed])
    assert seen["blocked"] == {}
    assert "btc" in seen["resumed"] and seen["resumed"]["btc"]["dry"]  # entradas retomadas tras el freno
    assert seen["status"]["cooldown_until"] < time.time()


# ── proxy: aplicado a órdenes/claves siempre; a lecturas (Gamma/libro) solo con CARO5M_PROXY_SCAN ──
@pytest.mark.parametrize("scan", [False, True])
def test_proxy_scan_toggle(bot, world, monkeypatch, scan):
    config.env_file().write_text("POLYMARKET_PROXY_LIST=socks5://u:p@h.example:1080\n")
    applied, sessions = [], []
    monkeypatch.setattr(bot, "apply_proxy", lambda p: applied.append(p))
    monkeypatch.setitem(bot.CFG, "CARO5M_PROXY_SCAN", scan)
    monkeypatch.setattr(bot, "discover_market", lambda s, coin, n: (sessions.append(dict(s.proxies)), None)[1])
    run_script(bot, monkeypatch, world, [lambda: None, lambda: None])
    assert applied == ["socks5://u:p@h.example:1080"]          # órdenes y claves SIEMPRE por el proxy
    want = {"http": "socks5://u:p@h.example:1080", "https": "socks5://u:p@h.example:1080"} if scan else {}
    assert sessions and all(p == want for p in sessions)


def test_build_client_uses_stored_nonce(bot, monkeypatch):
    from test_bot_units import FakeClob

    class C(FakeClob):
        def create_or_derive_api_key(self, nonce=None):
            self.calls.append(("derive", nonce))
            return "CREDS"

    monkeypatch.setattr(bot, "ClobClient", C)
    env = {"POLY_PRIVATE_KEY": "0x" + "11" * 32, "POLY_SAFE_ADDRESS": "0x" + "22" * 20, "POLY_CLOB_NONCE": "2"}
    assert bot.build_client(env).calls == [("derive", 2)]
    env.pop("POLY_CLOB_NONCE")
    assert bot.build_client(env).calls == [("derive", None)]
    assert "POLY_CLOB_NONCE" in bot.SECRET_KEYS
