"""Pantalla «Configuración de cuenta» (TUI headless). Todo con dobles: sin red, sin claves reales, sin transacciones."""
import asyncio
import html
import os
import re
import stat

import pytest
from textual.widgets import Checkbox, Input, RadioButton, Static

import demo_status
from caro5m import account, config, setup, status, wizard
from caro5m.tui.app import AccountScreen, Caro5mApp, ChoiceScreen, ConfirmScreen, ErrorsScreen, ProxyScreen, SettingsScreen
from test_account import FakeChain, FakeClient, FakeCreds, PK, SAFE, SIGNER, fake_client  # noqa: F401

SECRETS = [PK, PK[2:], "AK-1234567890", "SECRET-abcdefghij", "PASS-klmnopqrst", "S3cretPw"]


def run(coro):
    return asyncio.run(coro)


def txt(app):
    raw = app.export_screenshot()
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).replace("\xa0", " ")


def out_text(app):
    r = app.screen.query_one("#acc-out-text", Static).render()
    return r.plain if hasattr(r, "plain") else str(r)


async def with_app(body, size=(118, 110)):
    status.write_status(demo_status.build())
    app = Caro5mApp(first_run_wizard=False)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.3)
        await body(app, pilot)


async def settle(app, pilot):
    await app.workers.wait_for_complete()
    await pilot.pause(0.3)


def no_secrets(app):
    t = txt(app)
    for s in SECRETS:
        assert s not in t


PROFILE = "0x" + "77" * 20


async def pick(app, pilot, choice):
    app.screen.query_one(f"#acc-w-{choice}", RadioButton).value = True
    await pilot.pause(0.3)


async def put(app, pilot, wid, value):
    app.screen.query_one(wid, Input).value = value
    await pilot.pause(0.2)


def test_reachable_from_main_settings_and_w_alias():
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.2)
        assert isinstance(app.screen, AccountScreen)
        await pilot.press("escape")
        await pilot.press("c")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SettingsScreen)
        await pilot.press("a")
        await pilot.pause(0.2)
        assert isinstance(app.screen, AccountScreen)
        await pilot.press("escape")
        await pilot.press("escape")
        await pilot.press("w")  # «W» sigue funcionando (antiguo asistente): ahora es la misma pantalla
        await pilot.pause(0.2)
        assert isinstance(app.screen, AccountScreen)

    run(with_app(body))


def test_question_and_three_plain_options_and_steps_are_shown(isolated_env):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.4)
        t = txt(app)
        assert "¿Cómo entras en polymarket.com?" in t
        assert "Con MetaMask / Rabby / Coinbase Wallet / otra cartera del navegador" in t
        assert "Con email o Google (Magic)" in t and "Uso mi propia cartera sin la web de Polymarket" in t
        assert "Mostrar clave privada" in str(app.screen.query_one("#acc-wallet-help").render())
        assert "DIRECCIÓN DE TU CUENTA" in str(app.screen.query_one("#acc-funder-help").render())
        await pick(app, pilot, "email")
        assert "reveal.magic.link/polymarket" in str(app.screen.query_one("#acc-wallet-help").render())
        await pick(app, pilot, "own")
        assert not app.screen.query_one("#acc-funder", Input).display      # EOA: no hace falta dirección de fondos

    run(with_app(body))


def test_save_wallet_each_choice_and_key_is_cleared(isolated_env):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        s = app.screen
        assert s.query_one("#acc-key", Input).password is True
        await pick(app, pilot, "own")
        await put(app, pilot, "#acc-key", PK)
        await pilot.click("#acc-save-wallet")
        await settle(app, pilot)
        assert s.query_one("#acc-key", Input).value == ""  # la clave no queda en el campo
        assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 0
        assert account.read_env()["POLY_SAFE_ADDRESS"] == SIGNER
        assert stat.S_IMODE(os.stat(config.env_file()).st_mode) == 0o600
        await pick(app, pilot, "browser")
        await put(app, pilot, "#acc-funder", SAFE)
        await pilot.click("#acc-save-wallet")
        await settle(app, pilot)
        assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 2 and account.read_env()["POLY_SAFE_ADDRESS"] == SAFE
        await pick(app, pilot, "email")
        await pilot.click("#acc-save-wallet")
        await settle(app, pilot)
        assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 1
        assert config.load_settings()["DRY_RUN"] is True
        no_secrets(app)

    run(with_app(body))


def test_empty_key_keeps_funder_and_error_is_per_field_bug1(isolated_env):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        s = app.screen
        await put(app, pilot, "#acc-funder", SAFE)                 # sin clave: ni error ni descarte
        await pilot.click("#acc-save-wallet")
        await settle(app, pilot)
        assert account.read_env()["POLY_SAFE_ADDRESS"] == SAFE and "POLY_PRIVATE_KEY" not in account.read_env()
        assert not s.query_one("#acc-key-err").display and "PENDIENTE" in out_text(app)
        # clave inválida + funder válido: se guarda lo bueno y se marca solo la clave
        await put(app, pilot, "#acc-funder", "0x" + "34" * 20)
        await put(app, pilot, "#acc-key", "no-es-clave")
        await pilot.click("#acc-save-wallet")
        await settle(app, pilot)
        assert account.read_env()["POLY_SAFE_ADDRESS"] == "0x" + "34" * 20
        assert s.query_one("#acc-key-err").display and "clave privada" in str(s.query_one("#acc-key-err").render()).lower()
        assert s.query_one("#acc-key", Input).value == "no-es-clave"      # el campo erróneo se queda para corregirlo
        # dirección inválida + clave válida
        await put(app, pilot, "#acc-key", PK)
        await put(app, pilot, "#acc-funder", "0x123")
        await pilot.click("#acc-save-wallet")
        await settle(app, pilot)
        assert account.read_env()["POLY_PRIVATE_KEY"] == PK and s.query_one("#acc-funder-err").display
        assert s.query_one("#acc-funder", Input).value == "0x123" and not s.query_one("#acc-key-err").display
        no_secrets(app)

    run(with_app(body))


def test_funder_equal_signer_asks_and_user_decides_bug2(isolated_env):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        s = app.screen
        await put(app, pilot, "#acc-key", PK)
        await put(app, pilot, "#acc-funder", SIGNER)
        assert "no la de tu cuenta" in str(s.query_one("#acc-hint").render())
        await pilot.click("#acc-save-wallet")
        await settle(app, pilot)
        assert isinstance(app.screen, ChoiceScreen)                         # pregunta: no guarda nada solo
        assert not config.env_file().exists()
        await pilot.press("escape")                                          # cancelar = no se guarda nada y los campos siguen
        await settle(app, pilot)
        assert not config.env_file().exists() and s.query_one("#acc-key", Input).value == PK
        await pilot.click("#acc-save-wallet")
        await settle(app, pilot)
        await pilot.press("1")                                               # 1 = «Es mi cartera propia sin la web (EOA)»
        await settle(app, pilot)
        assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 0 and account.read_env()["POLY_SAFE_ADDRESS"] == SIGNER
        assert s._choice() == "own"
        no_secrets(app)

    run(with_app(body))


def test_profile_lookup_button_and_save_uses_polymarket_account(isolated_env, monkeypatch):
    monkeypatch.setattr(account, "_lookup_fn", lambda signer, **k: {"proxy_wallet": PROFILE, "name": "demo", "error": None})

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        s = app.screen
        await pilot.click("#acc-lookup")
        await settle(app, pilot)
        assert "Primero pega tu clave" in out_text(app)
        await put(app, pilot, "#acc-key", PK)
        await pilot.click("#acc-lookup")
        await settle(app, pilot)
        assert s.query_one("#acc-funder", Input).value == PROFILE and "Polymarket indica" in out_text(app)
        await pilot.click("#acc-save-wallet")
        await settle(app, pilot)
        assert account.read_env()["POLY_SAFE_ADDRESS"] == PROFILE and config.load_settings()["POLY_SIGNATURE_TYPE"] == 2

    run(with_app(body))


def test_profile_lookup_not_found_and_error(isolated_env, monkeypatch):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await put(app, pilot, "#acc-key", PK)
        await pilot.click("#acc-lookup")
        await settle(app, pilot)
        assert "No encontré" in out_text(app)
        monkeypatch.setattr(account, "_lookup_fn", lambda signer, **k: {"proxy_wallet": None, "name": None, "error": "La conexión tardó demasiado"})
        await pilot.click("#acc-lookup")
        await settle(app, pilot)
        assert "No pude consultarlo" in out_text(app)

    run(with_app(body))


def test_save_and_continue_generates_keys_and_runs_checks(isolated_env, fake_client, monkeypatch):
    monkeypatch.setattr(account, "run_preflight", lambda **k: [account.Check("x", "ok", "Todo", "bien")])

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await put(app, pilot, "#acc-key", PK)
        await put(app, pilot, "#acc-funder", SAFE)
        await pilot.click("#acc-save-go")
        await settle(app, pilot)
        await settle(app, pilot)
        o = out_text(app)
        assert "Cartera guardada" in o and "Claves CLOB generadas" in o and "Todo" in o
        assert account.read_env()["POLY_CLOB_API_KEY"] == "AK-1234567890" and ("init", 2, SAFE) in fake_client.calls
        assert config.load_settings()["DRY_RUN"] is True
        no_secrets(app)

    run(with_app(body))


def test_save_and_continue_stops_when_something_is_invalid(isolated_env, fake_client):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await put(app, pilot, "#acc-key", PK)
        await put(app, pilot, "#acc-funder", "0x123")
        await pilot.click("#acc-save-go")
        await settle(app, pilot)
        assert not any(c[0] == "keygen" for c in fake_client.calls if isinstance(c, tuple))
        assert "corrige" in out_text(app)

    run(with_app(body))


def test_first_run_opens_guided_account_when_env_incomplete(isolated_env):
    async def go(check):
        status.write_status(demo_status.build())
        app = Caro5mApp(first_run_wizard=True)
        async with app.run_test(size=(118, 70)) as pilot:
            await pilot.pause(0.5)
            await check(app, pilot)

    async def first(app, pilot):
        assert isinstance(app.screen, AccountScreen) and app.screen.guided
        assert "¡Bienvenido!" in out_text(app) and "Siguiente paso" in txt(app)
        await pilot.press("escape")                      # saltar: no vuelve a abrirse solo
        await pilot.pause(0.3)
        assert not isinstance(app.screen, AccountScreen) and account.setup_skipped()

    async def second(app, pilot):
        assert not isinstance(app.screen, AccountScreen)

    run(go(first))
    run(go(second))


def test_first_run_not_opened_when_setup_complete_or_disabled(isolated_env):
    wizard.write_env({"POLY_PRIVATE_KEY": PK, "POLY_SAFE_ADDRESS": SAFE})

    async def go():
        status.write_status(demo_status.build())
        app = Caro5mApp(first_run_wizard=True)
        async with app.run_test(size=(118, 70)) as pilot:
            await pilot.pause(0.5)
            assert not isinstance(app.screen, AccountScreen)

    run(go())


def test_proxy_save_mask_clear_and_invalid(isolated_env):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        s = app.screen
        await pilot.click("#acc-proxy-open")
        await pilot.pause(0.3)
        assert isinstance(app.screen, ProxyScreen)
        p = app.screen
        assert p.query_one("#proxy-input", Input).password is True
        p.query_one("#proxy-input", Input).value = "ftp://x:1"
        await pilot.pause(0.2)
        await pilot.click("#proxy-save")
        await pilot.pause(0.3)
        assert isinstance(app.screen, ProxyScreen) and "admitido" in txt(app) and "POLYMARKET_PROXY_LIST" not in account.read_env()
        p.query_one("#proxy-input", Input).value = "socks5://u:S3cretPw@h.example:1080"
        await pilot.pause(0.2)
        p.query_one("#proxy-scan", Checkbox).value = True
        await pilot.pause(0.2)
        await pilot.click("#proxy-save")
        await pilot.pause(0.3)
        assert isinstance(app.screen, AccountScreen)
        assert account.read_env()["POLYMARKET_PROXY_LIST"] == "socks5://u:S3cretPw@h.example:1080"
        assert config.load_settings()["CARO5M_PROXY_SCAN"] is True
        assert "••••:••••@h.example:1080" in txt(app) and "S3cretPw" not in txt(app)
        assert "socks5h://" in out_text(app)         # con «leer precios por el proxy» socks5:// resuelve el DNS en local para esas lecturas
        await pilot.click("#acc-proxy-clear")
        await pilot.pause(0.3)
        assert "POLYMARKET_PROXY_LIST" not in account.read_env()

    run(with_app(body))


def test_proxy_test_button(isolated_env, monkeypatch):
    wizard.write_env({"POLYMARKET_PROXY_LIST": "http://u:p@h.example:8080"})
    calls = []

    def fake_probe(proxy=None, *a, **k):
        calls.append(proxy)
        if proxy:
            return {"via": "proxy", "ok": True, "blocked": False, "country": "NL", "region": "NH", "latency_ms": 90, "ip": "x", "clob_ok": True, "error": None}
        return {"via": "directo", "ok": True, "blocked": True, "country": "DE", "region": "BY", "latency_ms": 40, "ip": "x", "clob_ok": True, "error": None}

    monkeypatch.setattr(account, "probe_proxy", fake_probe)

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await pilot.click("#acc-proxy-test")
        await settle(app, pilot)
        o = out_text(app)
        assert calls == [None, "http://u:p@h.example:8080"]
        assert "BLOQUEA" in o and "permite operar" in o and "NL" in o
        assert "u:p@" not in o

    run(with_app(body))


def test_generate_validate_and_regenerate_keys(isolated_env, fake_client):
    setup.apply_wallet("safe", PK, SAFE)

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await pilot.click("#acc-keys-gen")
        await settle(app, pilot)
        assert account.read_env()["POLY_CLOB_API_SECRET"] == "SECRET-abcdefghij"
        assert ("keygen", None) in fake_client.calls and "Claves CLOB guardadas" in out_text(app)
        no_secrets(app)
        await pilot.click("#acc-keys-validate")
        await settle(app, pilot)
        assert "válidas" in out_text(app) and "25" not in out_text(app) and "12,34" in out_text(app).replace(".", ",") or "12.34" in out_text(app)
        # regenerar: N no hace nada, S crea con nonce 1
        n_before = fake_client.calls.count(("keygen", 1))
        await pilot.click("#acc-keys-regen")
        await pilot.pause(0.3)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await settle(app, pilot)
        assert fake_client.calls.count(("keygen", 1)) == n_before
        await pilot.click("#acc-keys-regen")
        await pilot.pause(0.3)
        await pilot.press("s")
        await settle(app, pilot)
        assert ("keygen", 1) in fake_client.calls and account.read_env()["POLY_CLOB_NONCE"] == "1"
        no_secrets(app)

    run(with_app(body))


def test_generate_without_key_and_validate_without_creds(isolated_env):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await pilot.click("#acc-keys-gen")
        await settle(app, pilot)
        assert "Falta la clave privada" in out_text(app)
        await pilot.click("#acc-keys-validate")
        await settle(app, pilot)
        assert "No hay claves CLOB" in out_text(app)

    run(with_app(body))


def test_checks_button_renders_results(isolated_env, monkeypatch):
    monkeypatch.setattr(account, "run_preflight", lambda **k: [
        account.Check("clock", "ok", "Reloj del sistema", "desfase +0.1 s"),
        account.Check("geo", "fail", "Acceso a Polymarket", "bloqueado", "Configura un proxy")])

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await pilot.click("#acc-checks")
        await settle(app, pilot)
        o = out_text(app)
        assert "Reloj del sistema" in o and "Configura un proxy" in o and "impiden operar" in o

    run(with_app(body))


def test_eoa_approval_needs_confirmation(isolated_env, monkeypatch):
    setup.apply_wallet("eoa", PK)
    chain = FakeChain()
    monkeypatch.setattr(account, "_rpc", chain)

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await pilot.click("#acc-approve")
        await settle(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)  # diálogo S/N
        assert chain.sent == []
        await pilot.press("n")
        await settle(app, pilot)
        assert chain.sent == [] and "No se ha enviado nada" in out_text(app)
        await pilot.click("#acc-approve")
        await settle(app, pilot)
        await pilot.press("escape")  # Esc también es «No»
        await settle(app, pilot)
        assert chain.sent == []
        await pilot.click("#acc-approve")
        await settle(app, pilot)
        await pilot.press("s")
        await settle(app, pilot)
        assert len(chain.sent) == 4 and "confirmada" in out_text(app)
        no_secrets(app)

    run(with_app(body))


def test_safe_approval_never_sends_transactions(isolated_env, monkeypatch, fake_client):
    setup.apply_wallet("safe", PK, SAFE)
    setup.do_generate_keys()
    monkeypatch.setattr(account, "approve_onchain", lambda *a, **k: pytest.fail("Safe no envía transacciones"))

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await pilot.click("#acc-approve")
        await settle(app, pilot)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("s")
        await settle(app, pilot)
        assert ("refresh",) in fake_client.calls and "polymarket.com" in out_text(app)

    run(with_app(body))


def test_screen_shows_only_masked_secrets(isolated_env):
    wizard.write_env({"POLY_PRIVATE_KEY": PK, "POLY_SAFE_ADDRESS": SAFE, "POLYMARKET_PROXY_LIST": "http://u:S3cretPw@h.example:8080",
                      "POLY_CLOB_API_KEY": "AK-1234567890", "POLY_CLOB_API_SECRET": "SECRET-abcdefghij", "POLY_CLOB_API_PASSPHRASE": "PASS-klmnopqrst"})

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.4)
        t = txt(app)
        assert "AK-1" in t and "••••:••••@h.example:8080" in t
        no_secrets(app)

    run(with_app(body))
