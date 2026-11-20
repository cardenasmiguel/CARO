import os
import stat

import pytest

from caro5m import config, wizard

PK = "0x" + "ab" * 32
ADDR = "0x" + "12" * 20


def test_validation():
    assert wizard.validate_private_key(PK[2:]) == PK
    for bad in ("", "0x123", "zz" * 32):
        with pytest.raises(ValueError):
            wizard.validate_private_key(bad)
    assert wizard.validate_address(ADDR) == ADDR
    with pytest.raises(ValueError):
        wizard.validate_address("0x123")
    assert wizard.validate_proxy("") == ""
    with pytest.raises(ValueError):
        wizard.validate_proxy("sin-puerto")


def test_write_env_is_0600_and_merges(isolated_env):
    p = wizard.write_env({"POLY_PRIVATE_KEY": PK, "POLY_SAFE_ADDRESS": ADDR, "NO_PERMITIDA": "x"})
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    txt = p.read_text()
    assert PK in txt and ADDR in txt and "NO_PERMITIDA" not in txt
    p.write_text(txt + "OTRA=1\n")
    wizard.write_env({"POLYMARKET_PROXY_LIST": "http://u:p@h:80"})
    txt2 = p.read_text()
    assert PK in txt2 and "OTRA=1" in txt2 and "POLYMARKET_PROXY_LIST=http://u:p@h:80" in txt2
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    wizard.write_env({"POLY_PRIVATE_KEY": ""})  # vacío = conservar
    assert PK in p.read_text()


def test_secrets_never_reach_settings_file(isolated_env):
    wizard.write_env({"POLY_PRIVATE_KEY": PK})
    config.save_settings({"POLY_SIGNATURE_TYPE": "1"})
    assert PK not in config.settings_file().read_text()


def test_mask_address():
    assert wizard.mask_address(ADDR) == "0x1212…1212"
