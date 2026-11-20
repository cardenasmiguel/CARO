"""Configuración de cuenta: claves CLOB, proxy, comprobaciones y aprobaciones. TODO con servidores locales/mocks:
ni red real, ni claves reales, ni transacciones reales."""
import os
import stat
import time

import pytest

from caro5m import account, config, wizard
from netfakes import CountingProxy, FakePolymarket

PK = "0x" + "ab" * 32
SIGNER = wizard.signer_address(PK)
SAFE = "0x" + "12" * 20


@pytest.fixture
def poly():
    p = FakePolymarket()
    yield p
    p.close()


@pytest.fixture
def proxy():
    p = CountingProxy()
    yield p
    p.close()


@pytest.fixture(autouse=True)
def local_clob(monkeypatch, poly):
    """Los ajustes apuntan al CLOB local de mentira (nunca a Polymarket real)."""
    monkeypatch.setenv("CLOB_HOST", poly.url)
    monkeypatch.setattr(account, "GEOBLOCK_URL", poly.url + "/api/geoblock")
    monkeypatch.setattr(account.probe_proxy, "__defaults__", (None, poly.url + "/api/geoblock", 12.0))


# ── proxy: formato y enmascarado ──
def test_parse_proxy_formats():
    assert account.parse_proxy("1.2.3.4:8080") == "http://1.2.3.4:8080"
    for ok in ("http://u:p@h.example:8080", "https://h.example:443", "socks5://u:p@h.example:1080", "socks5h://h.example:1080"):
        assert account.parse_proxy(ok) == ok
    for bad in ("", "ftp://h:21", "http://h", "http://:80", "socks5://h:99999", "http://h:abc"):
        with pytest.raises(ValueError):
            account.parse_proxy(bad)
    assert wizard.validate_proxy("socks5://u:p@h.example:1080, http://h2.example:80")
    with pytest.raises(ValueError):
        wizard.validate_proxy("ftp://x:1")


def test_mask_proxy_hides_password():
    m = account.mask_proxy("http://user:S3cret@host.example:8080, socks5://u2:pw@h2.example:1080")
    assert "S3cret" not in m and "pw@" not in m and "http://••••:••••@host.example:8080" in m and "socks5://••••:••••@h2.example:1080" in m
    assert account.mask_proxy("") == "—"


def test_socks_client_can_be_built():
    c = account.build_httpx_client("socks5://u:p@127.0.0.1:1080")  # requiere httpx[socks]
    c.close()


def test_mask_secret():
    assert "abcdefghij" not in account.mask_secret("ak-created-1234567890")
    assert account.mask_secret("") == "—" and account.mask_secret("abc") == "••••"


# ── proxy: prueba real contra servidores locales ──
def test_probe_direct_vs_proxy_counts_traffic(poly, proxy):
    d = account.probe_proxy(None, poly.url)
    assert d["via"] == "directo" and d["ok"] and d["blocked"] is False and proxy.count == 0 and d["country"] == "ES"
    p = account.probe_proxy(proxy.url, poly.url)
    assert p["via"] == "proxy" and p["ok"] and p["latency_ms"] is not None
    assert proxy.count >= 3  # geoblock + 2× /time pasaron por el proxy
    lvl, txt = account.describe_probe(p)
    assert lvl == "ok" and "permite operar" in txt


def test_probe_reports_geoblock(poly, proxy):
    poly.blocked, poly.country = True, "DE"
    r = account.probe_proxy(None, poly.url)
    lvl, txt = account.describe_probe(r)
    assert lvl == "fail" and "BLOQUEA" in txt and "DE" in txt


def test_probe_dead_proxy_is_clear_error(poly):
    r = account.probe_proxy("http://127.0.0.1:9", poly.url, timeout=2)
    assert not r["ok"] and r["error"]
    assert account.describe_probe(r)[0] == "fail"


# ── claves CLOB con la librería REAL contra el CLOB local, por el proxy ──
def test_generate_credentials_real_client_via_proxy(poly, proxy):
    creds = account.generate_credentials(PK, 2, SAFE, proxy=proxy.url, host=poly.url, chain_id=137)
    assert creds.api_key == "ak-created-1234567890"
    posts = [h for h in poly.hits if h[0] == "POST" and h[1] == "/auth/api-key"]
    assert posts and posts[0][2].get("poly_address", "").lower() == SIGNER.lower()  # firma L1 de la clave
    assert any("/auth/api-key" in p for p in proxy.paths)  # la llamada de autenticación salió por el proxy


def test_generate_credentials_falls_back_to_derive_and_nonce(poly):
    poly.fail_create = True
    creds = account.generate_credentials(PK, 2, SAFE, host=poly.url, chain_id=137)
    assert creds.api_key == "ak-derived-1234567890"
    n = [h[2].get("poly_nonce") for h in poly.hits if h[1] == "/auth/derive-api-key"]
    assert n and n[-1] == "0"
    account.generate_credentials(PK, 2, SAFE, nonce=3, host=poly.url, chain_id=137)
    assert [h[2].get("poly_nonce") for h in poly.hits if h[1] == "/auth/derive-api-key"][-1] == "3"


def test_generate_without_key_or_bad_key():
    with pytest.raises(ValueError):
        account.generate_credentials("", 2, SAFE)
    with pytest.raises(ValueError):
        account.generate_credentials("0x1234", 2, SAFE)


def test_clob_via_proxy_restores_client(proxy):
    from py_clob_client_v2.http_helpers import helpers

    before = helpers._http_client
    with account.clob_via_proxy(proxy.url):
        assert helpers._http_client is not before
    assert helpers._http_client is before


class FakeCreds:
    api_key, api_secret, api_passphrase = "AK-1234567890", "SECRET-abcdefghij", "PASS-klmnopqrst"


class FakeClient:
    calls = []

    def __init__(self, private_key, sig, funder, host, chain_id, creds=None):
        self.sig, self.funder, self.creds = sig, funder, creds
        FakeClient.calls.append(("init", sig, funder))

    def create_or_derive_api_key(self, nonce=None):
        FakeClient.calls.append(("keygen", nonce))
        return FakeCreds()

    def get_api_keys(self):
        FakeClient.calls.append(("get_api_keys",))
        return {"apiKeys": [{"apiKey": "x"}]}

    def get_balance_allowance(self, params):
        FakeClient.calls.append(("balance", params.signature_type))
        return {"balance": "12340000", "allowances": {"0xa": "0", "0xb": "115792089237316195423570985008687907853269984665640564039457584007913129639935"}}

    def update_balance_allowance(self, params):
        FakeClient.calls.append(("refresh",))
        return {}


@pytest.fixture
def fake_client(monkeypatch):
    FakeClient.calls = []
    monkeypatch.setattr(account, "_make_client_fn", lambda pk, sig, funder, host, chain, creds=None: FakeClient(pk, sig, funder, host, chain, creds))
    return FakeClient


def test_validate_credentials_authenticated_read(fake_client):
    v = account.validate_credentials(PK, 2, SAFE, FakeCreds())
    assert v == {"ok": True, "keys": 1, "balance_usdc": 12.34, "allowance_ok": True, "error": None, "need_proxy": False}
    assert ("get_api_keys",) in fake_client.calls and ("balance", 2) in fake_client.calls


def test_validate_credentials_error_has_no_secrets(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(f"401 unauthorized for {PK} {SAFE}")

    monkeypatch.setattr(account, "_make_client_fn", boom)
    v = account.validate_credentials(PK, 2, SAFE, FakeCreds())
    assert not v["ok"] and PK not in v["error"] and SAFE not in v["error"]


def test_save_credentials_0600_and_nonce(isolated_env):
    account.save_credentials(FakeCreds(), 2)
    p = config.env_file()
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    env = account.read_env()
    assert env["POLY_CLOB_API_KEY"] == "AK-1234567890" and env["POLY_CLOB_NONCE"] == "2"
    assert account.next_nonce(env) == 3 and account.next_nonce({}) == 1
    c = account.stored_credentials(env)
    assert c.api_key == "AK-1234567890" and account.stored_credentials({}) is None


def test_remove_env_keeps_the_rest(isolated_env):
    wizard.write_env({"POLY_PRIVATE_KEY": PK, "POLYMARKET_PROXY_LIST": "http://u:p@h.example:80"})
    wizard.remove_env(["POLYMARKET_PROXY_LIST", "NO_PERMITIDA"])
    t = config.env_file().read_text()
    assert PK in t and "PROXY" not in t and stat.S_IMODE(os.stat(config.env_file()).st_mode) == 0o600


# ── tipo de cartera ──
def test_wallet_types_and_detection():
    assert {k: v["sig"] for k, v in account.WALLET_TYPES.items()} == {"eoa": 0, "magic": 1, "safe": 2}
    assert account.wallet_key(0) == "eoa" and account.wallet_key(1) == "magic" and account.wallet_key(2) == "safe"
    assert account.detect_wallet(PK, "")[0] == "eoa"
    assert account.detect_wallet(PK, SIGNER)[0] == "eoa"
    assert account.detect_wallet(PK, SAFE)[0] == "safe"


# ── comprobaciones ──
class Resp:
    def __init__(self, status=200, text="", js=None):
        self.status_code, self.text, self._js = status, text, js

    def json(self):
        return self._js

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, **kw):
        for k, v in self.routes.items():
            if url.endswith(k):
                if isinstance(v, Exception):
                    raise v
                return v() if callable(v) else v
        raise RuntimeError("ruta no simulada " + url)

    def post(self, url, **kw):
        return self.get(url, **kw)

    def close(self):
        pass


def use_http(monkeypatch, routes):
    monkeypatch.setattr(account, "_client_factory", lambda proxy=None, timeout=12.0, http2=True: FakeHttp(routes))


def test_check_clock_levels(monkeypatch):
    for skew, level in ((0.3, "ok"), (3.5, "warn"), (-90, "fail")):
        use_http(monkeypatch, {"/time": lambda s=skew: Resp(200, str(time.time() - s))})
        c = account.check_clock("https://clob.example", None)
        assert c.level == level, (skew, c)
    use_http(monkeypatch, {"/time": RuntimeError("red caída")})
    assert account.check_clock("https://clob.example", None).level == "warn"


def test_check_binance_451_hint(monkeypatch):
    use_http(monkeypatch, {"/api/v3/ping": Resp(451)})
    c = account.check_binance("https://api.binance.com")
    assert c.level == "fail" and "binance.us" in c.fix
    use_http(monkeypatch, {"/api/v3/ping": Resp(200, "{}")})
    assert account.check_binance("https://api.binance.com").level == "ok"


def test_check_rpcs(monkeypatch):
    def fake(rpc, method, params, timeout=8.0):
        if "bad" in rpc:
            raise RuntimeError("down")
        return "0x10"

    monkeypatch.setattr(account, "_rpc", fake)
    assert account.check_rpcs(["https://good.example", "https://good2.example"]).level == "ok"
    assert account.check_rpcs(["https://good.example", "https://bad.example"]).level == "warn"
    assert account.check_rpcs(["https://bad.example"]).level == "fail"


def test_check_gas_for_eoa(monkeypatch):
    monkeypatch.setattr(account, "_rpc", lambda *a, **k: hex(int(0.2e18)))
    assert account.check_gas(SIGNER, ["https://r.example"]).level == "ok"
    monkeypatch.setattr(account, "_rpc", lambda *a, **k: hex(int(0.001e18)))
    assert account.check_gas(SIGNER, ["https://r.example"]).level == "warn"


def test_check_wallet_funds_min_balance_vs_bet(monkeypatch):
    from caro5m import checks

    def mk(bal, ok):
        return lambda *a, **k: {"configured": True, "balance_usdc": bal, "allowance_ok": ok, "allowance_usdc": 5.0 if ok else 0.0, "error": None}

    monkeypatch.setattr(checks, "check_wallet", mk(1.02, True))  # < 1.00 × 1.03
    cs = account.check_wallet_funds(SAFE, ["r"], 137, 1.0, "safe")
    assert [c.level for c in cs] == ["fail", "ok"]
    monkeypatch.setattr(checks, "check_wallet", mk(1.03, False))
    cs = account.check_wallet_funds(SAFE, ["r"], 137, 1.0, "safe")
    assert cs[0].level == "ok" and cs[1].level == "fail" and "polymarket.com" in cs[1].fix
    cs = account.check_wallet_funds(SIGNER, ["r"], 137, 1.0, "eoa")
    assert "Aprobar allowances" in cs[1].fix


def test_check_local_shm_not_writable(isolated_env, monkeypatch):
    f = isolated_env / "ocupado"
    f.write_text("x")
    monkeypatch.setenv("CARO5M_SHM_DIR", str(f / "dentro"))
    cs = {c.id: c for c in account.check_local()}
    assert cs["shm"].level == "fail" and cs["data"].level == "ok"


def test_env_permissions_check(isolated_env):
    wizard.write_env({"POLY_PRIVATE_KEY": PK})
    assert {c.id: c for c in account.check_local()}["envperm"].level == "ok"
    os.chmod(config.env_file(), 0o644)
    assert {c.id: c for c in account.check_local()}["envperm"].level == "warn"


def test_run_preflight_offline_and_ready_logic(isolated_env):
    cs = account.run_preflight(with_network=False)
    ids = {c.id for c in cs}
    assert {"shm", "data", "wallet"} <= ids and "clock" not in ids
    assert account.ready_to_trade([account.Check("a", "ok", "x"), account.Check("b", "warn", "y")])
    assert not account.ready_to_trade([account.Check("a", "fail", "x")])
    assert account.summarize([account.Check("a", "ok", "x"), account.Check("b", "fail", "y"), account.Check("c", "warn", "z")]) == (1, 1, 1)


def test_run_preflight_full_with_geoblock_failure(isolated_env, monkeypatch, poly):
    from caro5m import checks

    poly.blocked, poly.country = True, "DE"
    wizard.write_env({"POLY_PRIVATE_KEY": PK, "POLY_SAFE_ADDRESS": SAFE})
    monkeypatch.setattr(account, "check_rpcs", lambda r: account.Check("rpc", "ok", "rpc"))
    monkeypatch.setattr(account, "check_binance", lambda h: account.Check("binance", "ok", "b"))
    monkeypatch.setattr(checks, "check_wallet", lambda *a, **k: {"configured": True, "balance_usdc": 9.0, "allowance_ok": True, "allowance_usdc": 9.0, "error": None})
    cs = account.run_preflight()
    by = {c.id: c for c in cs}
    assert by["geo_direct"].level == "fail" and "condiciones de uso" in by["geo_direct"].fix.lower() and "proxy" not in by["geo_direct"].fix.lower()
    assert by["clock"].level == "ok" and by["funds"].level == "ok" and by["creds"].level == "warn"
    assert not account.ready_to_trade(cs)


# ── aprobaciones: SIEMPRE con confirmación; solo mocks ──
class FakeChain:
    def __init__(self, approved=False, pol=1.0):
        self.calls, self.approved, self.pol, self.sent = [], approved, pol, []

    def __call__(self, rpc, method, params, timeout=8.0):
        self.calls.append(method)
        if method == "eth_call":
            data = params[0]["data"]
            if data.startswith("0xdd62ed3e"):
                return hex(10 ** 30 if self.approved else 0)
            return hex(1 if self.approved else 0)
        if method == "eth_gasPrice":
            return hex(50 * 10 ** 9)
        if method == "eth_estimateGas":
            return hex(60000)
        if method == "eth_getBalance":
            return hex(int(self.pol * 1e18))
        if method == "eth_getTransactionCount":
            return hex(7)
        if method == "eth_sendRawTransaction":
            self.sent.append(params[0])
            return "0x" + f"{len(self.sent):064x}"
        if method == "eth_getTransactionReceipt":
            return {"status": "0x1"}
        raise AssertionError(method)


def test_approve_requires_explicit_confirmation(monkeypatch):
    chain = FakeChain()
    monkeypatch.setattr(account, "_rpc", chain)
    for bad in (False, None, "s", 1):
        with pytest.raises(PermissionError):
            account.approve_onchain(PK, ["r"], 137, confirmed=bad)
    with pytest.raises(PermissionError):
        account.approve_onchain(PK, ["r"], 137)
    assert chain.calls == [] and chain.sent == []  # ni siquiera se consultó la red


def test_approve_sends_only_missing_signed_txs(monkeypatch):
    from eth_account import Account

    chain = FakeChain()
    monkeypatch.setattr(account, "_rpc", chain)
    res = account.approve_onchain(PK, ["r"], 137, confirmed=True)
    assert len(res) == 4 and all(r["status"] == "confirmada" for r in res) and len(chain.sent) == 4
    t = account.approval_targets(137)
    for raw in chain.sent:
        tx = Account.recover_transaction(raw)
        assert tx.lower() == SIGNER.lower()  # firmada por la clave del usuario
    selectors = {account.build_approval_tx(i)[:10] for i in account.approval_status(SIGNER, ["r"], 137)}
    assert selectors == {account.APPROVE_SELECTOR, account.SET_APPROVAL_FOR_ALL}
    # ya aprobado -> no envía nada
    chain2 = FakeChain(approved=True)
    monkeypatch.setattr(account, "_rpc", chain2)
    assert account.approve_onchain(PK, ["r"], 137, confirmed=True) == [] and chain2.sent == []


def test_approve_without_pol_aborts_before_sending(monkeypatch):
    chain = FakeChain(pol=0.0)
    monkeypatch.setattr(account, "_rpc", chain)
    with pytest.raises(RuntimeError, match="POL insuficiente"):
        account.approve_onchain(PK, ["r"], 137, confirmed=True)
    assert chain.sent == []


def test_approval_calldata_is_correct():
    sp = account.approval_targets(137)["spenders"][0][1]
    d = account.build_approval_tx({"kind": "erc20", "spender": sp})
    assert d.startswith("0x095ea7b3") and sp.lower().replace("0x", "") in d.lower() and d.endswith("f" * 64)
    d = account.build_approval_tx({"kind": "ctf", "spender": sp})
    assert d.startswith("0xa22cb465") and d.endswith("1".rjust(64, "0"))


def test_refresh_cache_is_not_an_approval(fake_client):
    account.refresh_allowance_cache(PK, 2, SAFE, FakeCreds())
    assert ("refresh",) in fake_client.calls


# ───────────── v1.2: cartera por preguntas, DNS bloqueado, reloj de reserva ─────────────
import socket  # noqa: E402

PROFILE = "0x" + "77" * 20


def test_wallet_choices_map_to_signature_types():
    assert {k: v["sig"] for k, v in account.WALLET_CHOICES.items()} == {"browser": 2, "email": 1, "own": 0}
    assert account.WALLET_CHOICES["browser"]["label"].startswith("Con MetaMask / Rabby / Coinbase Wallet")
    assert "reveal.magic.link/polymarket" in " ".join(account.WALLET_CHOICES["email"]["key_steps"])
    assert "Mostrar clave privada" in " ".join(account.WALLET_CHOICES["browser"]["key_steps"])
    assert account.choice_for_sig(2) == "browser" and account.choice_for_sig(1) == "email" and account.choice_for_sig(0) == "own"


def test_evaluate_empty_inputs_keep_everything_and_are_not_errors(isolated_env):
    wizard.write_env({"POLY_PRIVATE_KEY": PK, "POLY_SAFE_ADDRESS": SAFE})
    p = account.evaluate_wallet("browser", "", "")
    assert p.ok and p.key == "" and p.has_key and p.funder == SAFE and p.signer == SIGNER
    p = account.evaluate_wallet("browser", "", "", stored={})
    assert p.ok and not p.has_key and p.funder == ""        # nada que guardar, y tampoco es un error


def test_evaluate_validates_each_field_separately(isolated_env):
    p = account.evaluate_wallet("browser", "no-es-clave", SAFE, stored={})
    assert set(p.errors) == {"key"} and p.funder == SAFE      # la dirección buena se conserva
    p = account.evaluate_wallet("browser", PK, "0x123", stored={})
    assert set(p.errors) == {"funder"} and p.key == PK
    r = account.save_plan(p)                                   # se guarda lo válido, nada más
    assert r["saved_key"] and not r["saved_funder"] and account.read_env()["POLY_PRIVATE_KEY"] == PK
    assert "POLY_SAFE_ADDRESS" not in account.read_env()


def test_evaluate_funder_equal_signer_is_a_conflict_not_a_silent_save(isolated_env):
    p = account.evaluate_wallet("browser", PK, SIGNER, stored={})
    assert p.conflict and p.conflict["kind"] == "funder_is_signer" and not p.ok
    assert [o[0] for o in p.conflict["options"]] == ["switch_eoa", "keep"]
    with pytest.raises(ValueError):
        account.save_plan(p)                                   # sin decidir no se guarda
    assert not config.env_file().exists()
    q = account.resolve_conflict(p, "switch_eoa")
    assert q.wallet == "eoa" and q.choice == "own" and q.funder == SIGNER and q.conflict is None
    account.save_plan(q)
    assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 0
    with pytest.raises(ValueError):
        account.resolve_conflict(account.evaluate_wallet("email", PK, SIGNER, stored={}), "nonsense")


def test_evaluate_magic_maps_to_sig1_and_eoa_ignores_typed_funder_after_decision(isolated_env):
    p = account.evaluate_wallet("email", PK, SAFE, stored={})
    assert p.ok and p.wallet == "magic"
    account.save_plan(p)
    assert config.load_settings()["POLY_SIGNATURE_TYPE"] == 1
    p = account.evaluate_wallet("own", PK, SAFE, stored={})
    assert p.conflict["kind"] == "eoa_funder_differs" and p.funder == SIGNER
    assert account.resolve_conflict(p, "keep").funder == SIGNER
    p = account.evaluate_wallet("own", PK, SAFE, stored={})
    q = account.resolve_conflict(p, "switch_browser")
    assert q.wallet == "safe" and q.funder == SAFE


def test_evaluate_uses_profile_suggestion(monkeypatch, isolated_env):
    monkeypatch.setattr(account, "_lookup_fn", lambda s, **k: {"proxy_wallet": PROFILE, "name": None, "error": None})
    p = account.evaluate_wallet("browser", PK, "", stored={})
    assert p.ok and p.funder == PROFILE and p.funder_from_profile
    p = account.evaluate_wallet("browser", PK, SIGNER, stored={})            # funder == signer, pero hay perfil
    assert p.conflict["options"][0][0] == "use_suggested"
    assert account.resolve_conflict(p, "use_suggested").funder == PROFILE
    p = account.evaluate_wallet("browser", PK, SAFE, stored={})              # otra distinta de la del perfil
    assert p.conflict["kind"] == "funder_differs_from_profile"
    assert account.resolve_conflict(p, "keep").funder == SAFE
    p = account.evaluate_wallet("own", PK, "", stored={})                    # EOA pero con cuenta en Polymarket
    assert p.conflict["kind"] == "eoa_has_profile"
    q = account.resolve_conflict(p, "switch_email")
    assert q.wallet == "magic" and q.funder == PROFILE


def test_lookup_profile_address_with_real_http_client(poly):
    poly.profiles[SIGNER.lower()] = PROFILE
    import caro5m.account as a
    old = a.PROFILE_URL
    a.PROFILE_URL = poly.url + "/public-profile"
    try:
        r = account.lookup_profile_address(SIGNER)
        assert r["proxy_wallet"] == PROFILE and r["name"] == "demo" and r["error"] is None
        r = account.lookup_profile_address("0x" + "99" * 20)               # 404: sin perfil, no es un error
        assert r["proxy_wallet"] is None and r["error"] is None
        a.PROFILE_URL = "http://127.0.0.1:9/x"
        r = account.lookup_profile_address(SIGNER, timeout=2)
        assert r["proxy_wallet"] is None and r["error"]
    finally:
        a.PROFILE_URL = old


def test_lookup_goes_through_proxy(poly, proxy):
    poly.profiles[SIGNER.lower()] = PROFILE
    import caro5m.account as a
    old = a.PROFILE_URL
    a.PROFILE_URL = poly.url + "/public-profile"
    try:
        assert account.lookup_profile_address(SIGNER, proxy=proxy.url)["proxy_wallet"] == PROFILE
        assert proxy.count == 1
    finally:
        a.PROFILE_URL = old


def test_setup_incomplete_next_step_and_skip_marker(isolated_env):
    assert account.setup_incomplete({}) and account.setup_incomplete({"POLY_PRIVATE_KEY": PK})
    assert not account.setup_incomplete({"POLY_PRIVATE_KEY": PK, "POLY_SAFE_ADDRESS": SAFE})
    assert "clave privada" in account.next_step({}, 2)
    assert "dirección" in account.next_step({"POLY_PRIVATE_KEY": PK}, 2)
    assert "claves CLOB" in account.next_step({"POLY_PRIVATE_KEY": PK}, 0)          # EOA no necesita funder
    assert "comprobaciones" in account.next_step({"POLY_PRIVATE_KEY": PK, "POLY_CLOB_API_KEY": "a", "POLY_CLOB_API_SECRET": "b", "POLY_CLOB_API_PASSPHRASE": "c"}, 0)
    assert not account.setup_skipped()
    account.mark_setup_skipped()
    assert account.setup_skipped()


# ── errores de red en llano ──
def test_net_error_kind_classification():
    assert account.net_error_kind(socket.gaierror(-5, "No address associated with hostname")) == "dns"
    assert account.net_error_kind(Exception("ConnectError: [Errno -5] No address associated with hostname")) == "dns"
    assert account.net_error_kind(Exception("[Errno -2] Name or service not known")) == "dns"
    assert account.net_error_kind(Exception("ConnectError: [Errno 104] Connection reset by peer")) == "reset"
    assert account.net_error_kind(TimeoutError("timed out")) == "timeout"
    assert account.net_error_kind(ValueError("otra cosa")) == "other"
    try:  # causa encadenada
        try:
            raise socket.gaierror(-5, "x")
        except OSError as inner:
            raise RuntimeError("envoltorio") from inner
    except RuntimeError as e:
        assert account.net_error_kind(e) == "dns"


def test_dns_failure_in_probe_is_explained_plainly(monkeypatch):
    class Boom:
        def get(self, *a, **k):
            raise OSError("[Errno -5] No address associated with hostname")

        def close(self):
            pass

    monkeypatch.setattr(account, "_client_factory", lambda *a, **k: Boom())
    r = account.probe_proxy(None, "http://x.invalid")
    assert r["error_kind"] == "dns"
    lvl, txt = account.describe_probe(r)
    assert lvl == "fail" and "bloqueo de DNS" in txt and "España" in txt and "Errno" not in txt


def test_check_dns_ok_blocked_with_doh_and_with_proxy(monkeypatch):
    monkeypatch.setattr(account, "_resolve", lambda h, p: [(2, 1, 6, "", ("104.18.0.1", 443))])
    assert account.check_dns().level == "ok"

    def fail(h, p):
        raise socket.gaierror(-5, "No address associated with hostname")

    class DoH:
        def get(self, url, **k):
            class R:
                def json(self_):
                    return {"Answer": [{"type": 1, "data": "104.18.0.1"}]}
            return R()

        def close(self):
            pass

    monkeypatch.setattr(account, "_resolve", fail)
    monkeypatch.setattr(account, "_client_factory", lambda *a, **k: DoH())
    c = account.check_dns()
    assert c.level == "fail" and "bloqueo de DNS" in c.detail and "confirmado" in c.detail
    assert "http://" in c.fix and "1.1.1.1" in c.fix and "DoH" in c.fix
    assert account.check_dns(proxy="socks5h://u:p@h:1").level == "info"
    assert account.check_dns(proxy="http://u:p@h:1").level == "info"
    c = account.check_dns(proxy="socks5://u:p@h:1")
    assert c.level == "info"                                           # httpx (órdenes/claves): el proxy resuelve el DNS también con socks5://
    c = account.check_dns(proxy="socks5://u:p@h:1", scan=True)
    assert c.level == "warn" and "socks5h://" in c.fix                 # solo «requests» (leer precios) resuelve socks5:// en local


def test_check_dns_without_doh_still_explains(monkeypatch):
    def fail(h, p):
        raise socket.gaierror(-5, "No address associated with hostname")

    class Dead:
        def get(self, *a, **k):
            raise OSError("sin red")

        def close(self):
            pass

    monkeypatch.setattr(account, "_resolve", fail)
    monkeypatch.setattr(account, "_client_factory", lambda *a, **k: Dead())
    c = account.check_dns()
    assert c.level == "fail" and "bloqueo de DNS" in c.detail and "confirmado" not in c.detail


def test_clock_falls_back_to_neutral_http_date_when_polymarket_resets(monkeypatch, poly):
    def reset(*a, **k):
        raise OSError("[Errno 104] Connection reset by peer")

    monkeypatch.setattr(account, "_clob_time", reset)
    monkeypatch.setattr(account, "CLOCK_FALLBACKS", (("servidor neutral", lambda: account._http_date_time(poly.url)),))
    c = account.check_clock(poly.url, None)
    assert c.level == "ok" and "servidor neutral" in c.detail and "reiniciada" in c.detail


def test_clock_fallback_skew_is_detected_and_chain_continues(monkeypatch):
    monkeypatch.setattr(account, "_clob_time", lambda *a: (_ for _ in ()).throw(OSError("[Errno -5] No address associated with hostname")))
    now = time.time()
    monkeypatch.setattr(account, "CLOCK_FALLBACKS", (("roto", lambda: (_ for _ in ()).throw(OSError("x"))), ("ntp falso", lambda: (now - 12, now, 0.3))))
    c = account.check_clock("http://x", None)
    assert c.level == "warn" and "+12.0" in c.detail and "ntp falso" in c.detail
    monkeypatch.setattr(account, "CLOCK_FALLBACKS", (("ntp falso", lambda: (now - 120, now, 0.3)),))
    assert account.check_clock("http://x", None).level == "fail"
    monkeypatch.setattr(account, "CLOCK_FALLBACKS", (("http", lambda: (now - 2.5, now, 1.0)),))   # ±1 s de precisión: no alarma
    assert account.check_clock("http://x", None).level == "ok"


def test_clock_nothing_works_is_a_warning_with_reason(monkeypatch):
    boom = lambda *a: (_ for _ in ()).throw(OSError("[Errno 104] Connection reset by peer"))
    monkeypatch.setattr(account, "_clob_time", boom)
    monkeypatch.setattr(account, "CLOCK_FALLBACKS", (("a", boom), ("b", boom)))
    c = account.check_clock("http://x", None)
    assert c.level == "warn" and "ninguna fuente" in c.detail and "reiniciada" in c.detail


def test_clock_primary_source_still_used_when_available(monkeypatch, poly):
    monkeypatch.setattr(account, "CLOCK_FALLBACKS", (("no debe usarse", lambda: pytest.fail("no se usa"),),))
    c = account.check_clock(poly.url, None)
    assert c.level == "ok" and "medido con" not in c.detail


def test_preflight_includes_dns_check(isolated_env, monkeypatch):
    monkeypatch.setattr(account, "_resolve", lambda h, p: [(2, 1, 6, "", ("1.1.1.1", 443))])
    monkeypatch.setattr(account, "check_clock", lambda *a: account.Check("clock", "ok", "Reloj", "ok"))
    monkeypatch.setattr(account, "check_geo_and_clob", lambda *a: [])
    monkeypatch.setattr(account, "check_rpcs", lambda *a: account.Check("rpc", "ok", "rpc", "ok"))
    monkeypatch.setattr(account, "check_binance", lambda *a: account.Check("b", "ok", "b", "ok"))
    ids = [c.id for c in account.run_preflight()]
    assert "dns" in ids


def test_preflight_warns_when_funder_is_the_signer_miguels_case(isolated_env, monkeypatch):
    wizard.write_env({"POLY_PRIVATE_KEY": PK, "POLY_SAFE_ADDRESS": SIGNER})
    config.save_settings({"POLY_SIGNATURE_TYPE": "2"})
    c = {x.id: x for x in account.run_preflight(with_network=False)}
    assert c["funder_is_signer"].level == "warn" and "MISMA" in c["funder_is_signer"].detail and "Buscar mi dirección" in c["funder_is_signer"].fix
    config.save_settings({"POLY_SIGNATURE_TYPE": "0"})
    assert "funder_is_signer" not in {x.id for x in account.run_preflight(with_network=False)}
