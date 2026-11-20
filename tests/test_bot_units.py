import json
import time

import pytest

from caro5m import config, status


class FakeClob:
    instances = []

    def __init__(self, host, chain_id=None, key=None, signature_type=None, funder=None):
        self.args = dict(host=host, chain_id=chain_id, key=key, signature_type=signature_type, funder=funder)
        self.calls = []
        self.creds = None
        FakeClob.instances.append(self)

    def create_or_derive_api_key(self):
        self.calls.append("create_or_derive_api_key")
        return "CREDS"

    def set_api_creds(self, c):
        self.creds = c


def test_place_fok_dry_never_needs_a_client(bot):
    r = bot.place_fok(None, "tok", bot.BUY, 0.50, 2.0, dry=True)
    assert r["status"] == "matched" and r["dry_run"] and bot.order_filled(r)
    assert float(r["takingAmount"]) == round(bot.BET_SIZE / 0.50, 4)
    r = bot.place_fok(None, "tok", bot.SELL, 0.60, 2.0, dry=True)
    assert bot.order_filled(r) and r["dry_run"]


def test_place_fok_real_without_client_fails_closed(bot):
    with pytest.raises(RuntimeError):
        bot.place_fok(None, "tok", bot.BUY, 0.50, 2.0, dry=False)


def test_build_client_derives_creds_with_the_right_method(bot, monkeypatch):
    monkeypatch.setattr(bot, "ClobClient", FakeClob)
    env = {"POLY_PRIVATE_KEY": "0x" + "11" * 32, "POLY_SAFE_ADDRESS": "0x" + "22" * 20}
    c = bot.build_client(env)
    assert c.calls == ["create_or_derive_api_key"] and c.creds == "CREDS"
    assert c.args["signature_type"] == 2 and c.args["funder"] == "0x" + "22" * 20


def test_build_client_requires_key_and_funder(bot):
    with pytest.raises(RuntimeError):
        bot.build_client({})
    with pytest.raises(RuntimeError):
        bot.build_client({"POLY_PRIVATE_KEY": "0x" + "11" * 32})


def test_funder_aliases(bot):
    assert bot.funder_address({"POLY_FUNDER_ADDRESS": "0xAA"}) == "0xAA"
    assert bot.funder_address({"POLY_SAFE_ADDRESS": "0xBB", "POLY_FUNDER_ADDRESS": "0xAA"}) == "0xBB"


def test_no_proxy_is_fine(bot):
    assert bot.pick_proxy("") == ""


def test_push_order_is_bounded(bot):
    st = {}
    for i in range(500):
        bot.push_order(st, {"i": i})
    assert len(st["last_orders"]) == bot.MAX_LAST_ORDERS and st["last_orders"][-1]["i"] == 499


def test_record_trade_and_today_stats(bot):
    st = {}
    now = time.time()
    pos = {"coin": "btc", "side": "UP", "price": 0.5, "size": 2.0, "ts": now - 60, "dry": True}
    bot.record_trade(st, pos, 0.58, "TP", now)
    bot.record_trade(st, pos, 0.30, "SL", now)
    bot.record_trade(st, pos, None, "TIMEOUT", now)
    t = st["trades"]
    assert t[0]["pnl"] == pytest.approx(0.16) and t[1]["pnl"] == pytest.approx(-0.4) and t[2]["pnl"] is None
    s = bot.today_stats(st, now)
    assert s == {"pnl": pytest.approx(-0.24), "wins": 1, "losses": 1}
    for _ in range(300):
        bot.record_trade(st, pos, 0.6, "TP", now)
    assert len(st["trades"]) == 200


def test_state_is_separate_per_mode_and_bounded(bot):
    bot.set_state_mode("dry")
    st = bot.load_state()
    st["positions"]["btc"] = {"x": 1}
    st["last_orders"] = list(range(400))
    bot.save_state(st)
    assert len(json.loads(config.state_path("dry").read_text())["last_orders"]) <= bot.MAX_LAST_ORDERS
    bot.set_state_mode("live")
    assert bot.load_state()["positions"] == {}
    bot.set_state_mode("dry")
    assert "btc" in bot.load_state()["positions"]


def test_ensure_live_refuses_without_keys(bot):
    rt = bot.Runtime("dry")
    ok, why = bot.ensure_live(rt, {})
    assert not ok and "asistente" in why and rt.client is None


def test_ensure_live_refuses_low_balance_and_no_allowance(bot, monkeypatch):
    from caro5m import checks

    (config.env_file()).write_text("POLY_PRIVATE_KEY=0x" + "11" * 32 + "\nPOLY_SAFE_ADDRESS=0x" + "22" * 20 + "\n")
    monkeypatch.setattr(bot, "ClobClient", FakeClob)
    rt = bot.Runtime("dry")
    monkeypatch.setattr(checks, "check_wallet", lambda *a, **k: {"configured": True, "balance_usdc": 0.5, "allowance_ok": True, "error": None})
    ok, why = bot.ensure_live(rt, {})
    assert not ok and "saldo insuficiente" in why and rt.client is None
    monkeypatch.setattr(checks, "check_wallet", lambda *a, **k: {"configured": True, "balance_usdc": 50, "allowance_ok": False, "error": None})
    ok, why = bot.ensure_live(rt, {})
    assert not ok and "allowance" in why
    monkeypatch.setattr(checks, "check_wallet", lambda *a, **k: {"configured": True, "balance_usdc": 50, "allowance_ok": None, "error": "rpc caído"})
    ok, why = bot.ensure_live(rt, {})
    assert not ok and "rpc" in why
    monkeypatch.setattr(checks, "check_wallet", lambda *a, **k: {"configured": True, "balance_usdc": 50, "allowance_ok": True, "error": None})
    ok, why = bot.ensure_live(rt, {})
    assert ok and rt.client is not None


def test_handle_controls_mode_rules(bot, monkeypatch):
    rt = bot.Runtime("dry")
    bot.set_state_mode("dry")
    st = bot.load_state()
    # sin claves: "live" se rechaza y queda en PRUEBA
    status.set_mode_request("live")
    st = bot.handle_controls(rt, {}, st)
    assert rt.mode == "dry" and status.get_mode_request() is None
    assert any("No se activó el modo REAL" in str(w) for w in bot.WARNINGS)  # 1.2.3: texto nuevo, REAL en mayúsculas
    # con posición abierta no se cambia de modo
    monkeypatch.setattr(bot, "ensure_live", lambda rt, env: (True, "ok"))
    st["positions"]["btc"] = {"coin": "btc"}
    status.set_mode_request("live")
    st = bot.handle_controls(rt, {}, st)
    assert rt.mode == "dry"
    st["positions"] = {}
    status.set_mode_request("live")
    st = bot.handle_controls(rt, {}, st)
    assert rt.mode == "live"
    status.set_mode_request("dry")
    st = bot.handle_controls(rt, {}, st)
    assert rt.mode == "dry"


def test_handle_controls_pause_and_sell(bot):
    rt = bot.Runtime("dry")
    st = {"positions": {}}
    status.set_paused(True)
    bot.handle_controls(rt, {}, st)
    assert rt.paused
    status.set_paused(False)
    bot.handle_controls(rt, {}, st)
    assert not rt.paused
    status.request_sell(["btc"])
    bot.handle_controls(rt, {}, st)
    assert rt.sell_coins == {"btc"}


def test_read_pm_ptb_paths(bot, isolated_env):
    now = time.time()
    c, why = bot.read_pm_ptb("btc", int(now) - int(now) % 300, now)
    assert c is None  # sin fichero: no rompe, devuelve motivo
    assert isinstance(why, str)
