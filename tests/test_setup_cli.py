"""Asistente de terminal (install.sh): guiado, menú y no interactivo, con entrada simulada."""
import os
import stat

import pytest

from caro5m import account, config, setup, wizard
from test_account import FakeClient, FakeCreds, PK, SAFE, SIGNER, FakeChain  # noqa: F401  (fixtures y dobles)
from test_account import fake_client, local_clob, poly, proxy  # noqa: F401

SECRETS = [PK, PK[2:], "AK-1234567890", "SECRET-abcdefghij", "PASS-klmnopqrst", "S3cretPw"]


class FakeIO(setup.IO):
    def __init__(self, answers=()):
        super().__init__(color=False)
        self.answers, self.out = list(answers), []

    def say(self, text=""):
        self.out.append(text)

    def _next(self):
        assert self.answers, "el asistente pidió más respuestas de las previstas"
        return self.answers.pop(0)

    def ask(self, prompt, default=""):
        return self._next() or default

    def secret(self, prompt):
        return self._next()

    def yes(self, prompt, default=False):
        v = str(self._next()).lower()
        return default if v == "" else v in ("s", "y")

    @property
    def text(self):
        return "\n".join(self.out)


def assert_no_secrets(io):
    for s in SECRETS:
        assert s not in io.text, f"secreto filtrado en la salida: {s[:6]}…"


@pytest.fixture(autouse=True)
def quiet_network(monkeypatch, fake_client):
    monkeypatch.setattr(account, "run_preflight", lambda **k: [account.Check("x", "ok", "Todo", "bien")])
    monkeypatch.setattr(account, "probe_proxy", lambda proxy=None, *a, **k: {"via": "proxy" if proxy else "directo", "ok": True, "blocked": False, "country": "NL", "region": "NH", "latency_ms": 80, "ip": "1.1.1.1", "clob_ok": True, "error": None})


def test_apply_wallet_eoa_sets_funder_to_signer_and_sig0(isolated_env):
    r = setup.apply_wallet("eoa", PK)
    assert r["funder"] == SIGNER
    env = account.read_env()
    assert env["POLY_PRIVATE_KEY"] == PK and env["POLY_SAFE_ADDRESS"] == SIGNER
    assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 0
    assert stat.S_IMODE(os.stat(config.env_file()).st_mode) == 0o600


def test_apply_wallet_safe_and_magic(isolated_env):
    setup.apply_wallet("safe", PK, SAFE)
    assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 2 and account.read_env()["POLY_SAFE_ADDRESS"] == SAFE
    setup.apply_wallet("magic", "", SAFE)  # cambia de tipo sin repetir la clave
    assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 1 and account.read_env()["POLY_PRIVATE_KEY"] == PK
    with pytest.raises(ValueError):
        setup.apply_wallet("raro", PK)
    with pytest.raises(ValueError):
        setup.apply_wallet("safe", PK, "0x123")
    with pytest.raises(ValueError):
        setup.apply_wallet("safe", "0xabc")


def test_dry_run_is_never_touched(isolated_env):
    setup.apply_wallet("safe", PK, SAFE)
    setup.apply_proxy_setting("socks5://u:p@h.example:1080", True)
    setup.do_generate_keys()
    s = config.load_settings()
    assert s["DRY_RUN"] is True
    assert "DRY_RUN" not in config.settings_file().read_text()


def test_proxy_add_edit_remove_and_scan_toggle(isolated_env):
    setup.apply_proxy_setting("http://u:S3cretPw@h.example:8080")
    assert account.read_env()["POLYMARKET_PROXY_LIST"].startswith("http://u:S3cretPw@")
    assert config.load_settings()["CARO5M_PROXY_SCAN"] is False
    setup.apply_proxy_setting("socks5h://h2.example:1080", True)
    assert account.read_env()["POLYMARKET_PROXY_LIST"] == "socks5h://h2.example:1080" and config.load_settings()["CARO5M_PROXY_SCAN"] is True
    with pytest.raises(ValueError):
        setup.apply_proxy_setting("ftp://x:1")
    assert "ftp" not in account.read_env()["POLYMARKET_PROXY_LIST"]
    setup.apply_proxy_setting("")
    assert "POLYMARKET_PROXY_LIST" not in account.read_env()
    assert setup.first_proxy() is None


def test_generate_and_regenerate_keys(isolated_env, fake_client):
    setup.apply_wallet("safe", PK, SAFE)
    r = setup.do_generate_keys()
    assert r["validation"]["ok"] and "SECRET" not in str(r) and "AK-1234567890" not in str(r)
    assert ("keygen", None) in fake_client.calls
    assert account.read_env()["POLY_CLOB_API_SECRET"] == "SECRET-abcdefghij"
    r2 = setup.do_generate_keys(regenerate=True)
    assert r2["nonce"] == 1 and ("keygen", 1) in fake_client.calls and account.read_env()["POLY_CLOB_NONCE"] == "1"
    setup.do_generate_keys(regenerate=True)
    assert ("keygen", 2) in fake_client.calls
    # el cliente se construyó con el tipo de firma y el funder de la cartera
    assert ("init", 2, SAFE) in fake_client.calls


def test_generate_requires_key(isolated_env):
    with pytest.raises(ValueError):
        setup.do_generate_keys()


def test_guided_flow_end_to_end_no_secret_in_output(isolated_env, fake_client):
    io = FakeIO([
        "1",           # «¿Cómo entras en polymarket.com?» -> cartera del navegador (Safe)
        PK,            # clave (secret)
        SAFE,          # dirección de la cuenta de Polymarket
        "a",           # proxy: añadir
        "http://u:S3cretPw@h.example:8080",
        "n",           # ¿proxy también para lecturas? no
        "s",           # generar claves
    ])
    setup.guided(io)
    assert account.read_env()["POLY_CLOB_API_KEY"] == "AK-1234567890"
    assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 2
    assert "h.example:8080" in io.text and "S3cretPw" not in io.text
    assert "DRY_RUN=1" in io.text  # termina recordando que sigue en PRUEBA
    assert_no_secrets(io)


def test_menu_check_and_exit(isolated_env):
    io = FakeIO(["4", "0"])
    setup.menu(io)
    assert "Todo" in io.text


def test_non_interactive_from_env(isolated_env, fake_client):
    io = FakeIO()
    env = {"CARO5M_SETUP_WALLET": "safe", "CARO5M_SETUP_PRIVATE_KEY": PK, "CARO5M_SETUP_FUNDER": SAFE,
           "CARO5M_SETUP_PROXY": "http://u:S3cretPw@h.example:8080", "CARO5M_SETUP_PROXY_SCAN": "1", "CARO5M_SETUP_GENERATE_KEYS": "1"}
    assert setup.non_interactive(io, env) == 0
    e = account.read_env()
    assert e["POLY_PRIVATE_KEY"] == PK and e["POLY_CLOB_API_KEY"] == "AK-1234567890" and "h.example" in e["POLYMARKET_PROXY_LIST"]
    assert config.load_settings()["CARO5M_PROXY_SCAN"] is True and config.load_settings()["DRY_RUN"] is True
    assert_no_secrets(io)


def test_non_interactive_key_from_file_and_errors(isolated_env, tmp_path):
    kf = tmp_path / "k"
    kf.write_text(PK + "\n")
    io = FakeIO()
    assert setup.non_interactive(io, {"CARO5M_SETUP_WALLET": "eoa", "CARO5M_SETUP_PRIVATE_KEY_FILE": str(kf), "CARO5M_SETUP_CHECKS": "0"}) == 0
    assert account.read_env()["POLY_SAFE_ADDRESS"] == SIGNER
    io = FakeIO()
    assert setup.non_interactive(io, {"CARO5M_SETUP_PRIVATE_KEY": "mala"}) == 1
    assert "mala" not in io.text.replace("mala ", "")
    io = FakeIO()
    assert setup.non_interactive(io, {}) == 0 and "no se cambió nada" in io.text


def test_non_interactive_can_never_approve(isolated_env, monkeypatch):
    monkeypatch.setattr(account, "approve_onchain", lambda *a, **k: pytest.fail("no debe aprobar"))
    setup.non_interactive(FakeIO(), {"CARO5M_SETUP_WALLET": "eoa", "CARO5M_SETUP_PRIVATE_KEY": PK, "CARO5M_SETUP_APPROVE": "1", "CARO5M_SETUP_CHECKS": "0"})


def test_main_check_exit_codes(isolated_env, monkeypatch):
    assert setup.main(["--check"], FakeIO()) == 0
    monkeypatch.setattr(account, "run_preflight", lambda **k: [account.Check("x", "fail", "Mal", "x")])
    assert setup.main(["--check"], FakeIO()) == 2


def test_main_non_interactive_flag(isolated_env, monkeypatch):
    monkeypatch.delenv("CARO5M_SETUP_WALLET", raising=False)
    assert setup.main(["--non-interactive"], FakeIO()) == 0


# ── aprobaciones desde el asistente: S/N explícito ──
def test_eoa_approval_declined_sends_nothing(isolated_env, monkeypatch):
    setup.apply_wallet("eoa", PK)
    chain = FakeChain()
    monkeypatch.setattr(account, "_rpc", chain)
    io = FakeIO(["n"])
    setup.step_approvals(io)
    assert chain.sent == [] and "No se ha enviado nada" in io.text


def test_eoa_approval_confirmed_sends(isolated_env, monkeypatch):
    setup.apply_wallet("eoa", PK)
    chain = FakeChain()
    monkeypatch.setattr(account, "_rpc", chain)
    io = FakeIO(["s"])
    setup.step_approvals(io)
    assert len(chain.sent) == 4 and "confirmada" in io.text
    assert_no_secrets(io)


def test_blank_answer_is_no(isolated_env, monkeypatch):
    setup.apply_wallet("eoa", PK)
    chain = FakeChain()
    monkeypatch.setattr(account, "_rpc", chain)
    setup.step_approvals(FakeIO([""]))
    assert chain.sent == []


@pytest.mark.parametrize("wallet", ["safe", "magic"])
def test_safe_and_magic_never_send_transactions(isolated_env, monkeypatch, wallet, fake_client):
    setup.apply_wallet(wallet, PK, SAFE)
    setup.do_generate_keys()
    monkeypatch.setattr(account, "approve_onchain", lambda *a, **k: pytest.fail("Safe/Magic no envían transacciones"))
    io = FakeIO(["s"])  # acepta solo la actualización de caché
    setup.step_approvals(io)
    assert ("refresh",) in fake_client.calls and "polymarket.com" in io.text


# ───────────── v1.2: pregunta de cartera en lenguaje llano, campo a campo ─────────────
PROFILE = "0x" + "77" * 20


def with_profile(monkeypatch, wallet=PROFILE, error=None):
    monkeypatch.setattr(account, "_lookup_fn", lambda signer, **k: {"proxy_wallet": wallet, "name": "demo", "error": error})


def test_wallet_question_lists_plain_spanish_options_and_steps(isolated_env):
    io = FakeIO(["1", PK, SAFE])
    setup.step_wallet(io)
    t = io.text
    assert "¿Cómo entras en polymarket.com?" in t
    assert "Con MetaMask / Rabby / Coinbase Wallet / otra cartera del navegador" in t
    assert "Con email o Google (Magic)" in t
    assert "Uso mi propia cartera sin la web de Polymarket (avanzado, EOA)" in t
    assert "Mostrar clave privada" in t and "Perfil" in t           # dónde sacar clave y dirección
    assert "Tu clave corresponde a la dirección" in t and wizard.mask_address(SIGNER) in t and PK not in t
    assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 2
    assert account.read_env()["POLY_SAFE_ADDRESS"] == SAFE


@pytest.mark.parametrize("pick,sig,fragment", [("1", 2, "reveal"), ("2", 1, "reveal.magic.link/polymarket"), ("3", 0, "Polygon")])
def test_each_answer_maps_to_signature_type(isolated_env, pick, sig, fragment):
    answers = [pick, PK] + ([SAFE] if pick != "3" else [])
    io = FakeIO(answers)
    setup.step_wallet(io)
    assert config.load_settings()["POLY_SIGNATURE_TYPE"] == sig
    if pick == "3":
        assert account.read_env()["POLY_SAFE_ADDRESS"] == SIGNER
    if pick == "2":
        assert fragment in io.text


def test_empty_key_does_not_discard_funder_bug1(isolated_env):
    """Una clave vacía en el paso 1 daba error y descartaba el funder escrito (funder FALTA)."""
    io = FakeIO(["1", "", SAFE])                 # sin clave, pero con funder
    setup.step_wallet(io)
    e = account.read_env()
    assert e["POLY_SAFE_ADDRESS"] == SAFE and "POLY_PRIVATE_KEY" not in e
    assert "clave: PENDIENTE" in io.text and "✖" not in io.text
    io = FakeIO(["1", PK, ""])                   # luego solo la clave: el funder guardado se conserva
    setup.step_wallet(io)
    e = account.read_env()
    assert e["POLY_PRIVATE_KEY"] == PK and e["POLY_SAFE_ADDRESS"] == SAFE


def test_invalid_fields_are_reasked_one_by_one(isolated_env):
    io = FakeIO(["1", "mala-clave", PK, "0x123", SAFE])
    setup.step_wallet(io)
    assert io.text.count("✖") == 2 and "Inténtalo otra vez" in io.text
    e = account.read_env()
    assert e["POLY_PRIVATE_KEY"] == PK and e["POLY_SAFE_ADDRESS"] == SAFE


def test_funder_equal_to_signer_asks_and_can_switch_to_eoa_bug2(isolated_env):
    """La pista decía «parece EOA» pero guardaba Safe sin preguntar."""
    io = FakeIO(["1", PK, SIGNER, "1"])          # escribe su propia dirección como funder -> pregunta -> opción 1 (EOA)
    setup.step_wallet(io)
    assert "MISMA que la de tu clave" in io.text and "Cambiado a: cartera propia sin la web (EOA, avanzado)" in io.text
    assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 0
    assert account.read_env()["POLY_SAFE_ADDRESS"] == SIGNER


def test_funder_equal_to_signer_can_be_kept_explicitly(isolated_env):
    io = FakeIO(["1", PK, SIGNER, "2"])
    setup.step_wallet(io)
    assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 2 and account.read_env()["POLY_SAFE_ADDRESS"] == SIGNER


def test_profile_lookup_suggests_the_real_account(isolated_env, monkeypatch):
    with_profile(monkeypatch)
    io = FakeIO(["1", PK, ""])                   # sin funder: lo busca Polymarket
    setup.step_wallet(io)
    assert "Polymarket indica que tu cuenta es" in io.text
    assert account.read_env()["POLY_SAFE_ADDRESS"] == PROFILE and config.load_settings()["POLY_SIGNATURE_TYPE"] == 2
    # funder = dirección de la cartera, pero Polymarket conoce la cuenta real -> primera opción «usar la de Polymarket»
    io = FakeIO(["1", "", SIGNER, "1"])
    setup.step_wallet(io)
    assert account.read_env()["POLY_SAFE_ADDRESS"] == PROFILE


def test_profile_lookup_failure_is_not_fatal(isolated_env, monkeypatch):
    with_profile(monkeypatch, wallet=None, error="La conexión tardó demasiado")
    io = FakeIO(["1", PK, SAFE])
    setup.step_wallet(io)
    assert "No pude consultar tu perfil público" in io.text and account.read_env()["POLY_SAFE_ADDRESS"] == SAFE


def test_eoa_choice_with_existing_profile_warns(isolated_env, monkeypatch):
    with_profile(monkeypatch)
    io = FakeIO(["3", PK, "1"])                  # EOA, pero Polymarket tiene cuenta para esa clave -> opción 1: cartera del navegador
    setup.step_wallet(io)
    assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 2 and account.read_env()["POLY_SAFE_ADDRESS"] == PROFILE


def test_proxy_advice_for_socks5(isolated_env):
    assert account.proxy_advice("socks5://u:p@h:1") == ""            # órdenes/claves (httpx): el proxy resuelve el DNS igualmente
    assert "socks5h://" in account.proxy_advice("socks5://u:p@h:1", scan=True)   # leer precios con «requests»: socks5:// = DNS local
    assert account.proxy_advice("socks5h://u:p@h:1") == "" and account.proxy_advice("http://h:1") == ""


def test_guided_flow_no_secret_even_with_profile(isolated_env, monkeypatch, fake_client):
    with_profile(monkeypatch)
    io = FakeIO(["2", PK, "", "", "s"])          # Magic, clave, funder vacío (buscar), proxy: Enter, generar claves
    setup.guided(io)
    assert_no_secrets(io)
    assert account.read_env()["POLY_CLOB_API_KEY"] == "AK-1234567890" and config.load_settings()["POLY_SIGNATURE_TYPE"] == 1
