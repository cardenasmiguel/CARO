"""Paridad de la lógica de trading con el script original (opcional).

Se activa con CARO5M_ORIGINAL=/ruta/al/script_original.py. Compara, con entradas aleatorias, que
`evaluate_signal` (incluido el filtro anti-fakeout) da exactamente el mismo veredicto y que las
constantes de estrategia coinciden. Sin esa variable la prueba se omite (el repo no incluye el original).
"""
import importlib.util
import os
import random

import pytest

ORIG = os.environ.get("CARO5M_ORIGINAL")
pytestmark = pytest.mark.skipif(not ORIG or not os.path.exists(ORIG or ""), reason="CARO5M_ORIGINAL no definido")

CONSTS = [
    "ENTRY_SECS_MIN", "ENTRY_SECS_MAX", "ENTRY_SECS_LEFT_MIN", "DELTA_MIN", "DELTA_MAX", "DELTA_STRONG", "NEAR_ZERO",
    "BIN1M_CONTRA_MAX", "BIN1M_CONFIRM_MIN", "BIN1M_FLAT_MAX", "BIN1M_UP_FLOOR", "RSI_UP_MAX", "RSI_DOWN_MIN",
    "RSI_DOWN_HIGH_MAX", "LANE_A_DELTA_MIN", "LANE_A_SECS_MAX", "LANE_A_BIN1M_MIN", "LANE_A_MAX_ASK",
    "RICH_ASK_THRESHOLD", "SPREAD_SKIP", "SL_MIN_HOLD_SECS", "BINANCE_STALE_MAX", "CHAINLINK_STALE_MAX",
    "BET_SIZE", "TP_OFFSET", "SL_OFFSET", "CB_LOSSES", "CB_COOLDOWN", "MIN_TOKEN", "MAX_TOKEN", "POSITION_TIMEOUT",
]


def load_original():
    spec = importlib.util.spec_from_file_location("orig_bot", ORIG)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_constants_and_signal_parity(bot, monkeypatch):
    orig = load_original()
    for name in CONSTS:
        if hasattr(orig, name):
            assert getattr(bot, name) == getattr(orig, name), name
    for m in (bot, orig):
        monkeypatch.setattr(m, "log", lambda *_a, **_k: None)
    rnd = random.Random(7)
    n_sig = 0
    for i in range(20000):
        now = 1_800_000_000.0 + i * 3
        coin = rnd.choice(["btc", "eth"])
        cl = {
            "secs_into": rnd.uniform(30, 140), "secs_remaining": rnd.uniform(100, 270), "stale_secs": rnd.choice([0, 0, 1, 40]),
            "updated_at": now - rnd.choice([0, 1, 2, 30]), "delta_twap_pct": rnd.uniform(-0.25, 0.25),
            "delta_pct": rnd.uniform(-0.25, 0.25), "direction_twap": rnd.choice(["UP", "DOWN", ""]),
        }
        bn = {"ts": now - rnd.choice([0, 1, 20]), "change_1m": rnd.uniform(-0.12, 0.12), "rsi14": rnd.uniform(15, 85)}
        up, dn = rnd.uniform(0.3, 0.8), rnd.uniform(0.3, 0.8)
        for m in (bot, orig):
            m.record_market_tick(coin, now, bn["change_1m"], cl["delta_twap_pct"], up, dn)
        a = bot.evaluate_signal(coin, cl, bn, now, up_ask=up, dn_ask=dn)
        b = orig.evaluate_signal(coin, cl, bn, now, up_ask=up, dn_ask=dn)
        assert a[:2] == b[:2], (i, a, b)
        n_sig += a[0] is not None
    assert n_sig > 30  # el muestreo sí alcanza señales reales (no es una prueba vacía)
