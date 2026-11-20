from caro5m import messages as m


def test_friendly_known_errors():
    assert "Saldo insuficiente" in m.friendly("ORDER_ERR BTC UP: PolyApiException[status_code=400, error_message={'error': 'not enough balance / allowance'}]")[1]
    lvl, txt = m.friendly("ORDER_ERR ETH DOWN: status_code=503 trading is post-only")
    assert "503" in txt and lvl == "warn"
    assert "425" in m.friendly("ORDER_ERR BTC UP: 425 Too Early")[1]
    assert "429" in m.friendly("ORDER_ERR x: 429 Too Many Requests")[1]
    assert "liquidez" in m.friendly("ORDER_ERR BTC UP: order couldn't be fully filled. FOK orders are fully filled or killed")[1]
    assert "condiciones de uso" in m.friendly("ORDER_ERR: 403 Trading restricted in your region, geoblock")[1]
    assert "Freno" in m.friendly("CIRCUIT_BREAKER streak=2 cooldown=3600s")[1]


def test_friendly_unknown_is_trimmed():
    lvl, txt = m.friendly("ORDER_ERR BTC UP: " + "x" * 400)
    assert len(txt) <= 160


def test_live_unavailable_passthrough():
    lvl, txt = m.friendly("LIVE_UNAVAILABLE no se activa el modo real: saldo insuficiente (0.50 USDC)")
    assert lvl == "error" and txt.startswith("No se activa el modo real")


def test_is_warning_line():
    assert m.is_warning_line("ORDER_ERR BTC UP: boom")
    assert not m.is_warning_line("SCAN BTC slug=x | SIGNAL UP")


def test_short_reason_words():
    cases = {
        "binance_stale=12.0": "datos", "chainlink_stale=50": "datos", "window secs=10 left=290": "ventana",
        "|d_twap|=0.0500 not in [0.1,0.3]": "delta", "down_mild_opp_cold bin1m=+0.01 rsi=30": "momento", "twap_spot_disagree": "PTB", "bin1m_contra UP": "momento",
        "rsi_veto_up": "rsi", "cooldown": "cooldown", "wide_spread": "margen", "ask=0.8 not in [0.45,0.7]": "precio",
        "interval_mismatch": "ventana", "algo raro": "filtro",
    }
    for raw, word in cases.items():
        assert m.short_reason(raw) == word, raw
