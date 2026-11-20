"""v1.2.1: proxy que se puede añadir de verdad (formatos de proveedor, Enter, pantallas pequeñas), claves CLOB por el proxy,
errores en español, parada en el paso del proxy si el DNS está bloqueado, y registro de errores copiable y enmascarado."""
import os
import socket
import stat
import struct
import threading

import httpx
import pytest
from textual.widgets import Checkbox, Input, Static, TextArea

from caro5m import account, config, setup, setuplog, wizard
from caro5m.tui.app import AccountScreen, ErrorsScreen, ProxyScreen
from test_account import FakeClient, FakeCreds, PK, SAFE, SIGNER, fake_client  # noqa: F401
from test_setup_cli import FakeIO
from test_tui_account import out_text, put, run, settle, txt, with_app

# Credenciales FALSAS de pruebas (nunca un proxy real).
U, PW, IP = "usr9x", "p4ss!w0rd", "10.77.88.99"


def blocked_dns(monkeypatch):
    def fail(h, p=None, *a, **k):
        raise socket.gaierror(-5, "No address associated with hostname")

    monkeypatch.setattr(account, "_resolve", fail)


# ── formatos de proxy (causa de «no me permite agregar proxy»: el formato host:puerto:usuario:clave se rechazaba) ──
@pytest.mark.parametrize("text,expected", [
    (f"http://{U}:abc@{IP}:18080", f"http://{U}:abc@{IP}:18080"),
    (f"{IP}:18080:{U}:abc", f"http://{U}:abc@{IP}:18080"),                       # formato típico de proveedores
    (f"  {IP}:18080:{U}:abc\n", f"http://{U}:abc@{IP}:18080"),                    # con espacios/salto de línea al pegar
    (f"'{IP}:18080:{U}:abc'", f"http://{U}:abc@{IP}:18080"),                      # con comillas
    (f"{U}:abc@{IP}:18080", f"http://{U}:abc@{IP}:18080"),
    (f"{U}:abc:{IP}:18080", f"http://{U}:abc@{IP}:18080"),
    (f"{IP}:8080", f"http://{IP}:8080"),
    (f"socks5h://{U}:abc@{IP}:1080/", f"socks5h://{U}:abc@{IP}:1080"),
    (f"{IP}:18080:{U}:{PW}", f"http://{U}:p4ss%21w0rd@{IP}:18080"),                # caracteres especiales: se codifican
    (f"http://{U}:p%40ss@{IP}:1", f"http://{U}:p%40ss@{IP}:1"),
    (f"https://{U}:a@b@{IP}:443", f"https://{U}:a%40b@{IP}:443"),                  # @ dentro de la clave
    (f"{IP}:18080:{U}:ab:cd", f"http://{U}:ab%3Acd@{IP}:18080"),                   # ':' dentro de la clave
])
def test_proxy_formats_are_accepted_and_normalized(text, expected):
    assert account.parse_proxy(text) == expected
    assert wizard.validate_proxy(text) == expected
    assert PW not in account.mask_proxy(text) and U not in account.mask_proxy(text)


@pytest.mark.parametrize("text,frag", [
    ("", "vacío"), (IP, "puerto"), (f"{IP}:abc", "puerto"), (f"{IP}:99999", "puerto"), ("ftp://x:1", "admitido"),
    (f"{IP}:abc:u:p", "formato"), ("http://:80", "host"),
])
def test_proxy_errors_are_plain_spanish_with_the_valid_formats(text, frag):
    with pytest.raises(ValueError) as e:
        account.parse_proxy(text)
    assert frag in str(e.value).lower()
    if text and not text.startswith("ftp"):
        assert "host:puerto:usuario:clave" in str(e.value)


def test_httpx_accepts_the_normalized_proxy_with_special_chars():
    c = account.build_httpx_client(account.parse_proxy(f"{IP}:18080:{U}:{PW}"))
    c.close()


def test_describe_proxy_input_masks_credentials():
    lvl, t = account.describe_proxy_input(f"{IP}:18080:{U}:{PW}")
    assert lvl == "ok" and U not in t and PW not in t and "18080" in t and "ocultos" in t
    assert account.describe_proxy_input(IP)[0] == "fail"


# ── DNS: ¿quién resuelve polymarket.com cuando hay proxy? (stubs locales; sin red real) ──
class StubProxy:
    """Servidor TCP mínimo que registra lo que recibe (HTTP CONNECT o SOCKS5) y responde con error."""

    def __init__(self, kind):
        self.kind, self.seen, self.srv = kind, [], socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(8)
        self.port = self.srv.getsockname()[1]
        self.t = threading.Thread(target=self.loop, daemon=True)
        self.t.start()

    def loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            try:
                c.settimeout(3)
                (self.http if self.kind == "http" else self.socks)(c)
            except Exception:
                pass
            finally:
                c.close()

    def http(self, c):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = c.recv(4096)
            if not chunk:
                break
            data += chunk
        self.seen.append(("http", data.split(b"\r\n")[0].decode(), b"proxy-authorization" in data.lower()))
        c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")

    def socks(self, c):
        g = c.recv(262)                                   # 05 nmethods methods…
        if 2 in g[2:2 + g[1]]:
            c.sendall(b"\x05\x02")
            c.recv(512)                                   # usuario/clave
            c.sendall(b"\x01\x00")
        else:
            c.sendall(b"\x05\x00")
        req = c.recv(512)                                 # 05 01 00 ATYP …
        atyp = req[3]
        name = req[5:5 + req[4]].decode() if atyp == 3 else None
        self.seen.append(("socks", atyp, name))
        c.sendall(b"\x05\x05\x00\x01\x00\x00\x00\x00\x00\x00")   # connection refused

    def close(self):
        self.srv.close()


@pytest.fixture
def stub():
    made = []

    def make(kind):
        s = StubProxy(kind)
        made.append(s)
        return s

    yield make
    for s in made:
        s.close()


def test_http_proxy_sends_the_hostname_to_the_proxy_not_resolved_locally(stub):
    s = stub("http")
    c = account.build_httpx_client(f"http://{U}:abc@127.0.0.1:{s.port}", 5.0)
    with pytest.raises(httpx.RequestError):
        c.get("https://polymarket.invalid/api/geoblock")
    c.close()
    kind, line, auth = s.seen[0]
    assert line.startswith("CONNECT polymarket.invalid:443") and auth                  # el NOMBRE llega al proxy (DNS remoto)


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_httpx_socks_sends_a_domain_name_for_both_schemes(stub, scheme):
    s = stub("socks")
    c = account.build_httpx_client(f"{scheme}://{U}:abc@127.0.0.1:{s.port}", 5.0)
    with pytest.raises(httpx.RequestError):
        c.get("https://polymarket.invalid/")
    c.close()
    assert s.seen and s.seen[0] == ("socks", 3, "polymarket.invalid")                # ATYP 3 = dominio: lo resuelve el proxy


def test_requests_socks5_resolves_locally_but_socks5h_sends_the_name(stub):
    """La única diferencia real: las lecturas por «requests» (Gamma/libro con CARO5M_PROXY_SCAN=1)."""
    import requests

    s5 = stub("socks")
    with pytest.raises(requests.exceptions.RequestException):
        requests.get("https://polymarket.invalid/", proxies={"https": f"socks5://127.0.0.1:{s5.port}"}, timeout=5)
    assert not [x for x in s5.seen if x[1] == 3]                                      # socks5://: nunca llegó el nombre (DNS local)
    s5h = stub("socks")
    with pytest.raises(requests.exceptions.RequestException):
        requests.get("https://polymarket.invalid/", proxies={"https": f"socks5h://127.0.0.1:{s5h.port}"}, timeout=5)
    assert s5h.seen and s5h.seen[0] == ("socks", 3, "polymarket.invalid")


def test_keygen_really_goes_through_the_proxy_and_error_is_plain_spanish(stub, isolated_env, monkeypatch):
    """Cliente CLOB real (sin dobles) contra un proxy falso que rechaza: hay CONNECT al host del CLOB y el error sale traducido."""
    s = stub("http")
    with pytest.raises(account.ClobError) as e:
        account.generate_credentials(PK, 2, SAFE, proxy=f"http://{U}:abc@127.0.0.1:{s.port}", host="https://clob.polymarket.invalid")
    assert s.seen and s.seen[0][1].startswith("CONNECT clob.polymarket.invalid:443")
    msg = str(e.value)
    assert "PolyApiException" not in msg and "Request exception" not in msg
    assert "proxy" in msg.lower()
    log = setuplog.errors_log().read_text()
    assert "claves CLOB" in log and "Traceback" in log and PK not in log and PK[2:] not in log and "abc@" not in log


# ── errores del CLOB en español ──
def _poly_exc(status=None, msg="Request exception!", cause=None):
    from py_clob_client_v2.exceptions import PolyApiException

    try:
        try:
            raise cause or OSError("x")
        except Exception:
            raise PolyApiException(error_msg=msg)
    except PolyApiException as e:
        e.status_code = status
        return e


def test_explain_clob_error_dns_without_proxy_says_configure_proxy():
    e = _poly_exc(cause=socket.gaierror(-5, "No address associated with hostname"))
    m = account.explain_clob_error(e, None)
    assert "proxy" in m.lower() and "PolyApiException" not in m and "DNS" in m
    assert "resolver" in account.explain_clob_error(e, "http://u:p@h:1") and "Probar" in account.explain_clob_error(e, "http://u:p@h:1")


def test_explain_clob_error_other_cases():
    assert "conectar" in account.explain_clob_error(_poly_exc(), None) and "Prueba con un proxy" in account.explain_clob_error(_poly_exc(), None)
    assert "cortó" in account.explain_clob_error(_poly_exc(cause=ConnectionResetError(104, "Connection reset by peer")), None)
    assert "región" in account.explain_clob_error(_poly_exc(403, "Trading restricted in your region"), "http://h:1")
    assert "firma" in account.explain_clob_error(_poly_exc(401, "unauthorized"), None)
    assert "sin registrar" in account.explain_clob_error(_poly_exc(400, "Could not create api key"), None)
    assert "429" in account.explain_clob_error(_poly_exc(429, "slow down"), None)
    assert "problema" in account.explain_clob_error(_poly_exc(503, "bad"), None)
    assert account.explain_clob_error(account.NeedProxy("x"), None) == "x"


def test_gate_stops_before_any_network_when_dns_blocked_and_no_proxy(monkeypatch, isolated_env, fake_client):
    blocked_dns(monkeypatch)
    with pytest.raises(account.NeedProxy) as e:
        account.generate_credentials(PK, 2, SAFE)
    assert "proxy" in str(e.value).lower() and not fake_client.calls                     # ni siquiera se crea el cliente
    v = account.validate_credentials(PK, 2, SAFE, FakeCreds())
    assert v["need_proxy"] and not v["ok"]
    # con proxy configurado no se mira el DNS local (lo resuelve el proxy)
    creds = account.generate_credentials(PK, 2, SAFE, proxy="http://u:p@h.example:1")
    assert creds.api_key


# ── registro enmascarado ──
def test_log_files_are_private_masked_and_have_full_tracebacks(isolated_env):
    wizard.write_env({"POLY_PRIVATE_KEY": PK, "POLY_CLOB_API_SECRET": "SECRET-abcdefghij", "POLYMARKET_PROXY_LIST": f"http://{U}:{PW}@{IP}:18080"})
    try:
        raise RuntimeError(f"boom {PK} SECRET-abcdefghij via http://{U}:{PW}@{IP}:18080 {U} {PW} desde {IP} cuenta {SAFE}")
    except RuntimeError as ex:
        setuplog.error("prueba", ex, f"extra {PK[2:]}")
    setuplog.event(f"proxy {IP}:18080:{U}:{PW}")
    for p in (setuplog.errors_log(), setuplog.setup_log()):
        text = p.read_text()
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
        for secret in (PK, PK[2:], "SECRET-abcdefghij", U, PW, IP, SAFE):
            assert secret not in text, secret
    t = setuplog.errors_log().read_text()
    assert "Traceback (most recent call last)" in t and "RuntimeError" in t and "••••" in t and "0x1212…1212" in t
    assert "boom" in setuplog.report("errors") and "caro5m " in setuplog.report("errors")


def test_mask_text_misc(isolated_env):
    assert setuplog.mask_text("ip 8.8.4.4 y 127.0.0.1") == "ip 8.8.•••.••• y 127.0.0.1"
    assert PK not in setuplog.mask_text(PK) and PK[2:] not in setuplog.mask_text(PK[2:])
    assert "••••" in setuplog.mask_text("socks5h://abc:def@5.181.2.3:1080") and "abc" not in setuplog.mask_text("socks5h://abc:def@5.181.2.3:1080")


def test_library_logger_is_captured_to_the_log_not_stderr(isolated_env, capsys):
    import logging

    setuplog.install_library_capture()
    logging.getLogger("py_clob_client_v2").error("request error: %s", f"http://{U}:{PW}@{IP}:1")
    assert capsys.readouterr().err == ""
    t = setuplog.setup_log().read_text()
    assert "LIB ERROR" in t and PW not in t


def test_log_rotates(isolated_env, monkeypatch):
    monkeypatch.setattr(setuplog, "MAX_BYTES", 200)
    for i in range(30):
        setuplog.event("x" * 40 + str(i))
    assert setuplog.setup_log().with_suffix(".log.1").exists()


def test_copy_text_uses_system_tools_then_falls_back_to_osc52(monkeypatch):
    calls = []

    class R:
        returncode = 0

    def fake_run(cmd, input=None, **k):
        calls.append((cmd[0], input))
        if cmd[0] != "xclip":
            raise FileNotFoundError(cmd[0])
        return R()

    monkeypatch.setattr(setuplog.subprocess, "run", fake_run)
    monkeypatch.setitem(__import__("sys").modules, "pyperclip", None)               # sin pyperclip
    sent = []

    class App:
        def copy_to_clipboard(self, t):
            sent.append(t)

    msg = setuplog.copy_text("hola", App())
    assert "xclip" in msg and calls[-1] == ("xclip", b"hola") and sent == ["hola"]
    monkeypatch.setattr(setuplog.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    assert "OSC52" in setuplog.copy_text("hola", App())
    assert "No pude" in setuplog.copy_text("hola", None)


# ── CLI ──
@pytest.fixture
def cli_quiet(monkeypatch):
    monkeypatch.setattr(account, "run_preflight", lambda **k: [account.Check("x", "ok", "Todo", "bien")])
    monkeypatch.setattr(account, "probe_proxy", lambda proxy=None, *a, **k: {"via": "proxy" if proxy else "directo", "ok": True, "blocked": False, "country": "NL",
                                                                              "region": "NH", "latency_ms": 80, "ip": "1.1.1.1", "clob_ok": True, "error": None})


def test_cli_log_option_and_non_interactive_error_is_logged(isolated_env, monkeypatch, capsys, cli_quiet, fake_client):
    setup.apply_wallet("safe", PK, SAFE)
    blocked_dns(monkeypatch)
    io = FakeIO()
    assert setup.main(["--non-interactive"], io) == 0
    monkeypatch.setenv("CARO5M_SETUP_GENERATE_KEYS", "1")
    monkeypatch.setenv("CARO5M_SETUP_CHECKS", "0")
    io = FakeIO()
    assert setup.main(["--non-interactive"], io) == 1
    assert "proxy" in io.text.lower() and "PolyApiException" not in io.text and "errors.log" in io.text
    io = FakeIO()
    assert setup.main(["--log"], io) == 0 and str(setuplog.errors_log()) in io.text
    assert PK not in io.text


def test_cli_keys_step_stops_at_proxy_then_continues_when_configured(isolated_env, monkeypatch, fake_client, cli_quiet):
    setup.apply_wallet("safe", PK, SAFE)
    blocked_dns(monkeypatch)
    io = FakeIO(["s", "s", "a", f"{IP}:18080:{U}:abc", "", "n"])     # ¿generar? sí · ¿configurar proxy ahora? sí · añadir · proxy en formato de proveedor · (Enter) · ¿usar para leer precios? no
    setup.step_keys(io)
    assert account.read_env()["POLYMARKET_PROXY_LIST"] == f"http://{U}:abc@{IP}:18080"
    assert ("keygen", None) in fake_client.calls and "Claves guardadas" in io.text
    assert "bloquea polymarket.com" in io.text and f"{U}:abc" not in io.text


def test_cli_checks_are_written_to_the_log(isolated_env, monkeypatch):
    setuplog.record_check(account.Check("dns", "fail", "DNS de polymarket.com", "bloqueado 8.8.8.8", "usa proxy"))
    t = setuplog.errors_log().read_text()
    assert "FAIL" in t and "8.8.•••.•••" in t and "usa proxy" in t


# ── TUI: el proxy se puede añadir en cualquier tamaño de terminal ──
SIZES = [(120, 35), (100, 28), (80, 24), (128, 36)]       # 128×36 ≈ pantalla de 1024×576 con letra 8×16


@pytest.mark.parametrize("size", SIZES)
def test_proxy_screen_fits_and_enter_saves_provider_format(isolated_env, size):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await pilot.press("p")                                   # atajo: no hace falta bajar a la sección 2
        await pilot.pause(0.3)
        assert isinstance(app.screen, ProxyScreen)
        w, h = size
        for wid in ("#proxy-input", "#proxy-save", "#proxy-test", "#proxy-clear", "#proxy-cancel"):
            r = app.screen.query_one(wid).region
            assert r.width > 0 and r.x >= 0 and r.y >= 0 and r.x + r.width <= w and r.y + r.height <= h, (wid, r, size)
        await pilot.press(*f"{IP}:18080:{U}:abc")                 # se escribe tecla a tecla
        await pilot.pause(0.2)
        assert "Entendido" in txt(app) and U not in txt(app) and "ocultos" in txt(app)
        await pilot.press("enter")                                # Enter guarda (antes no hacía nada)
        await pilot.pause(0.4)
        assert isinstance(app.screen, AccountScreen)
        assert account.read_env()["POLYMARKET_PROXY_LIST"] == f"http://{U}:abc@{IP}:18080"
        o = out_text(app)
        assert "Proxy guardado" in o and U not in txt(app) and "abc@" not in txt(app)

    run(with_app(body, size=size))


@pytest.mark.parametrize("size", SIZES)
def test_proxy_screen_buttons_work_with_the_mouse(isolated_env, size):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await pilot.press("f2")                                  # F2 también abre el proxy
        await pilot.pause(0.3)
        assert isinstance(app.screen, ProxyScreen)
        app.screen.query_one("#proxy-input", Input).value = f"{IP}:1:{U}:abc"
        await pilot.pause(0.2)
        await pilot.click("#proxy-save")
        await pilot.pause(0.4)
        assert account.read_env()["POLYMARKET_PROXY_LIST"] == f"http://{U}:abc@{IP}:1"

    run(with_app(body, size=size))


def test_proxy_screen_paste_show_hide_empty_enter_cancel_and_quit(isolated_env):
    from textual import events

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.2)
        await pilot.press("p")
        await pilot.pause(0.3)
        s = app.screen
        await pilot.press("enter")                                # vacío: avisa, no se cierra ni se rompe
        await pilot.pause(0.2)
        assert isinstance(app.screen, ProxyScreen) and "Todavía no has escrito" in txt(app)
        inp = s.query_one("#proxy-input", Input)
        inp.focus()
        inp.post_message(events.Paste(f"{IP}:18080:{U}:abc\n"))   # pegar con salto de línea final
        await pilot.pause(0.3)
        assert "Entendido" in txt(app)
        assert inp.password is True
        s.query_one("#proxy-show", Checkbox).value = True
        await pilot.pause(0.2)
        assert inp.password is False
        await pilot.press("escape")                               # cancelar no guarda nada
        await pilot.pause(0.3)
        assert isinstance(app.screen, AccountScreen) and "POLYMARKET_PROXY_LIST" not in account.read_env()
        await pilot.press("p")
        await pilot.pause(0.3)
        app.screen.query_one("#proxy-input", Input).value = "esto no es un proxy"
        await pilot.pause(0.2)
        assert "Falta el puerto" in txt(app)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ProxyScreen) and "POLYMARKET_PROXY_LIST" not in account.read_env()

    run(with_app(body, size=(80, 24)))


def test_proxy_screen_test_button_and_remove(isolated_env, monkeypatch):
    seen = []

    def fake_probe(proxy=None, *a, **k):
        seen.append(proxy)
        return {"via": "proxy", "ok": True, "blocked": False, "country": "NL", "region": "NH", "latency_ms": 90, "ip": IP, "clob_ok": True, "error": None}

    monkeypatch.setattr(account, "probe_proxy", fake_probe)
    wizard.write_env({"POLYMARKET_PROXY_LIST": f"http://{U}:abc@{IP}:18080"})

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.2)
        await pilot.press("p")
        await pilot.pause(0.3)
        assert "Proxy guardado ahora" in txt(app) and U not in txt(app)
        await pilot.click("#proxy-test")                          # sin escribir nada: prueba el guardado
        await settle(app, pilot)
        assert seen == [f"http://{U}:abc@{IP}:18080"] and "NL" in txt(app)
        await pilot.click("#proxy-clear")
        await pilot.pause(0.4)
        assert "POLYMARKET_PROXY_LIST" not in account.read_env() and "Proxy eliminado" in out_text(app)

    run(with_app(body, size=(100, 30)))


def test_account_screen_points_to_the_proxy_and_to_the_errors(isolated_env):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        t = txt(app)
        assert "Teclas: P proxy · E errores" in t and "errors.log" in t
        assert "ninguno" in t

    run(with_app(body, size=(100, 28)))


# ── TUI: «Guardar y continuar» con el DNS bloqueado y sin proxy ──
def test_save_and_continue_stops_at_proxy_and_resumes_after_saving(isolated_env, monkeypatch, fake_client):
    blocked_dns(monkeypatch)
    ran = []
    monkeypatch.setattr(account, "run_preflight", lambda **k: ran.append(1) or [account.Check("x", "ok", "Todo", "bien")])

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await put(app, pilot, "#acc-key", PK)
        await put(app, pilot, "#acc-funder", SAFE)
        await pilot.click("#acc-save-go")
        await settle(app, pilot)
        await settle(app, pilot)
        assert isinstance(app.screen, ProxyScreen)                  # se para y abre el paso del proxy
        assert not fake_client.calls and not ran                    # ni claves ni comprobaciones todavía
        assert "bloquea polymarket.com" in txt(app)
        app.screen.query_one("#proxy-input", Input).value = f"{IP}:18080:{U}:abc"
        await pilot.pause(0.2)
        await pilot.press("enter")
        await settle(app, pilot)
        await settle(app, pilot)
        assert isinstance(app.screen, AccountScreen)
        o = out_text(app)
        assert "Claves CLOB generadas" in o and ran, o
        assert account.read_env()["POLY_CLOB_API_KEY"] == "AK-1234567890"
        assert U not in txt(app) and "abc@" not in txt(app)

    run(with_app(body))


def test_save_and_continue_clob_error_is_plain_spanish_and_points_to_the_log(isolated_env, monkeypatch):
    monkeypatch.setattr(account, "run_preflight", lambda **k: [account.Check("x", "ok", "Todo", "bien")])

    class Boom:
        def __init__(self, *a, **k):
            pass

        def create_or_derive_api_key(self, nonce=None):
            raise _poly_exc(cause=httpx.ConnectError("[Errno 104] Connection reset by peer"))

    monkeypatch.setattr(account, "_make_client_fn", lambda *a, **k: Boom())

    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await put(app, pilot, "#acc-key", PK)
        await put(app, pilot, "#acc-funder", SAFE)
        await pilot.click("#acc-save-go")
        await settle(app, pilot)
        await settle(app, pilot)
        o = out_text(app)
        assert "PolyApiException" not in o and "Request exception" not in o and "cortó" in o and "pulsa E" in o
        await pilot.press("e")                                       # el error completo se puede ver y copiar
        await pilot.pause(0.4)
        assert isinstance(app.screen, ErrorsScreen)
        body_text = app.screen.query_one("#err-text", TextArea).text
        assert "Traceback" in body_text and "Request exception" in body_text and PK not in body_text and PK[2:] not in body_text

    run(with_app(body, size=(118, 110)))


# ── TUI: pantalla de errores copiable ──
def test_errors_screen_shows_log_path_and_copies(isolated_env, monkeypatch):
    setuplog.error("claves CLOB", RuntimeError(f"fallo con {PK} y {IP}"))
    copied = []
    monkeypatch.setattr(setuplog, "copy_text", lambda text, app=None: copied.append(text) or "Copiado al portapapeles (prueba).")

    async def body(app, pilot):
        await pilot.press("e")                                       # desde el panel principal
        await pilot.pause(0.4)
        assert isinstance(app.screen, ErrorsScreen)
        t = txt(app)
        assert "errors.log" in t and "fallo con" in t and PK not in t and IP not in t
        await pilot.press("c")                                       # sin selección: copia todo
        await pilot.pause(0.3)
        assert copied and "fallo con" in copied[-1] and PK not in copied[-1] and IP not in copied[-1] and "caro5m 1." in copied[-1]
        assert "Copiado" in txt(app)
        ta = app.screen.query_one("#err-text", TextArea)
        ta.select_all()
        await pilot.click("#err-copy")
        await pilot.pause(0.3)
        assert len(copied) == 2
        await pilot.press("t")                                       # alterna a registro completo
        await pilot.pause(0.3)
        assert "setup.log" in txt(app)
        await pilot.press("escape")
        await pilot.pause(0.2)
        assert not isinstance(app.screen, ErrorsScreen)

    run(with_app(body, size=(100, 30)))


def test_errors_screen_empty_state(isolated_env):
    async def body(app, pilot):
        await pilot.press("a")
        await pilot.pause(0.3)
        await pilot.press("e")
        await pilot.pause(0.4)
        assert isinstance(app.screen, ErrorsScreen) and "sin errores registrados" in txt(app)

    run(with_app(body, size=(80, 24)))


# ── proxies rotativos que rechazan algún túnel suelto (visto en el proveedor de pruebas) ──
def test_retry_transient_retries_proxy_errors_but_not_http_errors():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ProxyError("403 Forbidden")
        return "ok"

    assert account.retry_transient(flaky) == "ok" and len(calls) == 3
    n = []

    def http_error():
        n.append(1)
        raise _poly_exc(403, "Trading restricted in your region")

    with pytest.raises(Exception):
        account.retry_transient(http_error)
    assert len(n) == 1                                                        # un 403 de Polymarket no se reintenta
    n.clear()

    def always():
        n.append(1)
        raise httpx.ProxyError("403 Forbidden")

    with pytest.raises(httpx.ProxyError):
        account.retry_transient(always)
    assert len(n) == 3


def test_proxy_403_has_its_own_plain_message_and_probe_keeps_the_geoblock_verdict():
    e = _poly_exc(cause=httpx.ProxyError("403 Forbidden"))
    m = account.explain_clob_error(e, "http://u:p@h:1")
    assert "403" in m and "proveedor" in m and "PolyApiException" not in m
    r = {"via": "proxy", "ok": False, "blocked": True, "country": "NL", "region": None, "latency_ms": None, "error": "CLOB: ProxyError: 403 Forbidden", "error_kind": "proxy"}
    lvl, t = account.describe_probe(r)
    assert lvl == "fail" and "BLOQUEA" in t and "NL" in t
    r = dict(r, blocked=None)
    assert "403" in account.describe_probe(r)[1] and "proveedor" in account.describe_probe(r)[1]
