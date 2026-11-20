import os
import stat

import pytest

from caro5m import config


def test_defaults_are_original_bot_values():
    s = config.load_settings()
    assert s["DRY_RUN"] is True  # por defecto SIEMPRE simulación
    assert (s["BET_SIZE"], s["TP_OFFSET"], s["SL_OFFSET"]) == (1.0, 0.08, 0.20)
    assert (s["CB_LOSSES"], s["CB_COOLDOWN"], s["POSITION_TIMEOUT"]) == (2, 3600, 360)
    assert s["AUTO_REDEEM"] is True
    assert (s["MIN_TOKEN"], s["MAX_TOKEN"]) == (0.45, 0.70)
    assert s["POLY_SIGNATURE_TYPE"] == 2 and s["CHAIN_ID"] == 137
    assert s["CARO5M_TZ"] == "Europe/Madrid"
    assert len(s["POLYGON_RPCS"]) >= 2


def test_layering_env_over_file_over_default(isolated_env, monkeypatch):
    config.settings_file().parent.mkdir(parents=True)
    config.settings_file().write_text("BET_SIZE=2.5\nTP_OFFSET=0.05\n")
    assert config.load_settings()["BET_SIZE"] == 2.5
    monkeypatch.setenv("BET_SIZE", "3")
    assert config.load_settings()["BET_SIZE"] == 3.0
    assert config.load_settings()["TP_OFFSET"] == 0.05


def test_invalid_value_falls_back(isolated_env, monkeypatch):
    monkeypatch.setenv("BET_SIZE", "0.1")  # por debajo del mínimo
    monkeypatch.setenv("CB_LOSSES", "abc")
    s = config.load_settings()
    assert s["BET_SIZE"] == 1.0 and s["CB_LOSSES"] == 2


def test_alias_and_decimal_comma(monkeypatch):
    monkeypatch.setenv("GROK_PM_PTB", "0")
    assert config.load_settings()["CARO5M_USE_PTB"] is False
    monkeypatch.setenv("BET_SIZE", "1,5")
    assert config.load_settings()["BET_SIZE"] == 1.5


def test_save_settings_only_known_keys_and_preserves_rest(isolated_env):
    p = config.settings_file()
    p.parent.mkdir(parents=True)
    p.write_text("# mi nota\nBET_SIZE=1.00\nOTRA_CLAVE=conservame\n")
    out = config.save_settings({"BET_SIZE": "2", "SL_OFFSET": "0.15"})
    txt = p.read_text()
    assert "# mi nota" in txt and "OTRA_CLAVE=conservame" in txt
    assert out["BET_SIZE"] == 2.0 and out["SL_OFFSET"] == 0.15
    with pytest.raises(ValueError):
        config.save_settings({"POLY_PRIVATE_KEY": "0x" + "1" * 64})  # un secreto NUNCA va a settings
    assert "POLY_PRIVATE_KEY" not in p.read_text()


def test_save_rejects_bad_values(isolated_env):
    with pytest.raises(ValueError):
        config.save_settings({"BET_SIZE": "0.5"})
    with pytest.raises(ValueError):
        config.save_settings({"MIN_TOKEN": "0.8", "MAX_TOKEN": "0.6"})
    with pytest.raises(ValueError):
        config.save_settings({"POLYGON_RPCS": "ftp://x"})
    with pytest.raises(ValueError):
        config.save_settings({"CARO5M_TZ": "Mars/Base"})


def test_shm_dir_override_and_paths(isolated_env):
    assert config.shm_dir() == isolated_env / "shm"
    assert config.state_path("dry").name == "state_dry.json" and config.state_path("live").name == "state_live.json"
    assert config.ptb_shm_path() == isolated_env / "shm" / "pm_ptb_latest.json"


def test_atomic_write_permissions(isolated_env):
    p = isolated_env / "x" / "f"
    config.atomic_write_text(p, "a", 0o600)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_example_settings_file_parses_with_inline_comments():
    from pathlib import Path

    ex = Path(__file__).resolve().parent.parent / "config" / "settings.env.example"
    s = config.load_settings(ex, environ={})
    d = config.load_settings(Path("/nonexistent"), environ={})
    assert s == d  # el ejemplo reproduce exactamente los valores por defecto
    assert s["BINANCE_HOST"] == "https://api.binance.com"
