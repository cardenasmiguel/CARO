import importlib
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

ENV_KEYS = [k for k in os.environ if k.startswith(("CARO5M_", "POLY", "PM_PTB", "GROK_", "BET_", "TP_", "SL_", "CB_", "MIN_TOKEN", "MAX_TOKEN", "DRY_RUN"))]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CARO5M_HOME", str(tmp_path))
    monkeypatch.setenv("CARO5M_SHM_DIR", str(tmp_path / "shm"))
    return tmp_path


@pytest.fixture
def bot(isolated_env):
    """Módulo del bot recargado con el entorno aislado de la prueba (nada de red ni de rutas reales)."""
    import caro5m.bot as b

    b = importlib.reload(b)
    (isolated_env / "shm").mkdir(exist_ok=True)
    b.WARNINGS.clear()
    return b


@pytest.fixture(autouse=True)
def no_auto_ptb(monkeypatch):
    """Las pruebas del panel nunca lanzan el PTB real (red): AUTO_START_PTB=0 salvo que la prueba lo active."""
    monkeypatch.setenv("AUTO_START_PTB", "0")


@pytest.fixture(autouse=True)
def no_profile_lookup(monkeypatch):
    """Ninguna prueba consulta el perfil público real de Polymarket (los que lo necesitan lo sustituyen)."""
    from caro5m import account

    monkeypatch.setattr(account, "_lookup_fn", lambda signer, **k: {"proxy_wallet": None, "name": None, "error": None})


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):
    """La puerta de red (network_gate) y check_dns no dependen del DNS real del equipo que ejecuta las pruebas."""
    from caro5m import account

    monkeypatch.setattr(account, "_resolve", lambda host, port=None, *a, **k: [(2, 1, 6, "", ("127.0.0.1", port or 443))])


@pytest.fixture(autouse=True)
def no_retry_sleep(monkeypatch):
    """Los reintentos por fallos pasajeros del proxy no esperan en las pruebas."""
    from caro5m import account

    monkeypatch.setattr(account, "_retry_sleep", lambda s: None)
