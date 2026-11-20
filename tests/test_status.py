import os
import time

from caro5m import config, status


def test_pause_roundtrip():
    assert not status.is_paused()
    status.set_paused(True)
    assert status.is_paused()
    status.set_paused(False)
    assert not status.is_paused()
    status.set_paused(False)  # idempotente


def test_mode_request_and_clear():
    assert status.get_mode_request() is None
    status.set_mode_request("live")
    assert status.get_mode_request() == "live"
    status.clear_mode_request()
    assert status.get_mode_request() is None


def test_mode_request_rejects_garbage():
    import pytest

    with pytest.raises(ValueError):
        status.set_mode_request("yolo")


def test_sell_request_consumed_once_and_expires():
    status.request_sell(["btc"])
    req = status.consume_sell()
    assert req["coins"] == ["btc"]
    assert status.consume_sell() is None
    status.request_sell(["btc"])
    f = config.control_dir() / "sell_now"
    old = time.time() - 600
    os.utime(f, (old, old))
    import json

    d = json.loads(f.read_text()); d["ts"] = old; f.write_text(json.dumps(d))
    assert status.consume_sell() is None  # caducada: no vende


def test_status_roundtrip_and_staleness():
    assert status.read_status() is None and not status.bot_running(None)
    status.write_status({"ts": time.time(), "pid": 1})
    st = status.read_status()
    assert status.bot_running(st)
    assert not status.bot_running({"ts": time.time() - 60})
