"""v1.2.6: proxy sin preferencia de país, venta al vencer el tiempo, cobro EOA, etiquetas de saldo y margen del libro."""

import time

import pytest

from caro5m import account, config, redeem, messages
from caro5m.tui import render


CID = "0x" + "ab" * 32
PK = "0x" + "11" * 32


def test_pick_proxy_uses_configured_order_not_country(bot):
    assert bot.pick_proxy("") == ""
    assert bot.pick_proxy("http://de.example:8080,http://nl-proxy.example:9") == "http://de.example:8080"
    assert bot.pick_proxy("socks5h://amsterdam.example:1;http://first.example:2") == "socks5h://amsterdam.example:1"
    # un host «nl» en segundo lugar ya no gana
    assert bot.pick_proxy("http://us.example:1,http://netherlands-gw.example:2") == "http://us.example:1"
    assert bot.pick_proxy("host:8080") == "http://host:8080"


def test_auto_redeem_default_is_on_and_hot():
    s = config.load_settings()
    assert s["AUTO_REDEEM"] is True
    assert config.SPEC_BY_KEY["AUTO_REDEEM"].hot is True


def test_balance_labels_by_wallet_type_es_and_en():
    assert render.balance_label(0) == "Saldo cartera"
    assert render.balance_label(1) == "Saldo cuenta (email)"
    assert render.balance_label(2) == "Saldo Safe"
    assert render.balance_label(0, "en") == "Wallet balance"
    assert render.balance_label(1, "en") == "Account balance (email)"
    assert render.balance_label(2, "en") == "Safe balance"
    assert render.balance_label(None) == "Saldo Safe"
    assert render.balance_label("nope") == "Saldo Safe"


def test_status_line_uses_wallet_type_label():
    now = time.time()
    st = {"wallet": {"configured": True, "balance_usdc": 12.34}, "bet_size": 1, "today": {}, "signature_type": 0, "loss_streak": 0, "cb_losses": 2}
    assert "Saldo cartera:" in render.status_line(st, now).plain
    st["signature_type"] = 1
    assert "Saldo cuenta (email):" in render.status_line(st, now).plain
    st["signature_type"] = 2
    assert "Saldo Safe:" in render.status_line(st, now).plain


def test_403_message_mentions_tos_and_not_a_proxy_bypass():
    lvl, txt = messages.friendly("ORDER_ERR: 403 Trading restricted in your region, geoblock")
    assert lvl == "error"
    assert "condiciones de uso" in txt and "403" in txt
    assert "proxy" not in txt.lower()
    assert "W]" not in txt


def test_short_reason_wide_spread_is_book_margin():
    assert messages.short_reason("wide_spread") == "margen"
    assert messages.short_reason("spread") == "margen"


def test_describe_probe_blocked_does_not_advise_bypass():
    r = {"via": "directo", "ok": False, "blocked": True, "country": "DE", "region": None, "latency_ms": 80, "error": None}
    lvl, t = account.describe_probe(r)
    assert lvl == "fail" and "BLOQUEA" in t and "DE" in t
    assert "condiciones de uso" in t
    assert "país permitido" not in t
    assert "Necesitas un proxy" not in t


def test_explain_clob_403_is_tos_not_bypass():
    class E(Exception):
        status_code = 403
        error_msg = "Trading restricted in your region"
    t = account.explain_clob_error(E())
    assert "región" in t or "países" in t
    assert "condiciones de uso" in t
    assert "país permitido" not in t
    assert "Usa un proxy" not in t


def test_encode_redeem_positions_layout():
    data = redeem.encode_redeem_positions("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB", CID, 1)
    raw = bytes.fromhex(data[2:])
    assert raw[:4].hex() == redeem.REDEEM_SELECTOR
    assert len(raw) == 4 + 32 * 6
    # offset del array = 128
    assert int.from_bytes(raw[4+96:4+128], "big") == 128
    assert int.from_bytes(raw[4+128:4+160], "big") == 1
    assert int.from_bytes(raw[-32:], "big") == 1
    down = redeem.encode_redeem_positions("0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB", CID, 2)
    assert int.from_bytes(bytes.fromhex(down[2:])[-32:], "big") == 2


def test_resolution_for_side_and_index_set():
    m = {"closed": True, "outcomes": ["Up", "Down"], "outcomePrices": '["1", "0"]'}
    assert redeem.resolution_for_side(m, "UP") == "won"
    assert redeem.resolution_for_side(m, "DOWN") == "lost"
    assert redeem.index_set_for_side("UP", ["Up", "Down"]) == 1
    assert redeem.index_set_for_side("DOWN", ["Up", "Down"]) == 2
    assert redeem.resolution_for_side({"closed": False, "outcomePrices": ["1", "0"]}, "UP") == "open"
    assert redeem.resolution_for_side({"closed": True}, "UP") == "unknown"
    assert redeem.redeem_supported(0) and not redeem.redeem_supported(1) and not redeem.redeem_supported(2)


def test_redeem_eoa_dry_never_touches_chain(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("no debería hablar con la cadena en DRY_RUN")
    monkeypatch.setattr(account, "_rpc", boom)
    r = redeem.redeem_eoa(PK, CID, 1, ["http://127.0.0.1:9"], dry=True)
    assert r["ok"] and r["dry_run"] and r["tx"] is None


def test_redeem_eoa_live_uses_mocked_rpc_only(monkeypatch):
    calls = []

    def fake_rpc(url, method, params, timeout=15.0):
        calls.append(method)
        if method == "eth_gasPrice":
            return hex(int(30e9))
        if method == "eth_getBalance":
            return hex(int(1e18))
        if method == "eth_getTransactionCount":
            return "0x1"
        if method == "eth_estimateGas":
            return hex(200000)
        if method == "eth_sendRawTransaction":
            assert isinstance(params[0], str) and params[0].startswith("0x") and len(params[0]) > 10
            return "0x" + "cd" * 32
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1"}
        raise AssertionError(method)

    monkeypatch.setattr(account, "_rpc", fake_rpc)
    monkeypatch.setattr(redeem, "_rpc_sleep", lambda s: None)
    r = redeem.redeem_eoa(PK, CID, 1, ["http://127.0.0.1:9"], dry=False, wait=True)
    assert r["ok"] and r["tx"] == "0x" + "cd" * 32 and r["status"] == "confirmada"
    assert "eth_sendRawTransaction" in calls
    # la clave no viaja en el resultado
    assert PK not in str(r) and "11" * 8 not in str(r).replace("0x", "")


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _Sess:
    def __init__(self, payload):
        self.payload = payload
        self.urls = []

    def get(self, url, params=None, timeout=None):
        self.urls.append((url, params))
        return _Resp(self.payload)


def _won_market(side="Up"):
    prices = '["1", "0"]' if side == "Up" else '["0", "1"]'
    return [{
        "slug": "btc-updown-5m-X", "acceptingOrders": False, "closed": True,
        "conditionId": CID, "outcomes": ["Up", "Down"],
        "clobTokenIds": ["tok-up", "tok-dn"], "outcomePrices": prices,
    }]


def _open_market():
    m = _won_market()[0]
    m["closed"] = False
    m["acceptingOrders"] = True
    m["outcomePrices"] = '["0.55", "0.45"]'
    return [m]


def _lost_market():
    return _won_market("Down")  # UP pierde


def _rt(bot, mode="dry"):
    rt = bot.Runtime(mode)
    rt.client = object() if mode == "live" else None
    return rt


def _pos(dry=True, **k):
    now = time.time()
    d = {"coin": "btc", "side": "UP", "price": 0.55, "size": 1.8182, "ts": now - 400,
         "dry": dry, "slug": "btc-updown-5m-X", "token_id": "tok-up", "condition_id": CID, "index_set": 1}
    d.update(k)
    return d


def test_timeout_sells_at_best_bid_in_dry(bot):
    rt, st, now = _rt(bot), {"positions": {"btc": _pos()}}, time.time()
    action = bot.close_on_timeout(rt, st, _Sess(_open_market()), "btc", st["positions"]["btc"], "tok-up", 0.42, now)
    assert action == "sold"
    assert "btc" not in st["positions"]
    tr = st["trades"][-1]
    assert tr["result"] == "TIMEOUT" and tr["exit"] == 0.42 and tr["pnl"] is not None


def test_timeout_keeps_position_if_market_still_open_and_no_fill(bot, monkeypatch):
    monkeypatch.setattr(bot, "place_fok", lambda *a, **k: {"status": "killed"})
    rt, st = _rt(bot), {"positions": {"btc": _pos()}}
    pos = st["positions"]["btc"]
    action = bot.close_on_timeout(rt, st, _Sess(_open_market()), "btc", pos, "tok-up", 0.40, time.time())
    assert action == "retry" and "btc" in st["positions"] and not st["positions"]["btc"].get("pending_redeem")


def test_timeout_resolved_loss_records_zero_exit(bot, monkeypatch):
    monkeypatch.setattr(bot, "place_fok", lambda *a, **k: {"status": "killed"})
    rt, st = _rt(bot), {"positions": {"btc": _pos()}}
    action = bot.close_on_timeout(rt, st, _Sess(_lost_market()), "btc", st["positions"]["btc"], "tok-up", 0.01, time.time())
    assert action == "lost" and "btc" not in st["positions"]
    tr = st["trades"][-1]
    assert tr["result"] == "TIMEOUT" and tr["exit"] == 0.0 and tr["pnl"] < 0


def test_timeout_eoa_dry_auto_redeem_when_won(bot, monkeypatch):
    monkeypatch.setattr(bot, "SIGNATURE_TYPE", 0)
    monkeypatch.setattr(bot, "AUTO_REDEEM", True)
    monkeypatch.setattr(bot, "place_fok", lambda *a, **k: {"status": "killed"})
    rt, st = _rt(bot, "dry"), {"positions": {"btc": _pos(dry=True)}}
    action = bot.close_on_timeout(rt, st, _Sess(_won_market()), "btc", st["positions"]["btc"], "tok-up", None, time.time())
    assert action == "redeemed" and "btc" not in st["positions"]
    tr = st["trades"][-1]
    assert tr["result"] == "REDEEM" and tr["exit"] == 1.0 and tr["pnl"] > 0


def test_timeout_safe_wallet_stays_pending_claim(bot, monkeypatch):
    monkeypatch.setattr(bot, "SIGNATURE_TYPE", 2)
    monkeypatch.setattr(bot, "AUTO_REDEEM", True)
    monkeypatch.setattr(bot, "place_fok", lambda *a, **k: {"status": "killed"})
    rt, st = _rt(bot, "live"), {"positions": {"btc": _pos(dry=False)}}
    pos = st["positions"]["btc"]
    action = bot.close_on_timeout(rt, st, _Sess(_won_market()), "btc", pos, "tok-up", None, time.time())
    assert action == "pending" and st["positions"]["btc"].get("pending_redeem") is True
    # el panel debe mostrar el aviso (sin descartar)
    status = bot.build_status(rt, st, time.time(), 0)
    assert status["positions"]["btc"]["pending_redeem"]
    assert "pendiente de cobrar en polymarket.com" in status["positions"]["btc"]["claim_hint"]
    panel = render.position_panel(status, time.time(), "Europe/Madrid")
    texts = []
    for r in panel.renderables:
        texts.append(getattr(r, "plain", str(r)))
    assert any("pendiente de cobrar en polymarket.com" in x for x in texts)


def test_timeout_magic_wallet_also_pending(bot, monkeypatch):
    monkeypatch.setattr(bot, "SIGNATURE_TYPE", 1)
    monkeypatch.setattr(bot, "place_fok", lambda *a, **k: {"status": "killed"})
    rt, st = _rt(bot, "live"), {"positions": {"btc": _pos(dry=False)}}
    action = bot.close_on_timeout(rt, st, _Sess(_won_market()), "btc", st["positions"]["btc"], "tok-up", None, time.time())
    assert action == "pending"


def test_pending_redeem_clears_when_ctf_balance_is_zero(bot, monkeypatch):
    monkeypatch.setattr(bot, "SIGNATURE_TYPE", 2)
    monkeypatch.setattr(redeem, "ctf_balance", lambda *a, **k: 0)
    monkeypatch.setattr(bot, "funder_address", lambda env: "0x" + "22" * 20)
    rt, st = _rt(bot, "live"), {"positions": {"btc": _pos(dry=False, pending_redeem=True)}}
    action = bot.close_on_timeout(rt, st, _Sess(_won_market()), "btc", st["positions"]["btc"], "tok-up", None, time.time())
    assert action == "redeemed_ext" and "btc" not in st["positions"]
    assert st["trades"][-1]["result"] == "REDEEM"


def test_eoa_auto_redeem_off_stays_pending(bot, monkeypatch):
    monkeypatch.setattr(bot, "SIGNATURE_TYPE", 0)
    monkeypatch.setattr(bot, "AUTO_REDEEM", False)
    monkeypatch.setattr(bot, "place_fok", lambda *a, **k: {"status": "killed"})
    rt, st = _rt(bot, "live"), {"positions": {"btc": _pos(dry=False)}}
    action = bot.close_on_timeout(rt, st, _Sess(_won_market()), "btc", st["positions"]["btc"], "tok-up", None, time.time())
    assert action == "pending" and st["positions"]["btc"]["pending_redeem"]


def test_eoa_live_redeem_mocked_chain(bot, monkeypatch):
    monkeypatch.setattr(bot, "SIGNATURE_TYPE", 0)
    monkeypatch.setattr(bot, "AUTO_REDEEM", True)
    monkeypatch.setattr(bot, "place_fok", lambda *a, **k: {"status": "killed"})
    monkeypatch.setattr(redeem, "ctf_balance", lambda *a, **k: 10**6)
    monkeypatch.setattr(bot, "read_secrets", lambda: {"POLY_PRIVATE_KEY": PK})
    sent = {}

    def fake_redeem(pk, cid, index_set, rpcs, chain_id=137, *, dry=False, wait=True, collateral=None):
        sent["pk"] = pk
        sent["cid"] = cid
        sent["dry"] = dry
        return {"ok": True, "dry_run": False, "tx": "0x" + "ee" * 32, "status": "confirmada", "error": None}

    monkeypatch.setattr(redeem, "redeem_eoa", fake_redeem)
    rt, st = _rt(bot, "live"), {"positions": {"btc": _pos(dry=False)}}
    action = bot.close_on_timeout(rt, st, _Sess(_won_market()), "btc", st["positions"]["btc"], "tok-up", None, time.time())
    assert action == "redeemed" and sent["pk"] == PK and sent["dry"] is False and sent["cid"] == CID
    assert "btc" not in st["positions"] and st["trades"][-1]["result"] == "REDEEM"
