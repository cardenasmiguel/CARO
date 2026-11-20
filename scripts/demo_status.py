#!/usr/bin/env python3
"""Escribe un status.json DE DEMOSTRACIÓN (datos inventados) para probar o fotografiar el TUI sin bot.

    CARO5M_HOME=/tmp/demo python scripts/demo_status.py && CARO5M_HOME=/tmp/demo python -m caro5m

Nunca lo uses junto a un bot real: sobrescribe data/status.json.
"""
import time

from caro5m import config, status


def build(now: float | None = None, with_position: bool = True) -> dict:
    now = now or time.time()
    start = now - (now % 300)
    st = {
        "v": 1, "ts": now, "pid": 4242, "version": "1.0.0", "started_at": now - 5400, "tz": "Europe/Madrid",
        "mode": "dry", "paused": False, "cooldown_until": 0, "loss_streak": 1, "cb_losses": 2, "bet_size": 1.0,
        "settings": {"tp": 0.08, "sl": 0.2, "min_token": 0.45, "max_token": 0.7, "timeout": 360},
        "signature_type": 2,
        "wallet": {"configured": True, "balance_usdc": 12.34, "allowance_ok": True, "error": None, "checked_ts": now - 20, "signature_type": 2},
        "live_available": True, "window": {"start": start, "end": start + 300},
        "coins": {
            "btc": {"ptb": 62268.57, "chainlink": 62361.20, "delta_twap": 0.152, "up_bid": 0.62, "up_ask": 0.63, "dn_bid": 0.36,
                    "dn_ask": 0.38, "signal": {"ok": True, "side": "UP", "word": "ok", "reason": "SIGNAL UP", "ask": 0.63}},
            "eth": {"ptb": 1768.217, "chainlink": 1764.880, "delta_twap": -0.136, "up_bid": 0.31, "up_ask": 0.33, "dn_bid": 0.66,
                    "dn_ask": 0.72, "signal": {"ok": False, "side": "DOWN", "word": "margen", "reason": "wide_spread", "ask": 0.72}},
        },
        "positions": {},
        "today": {"pnl": 0.24, "wins": 2, "losses": 1},
        "trades": [
            {"ts_close": now - 1500, "coin": "btc", "side": "UP", "entry": 0.55, "exit": 0.63, "pnl": 0.1455, "result": "TP", "dry": True},
            {"ts_close": now - 2900, "coin": "eth", "side": "DOWN", "entry": 0.52, "exit": 0.32, "pnl": -0.3846, "result": "SL", "dry": True},
            {"ts_close": now - 4300, "coin": "btc", "side": "DOWN", "entry": 0.49, "exit": 0.57, "pnl": 0.1633, "result": "TP", "dry": True},
            {"ts_close": now - 6100, "coin": "eth", "side": "UP", "entry": 0.58, "exit": 0.66, "pnl": 0.1379, "result": "TP", "dry": True},
        ],
        "health": {"chainlink_age": 22, "binance_age": 1.2, "ptb": {"btc": {"quality": "ok"}, "eth": {"quality": "ok"}},
                   "proxy": {"configured": True, "latency_ms": 182, "ok": True}, "clob_ok": True, "gamma_ok": True, "loop_ms": 430},
        "warnings": [
            {"ts": now - 40, "level": "warn", "text": "Orden no ejecutada: no había liquidez suficiente a ese precio. No se ha gastado nada.", "count": 2},
            {"ts": now - 400, "level": "warn", "text": "El motor de órdenes de Polymarket se está reiniciando (425). Reintento automático.", "count": 1},
        ],
    }
    if with_position:
        st["positions"] = {"btc": {"coin": "btc", "side": "UP", "entry": 0.55, "bid": 0.59, "tp": 0.63, "sl": 0.35,
                                   "pct": 7.27, "age": 72, "size": 1.8182, "dry": True, "timeout": 360}}
    return st


def build_showcase(now: float | None = None, with_position: bool = True, version: str | None = None) -> dict:
    """Estado de DEMOSTRACIÓN para las capturas del README: 6 monedas, posición abierta, operaciones y avisos (todo inventado)."""
    from caro5m import __version__

    now = now or time.time()
    start = now - (now % 300)
    st = build(now, with_position=False)
    st.update({"version": version or __version__, "pid": 4242, "started_at": now - 7200, "loss_streak": 1,
               "keys_present": True, "coins_enabled": ["btc", "eth", "xrp", "sol", "doge", "hype"], "coins_managing": [],
               "coins_degraded": ["hype"], "restart_pending": [], "window": {"start": start, "end": start + 300}})
    sig = lambda ok, side, word, reason, ask=None: {"ok": ok, "side": side, "word": word, "reason": reason, "ask": ask}  # noqa: E731
    st["coins"] = {
        "btc": {"ptb": 62268.57, "chainlink": 62361.20, "delta_twap": 0.152, "up_bid": 0.62, "up_ask": 0.63, "dn_bid": 0.36,
                "dn_ask": 0.38, "feed_src": "chainlink", "signal": sig(False, "UP", "posición", "in_position", 0.63)},
        "eth": {"ptb": 1768.217, "chainlink": 1764.880, "delta_twap": -0.136, "up_bid": 0.28, "up_ask": 0.30, "dn_bid": 0.57,
                "dn_ask": 0.60, "feed_src": "chainlink", "signal": sig(False, "DOWN", "margen", "wide_spread", 0.60)},
        "xrp": {"ptb": 0.51874, "chainlink": 0.51962, "delta_twap": 0.118, "up_bid": 0.58, "up_ask": 0.60, "dn_bid": 0.39,
                "dn_ask": 0.41, "feed_src": "chainlink", "signal": sig(True, "UP", "ok", "SIGNAL UP", 0.60)},
        "sol": {"ptb": 138.42, "chainlink": 138.39, "delta_twap": -0.021, "up_bid": 0.47, "up_ask": 0.49, "dn_bid": 0.50,
                "dn_ask": 0.52, "feed_src": "chainlink", "signal": sig(False, None, "delta", "|d_twap|=0.0210 not in [0.1,0.3]")},
        "doge": {"ptb": 0.12385, "chainlink": 0.12403, "delta_twap": 0.104, "up_bid": 0.55, "up_ask": 0.57, "dn_bid": 0.42,
                 "dn_ask": 0.44, "feed_src": "chainlink", "signal": sig(False, "UP", "momento", "bin1m_no_confirm UP")},
        "hype": {"ptb": 24.871, "chainlink": 24.902, "delta_twap": 0.087, "up_bid": 0.52, "up_ask": 0.54, "dn_bid": 0.45,
                 "dn_ask": 0.47, "feed_src": "binance", "signal": sig(False, None, "ventana", "window secs=31 left=269")},
    }
    st["health"]["ptb"] = {c: {"quality": "ok"} for c in st["coins"]}
    st["today"] = {"pnl": 0.31, "wins": 3, "losses": 1}
    st["trades"] = [
        {"ts_close": now - 900, "coin": "xrp", "side": "DOWN", "entry": 0.58, "exit": 0.66, "pnl": 0.1379, "result": "TP", "dry": True},
        {"ts_close": now - 1500, "coin": "btc", "side": "UP", "entry": 0.55, "exit": 0.63, "pnl": 0.1455, "result": "TP", "dry": True},
        {"ts_close": now - 2900, "coin": "eth", "side": "DOWN", "entry": 0.52, "exit": 0.32, "pnl": -0.3846, "result": "SL", "dry": True},
        {"ts_close": now - 4300, "coin": "sol", "side": "UP", "entry": 0.49, "exit": 0.57, "pnl": 0.1633, "result": "TP", "dry": True},
        {"ts_close": now - 5200, "coin": "doge", "side": "UP", "entry": 0.61, "exit": None, "pnl": None, "result": "TIMEOUT", "dry": True},
        {"ts_close": now - 6100, "coin": "eth", "side": "UP", "entry": 0.58, "exit": 0.66, "pnl": 0.2379, "result": "TP", "dry": True},
    ]
    st["warnings"] = [
        {"ts": now - 40, "level": "warn", "text": "Orden no ejecutada: no había liquidez suficiente a ese precio. No se ha gastado nada.", "count": 2},
        {"ts": now - 400, "level": "warn", "text": "El motor de órdenes de Polymarket se está reiniciando (425). Reintento automático.", "count": 1},
        {"ts": now - 1300, "level": "warn", "text": "Tiempo de espera agotado hablando con el servidor. Reintentando.", "count": 3},
    ]
    if with_position:
        st["positions"] = {"btc": {"coin": "btc", "side": "UP", "entry": 0.55, "bid": 0.59, "tp": 0.63, "sl": 0.35, "tp_capped": False,
                                   "pct": 7.27, "age": 72, "size": 1.8182, "dry": True, "timeout": 360}}
    return st


if __name__ == "__main__":
    status.write_status(build())
    print("status de demostración escrito en", config.status_path())
