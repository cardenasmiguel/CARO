"""Configuración de cuenta para poder operar: tipo de cartera, claves CLOB, proxy, comprobaciones y aprobaciones.

Lógica SIN interfaz, compartida por ``python -m caro5m.setup`` (instalador / terminal) y la pantalla
«Configuración de cuenta» del panel. Reglas de seguridad:
  * las claves solo viven en el .env (0600); aquí solo se muestran enmascaradas y NUNCA se registran en logs;
  * nada se envía a la cadena sin ``confirmed=True`` (y la interfaz exige S/N explícito);
  * todo lo que sale hacia Polymarket (derivar claves, validar, probar) puede ir por el proxy configurado.
"""

from __future__ import annotations

import contextlib
import re
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlparse

from . import config, setuplog, wizard

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"  # endpoint público de Polymarket: {blocked, ip, country, region}
UA = "caro5m-setup/1.2 (read-only checks)"
PROFILE_URL = "https://gamma-api.polymarket.com/public-profile"  # público: ?address=<EOA firmante> -> {proxyWallet, name, …}
DOH_URL = "https://cloudflare-dns.com/dns-query"  # DNS sobre HTTPS (JSON) para distinguir bloqueo de DNS

# ── tipos de cartera ───────────────────────────────────────────────
WALLET_TYPES: Dict[str, Dict[str, Any]] = {
    "eoa": {
        "sig": 0, "label": "EOA (cartera propia: MetaMask, etc.)",
        "help": ("Operas directamente con la dirección de tu clave privada. El saldo (USDC) y el gas (POL) están en esa dirección. "
                 "Los permisos (allowances) hay que aprobarlos on-chain: este asistente puede hacerlo si das tu confirmación."),
    },
    "magic": {
        "sig": 1, "label": "Magic / email (proxy de Polymarket)",
        "help": ("Entraste en Polymarket con email o Google. La clave privada se exporta desde reveal.magic.link/polymarket "
                 "y la «dirección de depósito» de tu perfil es el funder. Los permisos se aprueban solos al depositar/operar en polymarket.com."),
    },
    "safe": {
        "sig": 2, "label": "Safe (cartera del navegador conectada a Polymarket)",
        "help": ("Conectaste MetaMask u otra cartera a polymarket.com y Polymarket creó un Safe a tu nombre. La clave es la de tu cartera "
                 "(la que firma) y el funder es la dirección del Safe (tu perfil → copiar dirección). Los permisos se aprueban desde la web."),
    },
}
SIG_TO_KEY = {v["sig"]: k for k, v in WALLET_TYPES.items()}


def wallet_key(sig: int) -> str:
    return SIG_TO_KEY.get(int(sig), "safe")


# ── la pregunta de la cartera, en lenguaje llano ───────────────────
# Cada opción de «¿Cómo entras en polymarket.com?» se traduce a un tipo técnico (signature_type) sin que el usuario tenga que saberlo.
WALLET_CHOICES: Dict[str, Dict[str, Any]] = {
    "browser": {
        "wallet": "safe", "sig": 2,
        "label": "Con MetaMask / Rabby / Coinbase Wallet / otra cartera del navegador",
        "short": "cartera del navegador (MetaMask, Rabby…)",
        "key_steps": [
            "MetaMask: abre la extensión → ⋮ (tres puntos) junto a tu cuenta → «Detalles de la cuenta» → «Mostrar clave privada» (te pide tu contraseña) → copiar.",
            "Rabby: Ajustes → Direcciones → tu cuenta → «Ver clave privada».",
            "Coinbase Wallet / otra: busca «Mostrar» o «Exportar clave privada» en Ajustes → Seguridad.",
            "Es la clave de la cartera con la que conectas a Polymarket (no la frase de 12 palabras ni la dirección pública).",
        ],
        "funder_steps": [
            "Entra en polymarket.com con tu cartera → clic en tu avatar → «Perfil».",
            "Copia la dirección que aparece junto a tu nombre (o pulsa «Depositar» y copia la dirección de depósito). Esa es tu cuenta de Polymarket.",
            "OJO: NO es la dirección de MetaMask. Si lo prefieres, deja este campo vacío y la buscamos por ti a partir de tu clave.",
        ],
    },
    "email": {
        "wallet": "magic", "sig": 1,
        "label": "Con email o Google (Magic)",
        "short": "email o Google (Magic)",
        "key_steps": [
            "Abre reveal.magic.link/polymarket e inicia sesión con el MISMO email o cuenta de Google que usas en Polymarket.",
            "Pulsa «Reveal private key» (revelar clave privada) y cópiala.",
        ],
        "funder_steps": [
            "Entra en polymarket.com → clic en tu avatar → «Perfil» (o «Depositar»).",
            "Copia la dirección de tu perfil / de depósito. Si prefieres, deja el campo vacío y la buscamos por ti a partir de tu clave.",
        ],
    },
    "own": {
        "wallet": "eoa", "sig": 0,
        "label": "Uso mi propia cartera sin la web de Polymarket (avanzado, EOA)",
        "short": "cartera propia sin la web (EOA, avanzado)",
        "key_steps": [
            "Necesitas la clave privada de la cartera que tiene los USDC y algo de POL (para el gas) en la red Polygon.",
            "Usa solo una cartera dedicada al bot, con poco saldo.",
        ],
        "funder_steps": ["No hace falta: en este modo los fondos están en la dirección de tu propia clave."],
    },
}
WALLET_TO_CHOICE = {v["wallet"]: k for k, v in WALLET_CHOICES.items()}


def choice_for_sig(sig: int) -> str:
    return WALLET_TO_CHOICE[wallet_key(sig)]


def detect_wallet(private_key: str, funder: str) -> Tuple[str, str]:
    """Sugerencia (tipo, explicación) a partir de la clave y la dirección de fondos. Es solo una pista."""
    signer = wizard.signer_address(private_key) if private_key else None
    if not funder or (signer and funder.lower() == signer.lower()):
        return "eoa", "La dirección de fondos es la de tu clave (o está vacía): parece una cartera EOA."
    return "safe", ("La dirección de fondos es distinta de la de tu clave: lo habitual es un Safe (conectaste una cartera a Polymarket). "
                    "Si entraste con email/Google elige «Magic».")


# ── enmascarado (nunca se muestra un secreto completo) ─────────────
def mask_secret(v: str, keep: int = 4) -> str:
    v = v or ""
    if not v:
        return "—"
    return v[:keep] + "…" + v[-2:] if len(v) > keep + 6 else "••••"


def mask_proxy(url: str) -> str:
    """Muestra el proxy sin credenciales: http://••••:••••@host:puerto (varios separados por coma)."""
    outs = []
    for it in [x.strip() for x in (url or "").replace(";", ",").split(",") if x.strip()]:
        try:
            scheme, user, _pw, host, port = split_proxy(it)
            outs.append(f"{scheme}://{'••••:••••@' if user else ''}{host}:{port}")
        except ValueError:
            outs.append("••••")
    return ", ".join(outs) or "—"


# ── proxy ──────────────────────────────────────────────────────────
PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


_HOSTLIKE = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)$")
_PROXY_FORMS = ("esquema://usuario:clave@host:puerto", "usuario:clave@host:puerto", "host:puerto:usuario:clave", "host:puerto")
PROXY_FORMAT_HELP = "Formatos válidos: http://usuario:clave@host:puerto · host:puerto:usuario:clave · usuario:clave@host:puerto · host:puerto"


def _is_port(x: str) -> bool:
    return x.isdigit() and 0 < int(x) < 65536


def _split_one(text: str) -> Tuple[str, str, str, str, str]:
    """(esquema, usuario, clave, host, puerto) de UNA sola línea de proxy. Admite los formatos habituales:
    ``esquema://usuario:clave@host:puerto``, ``usuario:clave@host:puerto``, ``host:puerto:usuario:clave`` (el que dan casi todos los
    proveedores de proxy), ``usuario:clave:host:puerto`` y ``host:puerto``. Sin esquema se asume http://. ValueError claro si no encaja."""
    u = (text or "").strip().strip("\"'`<>").strip()
    u = re.sub(r"[\u200b\u200c\u200d\ufeff\u00a0]", "", u)           # caracteres invisibles que se cuelan al copiar/pegar
    if not u:
        raise ValueError("Proxy vacío")
    if re.search(r"\s", u):
        raise ValueError("El proxy no puede llevar espacios ni saltos de línea (pega solo una línea). " + PROXY_FORMAT_HELP)
    scheme = "http"
    m = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*)://(.*)$", u)
    if m:
        scheme, u = m.group(1).lower(), m.group(2)
        if scheme not in PROXY_SCHEMES:
            raise ValueError(f"Esquema «{scheme}» no admitido. Usa http://, https://, socks5:// o socks5h://")
    u = u.rstrip("/")
    user = pw = ""
    if "@" in u:
        cred, _, hostport = u.rpartition("@")
        user, _, pw = cred.partition(":")
        host, sep, port = hostport.rpartition(":")
        if not sep:
            raise ValueError("Falta el puerto. " + PROXY_FORMAT_HELP)
    else:
        parts = u.split(":")
        if len(parts) == 1:
            raise ValueError("Falta el puerto. " + PROXY_FORMAT_HELP)
        if len(parts) == 2:
            host, port = parts
        elif len(parts) >= 4 and _is_port(parts[1]) and _HOSTLIKE.match(parts[0]):      # host:puerto:usuario:clave
            host, port, user, pw = parts[0], parts[1], parts[2], ":".join(parts[3:])
        elif len(parts) == 4 and _is_port(parts[3]) and _HOSTLIKE.match(parts[2]):      # usuario:clave:host:puerto
            user, pw, host, port = parts
        else:
            raise ValueError("No reconozco el formato. " + PROXY_FORMAT_HELP)
    from urllib.parse import unquote

    host = host.strip()
    if not host or not _HOSTLIKE.match(host):
        raise ValueError("El host del proxy no es válido (usa una IP o un nombre como proxy.ejemplo.com). " + PROXY_FORMAT_HELP)
    if not _is_port(port):
        raise ValueError("El puerto debe ser un número entre 1 y 65535 (por ejemplo 8080). " + PROXY_FORMAT_HELP)
    return scheme, unquote(user), unquote(pw), host, port


def proxy_candidates(text: str) -> List[str]:
    """Trozos de lo pegado que pueden ser un proxy: uno por línea o separado por espacios (si se pegó más de uno).
    Se quitan caracteres invisibles, comillas y comas/punto y coma sueltos al final."""
    t = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text or "").replace("\u00a0", " ")
    toks = t.split()
    out = []
    for tok in toks:
        tok = tok.strip("\"'`<>")
        if len(toks) > 1:
            tok = tok.rstrip(",;")
        if tok:
            out.append(tok)
    return out


def first_valid_proxy_text(text: str) -> str:
    """El primer trozo pegado que es un proxy válido (si no hay ninguno, el primero, para que el error hable de él)."""
    cands = proxy_candidates(text)
    for c in cands:
        try:
            _split_one(c)
            return c
        except ValueError:
            continue
    return cands[0] if cands else ""


def split_proxy(text: str) -> Tuple[str, str, str, str, str]:
    """(esquema, usuario, clave, host, puerto), con usuario/clave YA descodificados. Si se pega más de una línea o varios proxies
    separados por espacios, se usa el PRIMERO que sea válido (en vez de dar error). Formatos: ver ``_split_one``."""
    cands = proxy_candidates(text)
    if len(cands) <= 1:
        return _split_one(cands[0] if cands else "")
    first_err: Optional[ValueError] = None
    for c in cands:
        try:
            return _split_one(c)
        except ValueError as e:
            first_err = first_err or e
    assert first_err is not None
    raise first_err


def proxy_form(text: str) -> str:
    """Qué formato se reconoció (para el aviso en pantalla)."""
    u = first_valid_proxy_text(text)
    if "://" in u:
        return _PROXY_FORMS[0]
    return _PROXY_FORMS[1] if "@" in u else (_PROXY_FORMS[2] if u.count(":") >= 3 else _PROXY_FORMS[3])


def parse_proxy(url: str) -> str:
    """Valida UN proxy y lo normaliza a ``esquema://usuario:clave@host:puerto`` (sin esquema => http://; usuario/clave con
    caracteres especiales se codifican). Admite http, https, socks5 y socks5h. Ver ``split_proxy`` para los formatos."""
    from urllib.parse import quote

    scheme, user, pw, host, port = split_proxy(url)
    cred = (quote(user, safe="") + (":" + quote(pw, safe="") if pw else "") + "@") if (user or pw) else ""
    return f"{scheme}://{cred}{host}:{port}"


def describe_proxy_input(text: str) -> Tuple[str, str]:
    """(nivel, frase) para la vista previa mientras se escribe: qué se ha entendido, sin mostrar usuario ni clave."""
    try:
        scheme, user, pw, host, port = split_proxy(text)
    except ValueError as e:
        return "fail", f"✖ {e}"
    auth = "con usuario y clave (ocultos)" if user or pw else "sin usuario"
    extra = " · se guardará como " + scheme + "://" + ("usuario:clave@" if user or pw else "") + f"{host}:{port}" if proxy_form(text) not in (_PROXY_FORMS[0], _PROXY_FORMS[3]) else ""
    several = " · había varias líneas: se usa la primera válida" if len(proxy_candidates(text)) > 1 else ""
    return "ok", f"✔ Entendido: {scheme}:// · {host} · puerto {port} · {auth}{extra}{several}"


def parse_proxy_list(text: str) -> List[str]:
    return [parse_proxy(x) for x in (text or "").replace(";", ",").split(",") if x.strip()]


def build_httpx_client(proxy: Optional[str] = None, timeout: float = 12.0, http2: bool = True):
    import httpx

    kw: Dict[str, Any] = {"timeout": timeout, "headers": {"User-Agent": UA}}
    if proxy:
        kw["proxy"] = proxy
    try:
        return httpx.Client(http2=http2, **kw)
    except ImportError:  # h2 no instalado: sin HTTP/2
        return httpx.Client(**kw)


_client_factory: Callable[..., Any] = build_httpx_client  # sustituible en pruebas


@contextlib.contextmanager
def clob_via_proxy(proxy: Optional[str]) -> Iterator[None]:
    """Mientras dura el bloque, py_clob_client_v2 sale por ``proxy`` (si se da). Restaura el cliente al terminar."""
    if not proxy:
        yield
        return
    from py_clob_client_v2.http_helpers import helpers

    old = helpers._http_client
    helpers._http_client = _client_factory(proxy, 30.0)
    try:
        yield
    finally:
        try:
            helpers._http_client.close()
        except Exception:
            pass
        helpers._http_client = old


def probe_proxy(proxy: Optional[str], clob_host: Optional[str] = None, geoblock_url: str = GEOBLOCK_URL, timeout: float = 12.0) -> Dict[str, Any]:
    """Prueba la salida (por el proxy o directa): IP/país de salida, si Polymarket la bloquea y latencia al CLOB."""
    clob_host = (clob_host or config.load_settings()["CLOB_HOST"]).rstrip("/")
    out: Dict[str, Any] = {"via": "proxy" if proxy else "directo", "ok": False, "ip": None, "country": None, "region": None,
                           "blocked": None, "latency_ms": None, "clob_ok": False, "error": None}
    try:
        c = _client_factory(proxy, timeout)
    except Exception as e:
        out["error"] = f"no se pudo preparar el cliente ({str(e)[:80]}); ¿falta «pip install httpx[socks]» para socks5?"
        return out
    try:
        get = (lambda url: retry_transient(lambda: c.get(url), 3, 1.0)) if proxy else c.get
        try:
            r = get(geoblock_url)
            r.raise_for_status()
            j = r.json()
            out.update(ip=j.get("ip"), country=j.get("country"), region=j.get("region"), blocked=bool(j.get("blocked")))
        except Exception as e:
            out["error_kind"] = net_error_kind(e)
            out["error"] = f"geoblock: {type(e).__name__}: {str(e)[:80]}"
        lat = []
        for _ in range(2):
            t0 = time.time()
            try:
                r = get(f"{clob_host}/time")
                if r.status_code == 200:
                    lat.append((time.time() - t0) * 1000)
                    out["clob_ok"] = True
                else:
                    out["error"] = out["error"] or f"CLOB respondió {r.status_code}"
            except Exception as e:
                out["error_kind"] = out.get("error_kind") or net_error_kind(e)
                out["error"] = out["error"] or f"CLOB: {type(e).__name__}: {str(e)[:80]}"
        if lat:
            out["latency_ms"] = round(min(lat))
        out["ok"] = bool(out["clob_ok"] and out["blocked"] is not None)
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


def describe_probe(r: Dict[str, Any]) -> Tuple[str, str]:
    """(nivel, texto en español) para mostrar el resultado de ``probe_proxy``."""
    via = "Por el proxy" if r["via"] == "proxy" else "Conexión directa"
    if r.get("blocked"):                     # el veredicto del geoblock manda aunque el CLOB haya fallado después
        where = f"{r.get('country') or '?'}{('/' + r['region']) if r.get('region') else ''}"
        lat = f", {r['latency_ms']} ms al CLOB" if r.get("latency_ms") is not None else ""
        return "fail", f"{via}: salida {where} — Polymarket BLOQUEA el trading desde ahí (restringe algunos países){lat}. Debes cumplir sus condiciones de uso y la ley de tu país."
    if r.get("error") and not r.get("ok"):
        kind = r.get("error_kind")
        if kind in ("dns", "reset", "timeout", "proxy"):
            return "fail", f"{via}: no se pudo comprobar — {explain_net_error(kind, RuntimeError(r.get('error') or ''))}"
        return "fail", f"{via}: no se pudo comprobar — {r['error']}"
    where = f"{r.get('country') or '?'}{('/' + r['region']) if r.get('region') else ''}"
    lat = f", {r['latency_ms']} ms al CLOB" if r.get("latency_ms") is not None else ""
    if r["blocked"]:
        return "fail", f"{via}: salida {where} — Polymarket BLOQUEA el trading desde ahí (restringe algunos países){lat}. Debes cumplir sus condiciones de uso y la ley de tu país."
    return "ok", f"{via}: salida {where} — Polymarket permite operar{lat}."


# ── errores de red explicados en llano ─────────────────────────────
def _chain_text(e: BaseException) -> str:
    seen, cur, txt = 0, e, ""
    while cur is not None and seen < 6:
        txt += f" {type(cur).__name__} {cur}"
        cur, seen = (cur.__cause__ or cur.__context__), seen + 1
    return txt


def net_error_kind(e: BaseException) -> str:
    """dns | proxy | reset | timeout | other. Mira también la causa encadenada (httpx envuelve el error del socket)."""
    import socket

    seen, cur = 0, e
    while cur is not None and seen < 6:
        if isinstance(cur, socket.gaierror):
            return "dns"
        cur, seen = (cur.__cause__ or cur.__context__), seen + 1
    low = _chain_text(e).lower()
    if "proxyerror" in low or "407" in low and "proxy" in low:
        return "proxy"
    if ("errno -5" in low or "errno -2" in low or "errno -3" in low or "no address associated" in low or "name or service not known" in low
            or "nodename nor servname" in low or "temporary failure in name resolution" in low or "getaddrinfo" in low):
        return "dns"
    if "errno 104" in low or "connection reset" in low or "connectionreset" in low or "remoteprotocolerror" in low or "server disconnected" in low:
        return "reset"
    if "timeout" in low or "timed out" in low:
        return "timeout"
    return "other"


DNS_HELP = ("Tu proveedor de internet (o el DNS de tu red) no resuelve polymarket.com: es un bloqueo de DNS, habitual en España. "
            "No es un fallo de caro5m ni de tu cuenta.")
DNS_FIX = ("Lo más fácil: añade un proxy en «Proxy» (http://, https:// o socks5h://): el proxy resuelve el DNS por ti. "
           "Alternativa: cambia el DNS del sistema a 1.1.1.1 o 9.9.9.9, o activa DNS sobre HTTPS (DoH) en el sistema/navegador.")
PROXY_REJECT_HELP = "El proxy rechazó la conexión (usuario/contraseña incorrectos, proxy caído o sin permiso)."
PROXY_403_HELP = ("El proxy respondió 403 (rechazó el túnel): suele ser un filtro o límite de tu proveedor de proxy (demasiadas peticiones seguidas, "
                  "dominio o IP no autorizados). Reintenta en unos segundos o pide a tu proveedor que permita polymarket.com.")
NEED_PROXY_MSG = ("Tu proveedor de internet bloquea polymarket.com (DNS) y todavía no hay proxy configurado: "
                  "configura primero el proxy (paso 2) y vuelve a intentarlo.")


class ClobError(RuntimeError):
    """Error del CLOB ya traducido a español; ``original`` conserva la excepción real (para el registro)."""

    def __init__(self, message: str, original: Optional[BaseException] = None):
        super().__init__(message)
        self.original = original


class NeedProxy(RuntimeError):
    """El DNS de polymarket.com está bloqueado y no hay proxy: no tiene sentido seguir hasta configurarlo."""
RESET_HELP = ("La conexión fue reiniciada antes de terminar: tu proveedor puede estar bloqueando Polymarket (bloqueo por SNI/IP) "
              "o hay un cortafuegos en medio.")


def _short_reason(e: Optional[BaseException]) -> str:
    kind = net_error_kind(e) if e is not None else "other"
    if kind == "proxy":
        return "el proxy rechazó el túnel" + (" (403)" if e is not None and "403" in _chain_text(e) else "")
    return explain_net_error(kind, e)[:80]


def explain_net_error(kind: str, e: Optional[BaseException] = None) -> str:
    if kind == "dns":
        return DNS_HELP
    if kind == "reset":
        return RESET_HELP
    if kind == "timeout":
        return "La conexión tardó demasiado (red lenta, bloqueada o proxy caído)."
    if kind == "proxy":
        return PROXY_REJECT_HELP if e is None or "403" not in _chain_text(e) else PROXY_403_HELP
    return _clean_err(e)[:100] if e is not None else "error de red"


def network_gate(proxy: Optional[str], host: Optional[str] = None) -> None:
    """Sin proxy y con el DNS de Polymarket bloqueado no se intenta nada de red: se avisa de que falta el proxy.

    Con proxy configurado no se comprueba el DNS local (lo resuelve el proxy)."""
    if proxy:
        return
    host = host or urlparse(config.load_settings()["CLOB_HOST"]).hostname or "clob.polymarket.com"
    try:
        _resolve(host, 443)
    except Exception as e:
        if net_error_kind(e) == "dns":
            raise NeedProxy(NEED_PROXY_MSG) from e


_retry_sleep: Callable[[float], None] = time.sleep  # sustituible en pruebas


def retry_transient(fn: Callable[[], Any], tries: int = 3, delay: float = 1.5) -> Any:
    """Reintenta ``fn`` si falla por algo pasajero del proxy (túnel rechazado, corte, tiempo agotado). Los proxies rotativos
    rechazan a veces una conexión suelta; un error HTTP del propio Polymarket (400/401/403…) NO se reintenta."""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if i == tries - 1 or isinstance(e, NeedProxy) or net_error_kind(e) not in ("proxy", "reset", "timeout"):
                raise
            setuplog.event(f"fallo pasajero ({net_error_kind(e)}); reintento {i + 2}/{tries}", "WARN")
            _retry_sleep(delay * (i + 1))


def explain_clob_error(e: BaseException, proxy: Optional[str] = None) -> str:
    """Traduce un error del cliente CLOB (PolyApiException «Request exception!», httpx, HTTP 4xx…) a una frase clara en español."""
    if isinstance(e, (NeedProxy, ClobError)):
        return str(e)
    kind = net_error_kind(e)
    status = getattr(e, "status_code", None)
    body = str(getattr(e, "error_msg", "") or "")
    low = body.lower()
    via = "por el proxy " if proxy else ""
    if kind == "dns":
        return (f"No se pudo resolver el nombre de Polymarket {via}(DNS). " + ("Revisa que el proxy funcione (botón «Probar»)." if proxy else NEED_PROXY_MSG))
    if kind == "proxy":
        return explain_net_error("proxy", e) + " Usa «Probar» en la pantalla del proxy."
    if kind == "reset":
        return ("La conexión con Polymarket se cortó " + via + "(muchos proveedores la cortan al bloquear polymarket.com). "
                + ("Prueba otro proxy." if proxy else "Configura un proxy y vuelve a intentarlo."))
    if kind == "timeout":
        return f"Polymarket tardó demasiado en responder {via}(red lenta, bloqueada o proxy caído)."
    if isinstance(status, int):
        if status in (403, 451) or re.search(r"region|restrict|geoblock|blocked|not available in your", low):
            return ("Polymarket restringe el trading desde algunos países (restricción de región). "
                    "Debes cumplir sus condiciones de uso y la ley de tu país; caro5m no evita esa restricción.")
        if status == 401:
            return "Polymarket rechazó la firma: revisa que la clave y el tipo de cartera sean correctos y que el reloj del equipo esté en hora."
        if status == 400:
            return ("Polymarket no aceptó la petición (400): suele ser una cuenta sin registrar o un tipo de cartera equivocado. "
                    "Entra una vez en polymarket.com con esa cartera y revisa el tipo elegido.")
        if status == 429:
            return "Demasiadas peticiones (429): espera un minuto y vuelve a intentarlo."
        if status >= 500:
            return f"Polymarket tiene un problema ahora mismo (error {status}). Inténtalo de nuevo en unos minutos."
    if status is None and "request exception" in low:
        return "No se pudo conectar con Polymarket " + via + "(sin respuesta). " + ("Prueba el proxy con «Probar»." if proxy else "Prueba con un proxy.")
    return _clean_err(e)


def proxy_advice(proxy: str, scan: bool = False) -> str:
    """Consejo breve al guardar un proxy (vacío si no hay nada que decir).

    Órdenes y claves usan httpx: con http://, https://, socks5:// y socks5h:// el nombre de polymarket.com lo resuelve el PROXY
    (CONNECT con el nombre / SOCKS5 con tipo «dominio»). Solo las lecturas por «requests» (CARO5M_PROXY_SCAN=1) distinguen:
    socks5:// resuelve el DNS en tu equipo y socks5h:// en el proxy."""
    if scan and proxy and proxy.lower().startswith("socks5://"):
        return ("Consejo: con «leer precios por el proxy» activado, socks5:// resuelve el DNS en tu equipo para esas lecturas; "
                "usa socks5h:// para que lo resuelva el proxy.")
    return ""


# ── cliente CLOB + claves ──────────────────────────────────────────
def _make_client(private_key: str, sig_type: int, funder: str, host: str, chain_id: int, creds: Any = None):
    from py_clob_client_v2.client import ClobClient

    c = ClobClient(host, chain_id=chain_id, key=private_key, signature_type=int(sig_type), funder=funder or None)
    if creds is not None:
        c.set_api_creds(creds)
    return c


_make_client_fn: Callable[..., Any] = _make_client  # sustituible en pruebas


def generate_credentials(private_key: str, sig_type: int, funder: str, *, proxy: Optional[str] = None, nonce: Optional[int] = None,
                         host: Optional[str] = None, chain_id: Optional[int] = None) -> Any:
    """Deriva (o crea) las credenciales L2 del CLOB con una firma L1 de la clave. ``nonce`` distinto = claves nuevas."""
    s = config.load_settings()
    host, chain_id = host or s["CLOB_HOST"], chain_id or s["CHAIN_ID"]
    private_key = wizard.validate_private_key(private_key)
    setuplog.event(f"claves CLOB: generando (firma={sig_type}, proxy={'sí' if proxy else 'no'}, nonce={nonce})")
    network_gate(proxy, urlparse(host).hostname)
    try:
        with clob_via_proxy(proxy):
            c = _make_client_fn(private_key, sig_type, funder, host, chain_id)
            call = (lambda: c.create_or_derive_api_key(nonce=nonce)) if nonce is not None else c.create_or_derive_api_key
            creds = retry_transient(call) if proxy else call()
    except NeedProxy:
        raise
    except Exception as e:
        setuplog.error("claves CLOB (create_or_derive_api_key)", e, f"firma={sig_type} proxy={'sí' if proxy else 'no'}")
        raise ClobError(explain_clob_error(e, proxy), e) from e
    if not getattr(creds, "api_key", None):
        raise RuntimeError("Polymarket no devolvió credenciales (¿cuenta sin registrar? Conecta la cartera una vez en polymarket.com)")
    return creds


def validate_credentials(private_key: str, sig_type: int, funder: str, creds: Any, *, proxy: Optional[str] = None,
                         host: Optional[str] = None, chain_id: Optional[int] = None) -> Dict[str, Any]:
    """Lectura autenticada (get_api_keys + balance/allowance). No envía nada. Devuelve un resumen sin secretos."""
    s = config.load_settings()
    host, chain_id = host or s["CLOB_HOST"], chain_id or s["CHAIN_ID"]
    out: Dict[str, Any] = {"ok": False, "keys": None, "balance_usdc": None, "allowance_ok": None, "error": None, "need_proxy": False}
    try:
        from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

        network_gate(proxy, urlparse(host).hostname)
        with clob_via_proxy(proxy):
            c = _make_client_fn(wizard.validate_private_key(private_key), sig_type, funder, host, chain_id, creds)
            rt = (lambda f: retry_transient(f)) if proxy else (lambda f: f())
            keys = rt(c.get_api_keys)
            out["keys"] = len(keys.get("apiKeys", keys)) if isinstance(keys, dict) else (len(keys) if isinstance(keys, list) else None)
            ba = rt(lambda: c.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=int(sig_type))))
        if isinstance(ba, dict):
            if ba.get("balance") is not None:
                out["balance_usdc"] = int(ba["balance"]) / 10 ** 6
            al = ba.get("allowances")
            if isinstance(al, dict):
                out["allowance_ok"] = any(int(v) > 0 for v in al.values())
        out["ok"] = True
    except Exception as e:
        out["error"] = explain_clob_error(e, proxy)
        out["need_proxy"] = isinstance(e, NeedProxy)
        if not isinstance(e, NeedProxy):
            setuplog.error("validar claves CLOB", e)
    return out


def refresh_allowance_cache(private_key: str, sig_type: int, funder: str, creds: Any, *, proxy: Optional[str] = None) -> Dict[str, Any]:
    """Pide al CLOB que vuelva a leer saldo/allowance de la cadena (gratis; NO aprueba nada)."""
    from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams

    s = config.load_settings()
    network_gate(proxy, urlparse(s["CLOB_HOST"]).hostname)
    try:
        with clob_via_proxy(proxy):
            c = _make_client_fn(wizard.validate_private_key(private_key), sig_type, funder, s["CLOB_HOST"], s["CHAIN_ID"], creds)
            call = lambda: c.update_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=int(sig_type)))  # noqa: E731
            return retry_transient(call) if proxy else call()
    except NeedProxy:
        raise
    except Exception as e:
        setuplog.error("refrescar saldo/allowance", e)
        raise ClobError(explain_clob_error(e, proxy), e) from e


def _clean_err(e: Exception) -> str:
    """Mensaje de error sin posibles secretos (recorta y quita cualquier hex largo)."""
    msg = re.sub(r"0x[0-9a-fA-F]{40,}", "0x…", f"{type(e).__name__}: {e}")
    return msg[:160]


def next_nonce(env: Dict[str, str]) -> int:
    try:
        return int(env.get("POLY_CLOB_NONCE") or 0) + 1
    except ValueError:
        return 1


def save_credentials(creds: Any, nonce: Optional[int] = None) -> None:
    upd = {"POLY_CLOB_API_KEY": creds.api_key, "POLY_CLOB_API_SECRET": creds.api_secret, "POLY_CLOB_API_PASSPHRASE": creds.api_passphrase}
    if nonce is not None:
        upd["POLY_CLOB_NONCE"] = str(int(nonce))
    wizard.write_env(upd)


def stored_credentials(env: Dict[str, str]) -> Any:
    from py_clob_client_v2.clob_types import ApiCreds

    k, s, p = env.get("POLY_CLOB_API_KEY"), env.get("POLY_CLOB_API_SECRET"), env.get("POLY_CLOB_API_PASSPHRASE")
    return ApiCreds(api_key=k, api_secret=s, api_passphrase=p) if (k and s and p) else None


def read_env() -> Dict[str, str]:
    """Lee el .env (solo en las pantallas de configuración; nunca se imprime ni se registra)."""
    try:
        return config.parse_env_text(config.env_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


# ── cartera: validar campo a campo, detectar incoherencias y proponer la dirección de fondos ──
def lookup_profile_address(signer: str, *, proxy: Optional[str] = None, timeout: float = 8.0) -> Dict[str, Any]:
    """Pregunta al perfil PÚBLICO de Polymarket (gamma-api /public-profile?address=…) cuál es la cuenta (Safe/proxy)
    asociada a la dirección de esa clave. No envía la clave, solo la dirección pública. {"proxy_wallet", "name", "error"}."""
    out: Dict[str, Any] = {"proxy_wallet": None, "name": None, "error": None}
    try:
        c = _client_factory(proxy, timeout, False)
    except Exception as e:
        out["error"] = _clean_err(e)
        return out
    try:
        r = c.get(PROFILE_URL, params={"address": signer})
        if r.status_code == 404:
            return out  # esa clave aún no tiene perfil en Polymarket
        r.raise_for_status()
        j = r.json()
        pw = j.get("proxyWallet")
        if isinstance(pw, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", pw):
            out["proxy_wallet"] = pw
        out["name"] = j.get("name") or j.get("pseudonym")
    except Exception as e:
        out["error"] = explain_net_error(net_error_kind(e), e)
        setuplog.error("buscar mi dirección (perfil público)", e)
    finally:
        try:
            c.close()
        except Exception:
            pass
    return out


_lookup_fn: Callable[..., Dict[str, Any]] = lookup_profile_address  # sustituible en pruebas


@dataclass
class WalletPlan:
    """Resultado de revisar lo que el usuario escribió. Nada se guarda hasta ``save_plan``; cada campo se valida por separado."""
    choice: str                      # browser | email | own
    wallet: str                      # safe | magic | eoa (tipo técnico)
    key: str = ""                    # clave nueva y válida ("" = conservar la guardada / no hay)
    funder: str = ""                 # dirección de fondos final ("" = sin definir)
    signer: Optional[str] = None     # dirección derivada de la clave (efectiva)
    errors: Dict[str, str] = field(default_factory=dict)   # {"key": …, "funder": …}
    warnings: List[str] = field(default_factory=list)
    conflict: Optional[Dict[str, Any]] = None              # {"kind", "message", "options": [(código, texto)]}
    suggestion: Optional[str] = None                        # cuenta de Polymarket asociada a la clave (perfil público)
    funder_from_profile: bool = False
    typed_funder: str = ""          # dirección escrita por el usuario (ya validada)
    has_key: bool = False            # hay clave efectiva (nueva o guardada)

    @property
    def ok(self) -> bool:
        return not self.errors and self.conflict is None


def evaluate_wallet(choice: str, key_text: str = "", funder_text: str = "", *, stored: Optional[Dict[str, str]] = None,
                    lookup: bool = True, proxy: Optional[str] = None) -> WalletPlan:
    """Revisa clave y dirección. Vacío = conservar/saltar (nunca es un error ni descarta el otro campo)."""
    if choice not in WALLET_CHOICES:
        raise ValueError("Opción de cartera desconocida")
    stored = stored if stored is not None else read_env()
    wallet = WALLET_CHOICES[choice]["wallet"]
    plan = WalletPlan(choice=choice, wallet=wallet)
    key_text, funder_text = (key_text or "").strip(), (funder_text or "").strip()
    stored_key = stored.get("POLY_PRIVATE_KEY", "")
    stored_funder = stored.get("POLY_SAFE_ADDRESS") or stored.get("POLY_FUNDER_ADDRESS") or ""
    if key_text:
        try:
            plan.key = wizard.validate_private_key(key_text)
        except ValueError as e:
            plan.errors["key"] = str(e)
    eff_key = plan.key or stored_key
    plan.has_key = bool(eff_key)
    plan.signer = wizard.signer_address(eff_key) if eff_key else None
    typed_funder = ""
    if funder_text:
        try:
            typed_funder = wizard.validate_address(funder_text)
        except ValueError as e:
            plan.errors["funder"] = str(e)
    if lookup and plan.signer:
        r = _lookup_fn(plan.signer, proxy=proxy)
        plan.suggestion = r.get("proxy_wallet")
        if r.get("error"):
            plan.warnings.append(f"No pude consultar tu perfil público de Polymarket ({r['error'][:90]}); sigue con la dirección manual.")
    plan.typed_funder = typed_funder
    sg = (plan.suggestion or "").lower()
    signer_l = (plan.signer or "").lower()
    if wallet == "eoa":
        plan.funder = plan.signer or typed_funder or stored_funder
        if typed_funder and signer_l and typed_funder.lower() != signer_l:
            plan.conflict = {"kind": "eoa_funder_differs", "options": [("switch_browser", "Es de cartera del navegador / Safe: usar esa dirección"), ("keep", "Mantener EOA (usar la dirección de mi clave)")],
                             "message": (f"Con «cartera propia (EOA)» el dinero está en la dirección de tu clave ({wizard.mask_address(plan.signer or '')}), "
                                         f"pero escribiste otra dirección ({wizard.mask_address(typed_funder)}). ¿Tu cuenta es de la web de Polymarket?")}
        elif sg:
            plan.conflict = {"kind": "eoa_has_profile", "options": [("switch_browser", "Entro con MetaMask/Rabby…: usar esa cuenta"), ("switch_email", "Entro con email/Google: usar esa cuenta"), ("keep", "Sigo con EOA")],
                             "message": (f"Polymarket tiene una cuenta asociada a esa clave ({wizard.mask_address(plan.suggestion or '')}). "
                                         "Si usas polymarket.com con ella, EOA no es lo adecuado.")}
        return plan
    # Safe / Magic
    funder = typed_funder or ("" if "funder" in plan.errors else stored_funder)
    if not funder and sg:
        funder, plan.funder_from_profile = plan.suggestion or "", True
    plan.funder = funder
    if funder and signer_l and funder.lower() == signer_l:
        opts = [("switch_eoa", "Es mi cartera propia sin la web (EOA)"), ("keep", f"Mantener {WALLET_CHOICES[choice]['short']} con esa dirección")]
        if sg:
            opts.insert(0, ("use_suggested", f"Usar la cuenta que Polymarket asocia a mi clave ({wizard.mask_address(plan.suggestion or '')})"))
        plan.conflict = {"kind": "funder_is_signer", "options": opts,
                         "message": ("La dirección de fondos es la MISMA que la de tu clave. Con una cartera del navegador / email, los fondos están en una cuenta distinta "
                                     "(tu perfil de Polymarket), no en la dirección de la cartera." + (f" Polymarket indica que tu cuenta es {wizard.mask_address(plan.suggestion or '')}." if sg else ""))}
    elif funder and sg and funder.lower() != sg and not plan.funder_from_profile:
        plan.conflict = {"kind": "funder_differs_from_profile", "options": [("use_suggested", f"Usar la de Polymarket ({wizard.mask_address(plan.suggestion or '')})"), ("keep", "Mantener la que escribí")],
                         "message": (f"La dirección de fondos que escribiste ({wizard.mask_address(funder)}) no coincide con la que Polymarket asocia a tu clave "
                                     f"({wizard.mask_address(plan.suggestion or '')}).")}
    return plan


def resolve_conflict(plan: WalletPlan, decision: str) -> WalletPlan:
    """Aplica la decisión del usuario a un conflicto detectado por ``evaluate_wallet`` y lo da por resuelto."""
    c = plan.conflict
    if not c:
        return plan
    codes = [o[0] for o in c["options"]]
    if decision not in codes:
        raise ValueError("Decisión no válida")
    if decision == "use_suggested" and plan.suggestion:
        plan.funder = plan.suggestion
    elif decision == "switch_eoa":
        plan.choice, plan.wallet, plan.funder = "own", "eoa", plan.signer or plan.funder
    elif decision == "switch_browser":
        plan.choice, plan.wallet = "browser", "safe"
        if c["kind"] == "eoa_has_profile" and plan.suggestion:
            plan.funder = plan.suggestion
        else:  # eoa_funder_differs: la dirección que escribió
            plan.funder = plan.typed_funder or plan.suggestion or plan.funder
    elif decision == "switch_email":
        plan.choice, plan.wallet = "email", "magic"
        plan.funder = plan.suggestion or plan.funder
    plan.conflict = None
    return plan


def save_plan(plan: WalletPlan) -> Dict[str, Any]:
    """Guarda lo válido (clave y fondos en .env 0600, tipo de firma en settings.env). Los campos con error se omiten; no se pierde nada más."""
    if plan.conflict:
        raise ValueError("Hay una incoherencia sin resolver")
    upd: Dict[str, str] = {}
    if plan.key:
        upd["POLY_PRIVATE_KEY"] = plan.key
    if plan.funder and "funder" not in plan.errors:
        upd["POLY_SAFE_ADDRESS"] = plan.funder
    if upd:
        wizard.write_env(upd)
    config.save_settings({"POLY_SIGNATURE_TYPE": str(WALLET_CHOICES[plan.choice]["sig"])})
    return {"saved_key": bool(plan.key), "saved_funder": "POLY_SAFE_ADDRESS" in upd, "wallet": plan.wallet, "choice": plan.choice,
            "funder": plan.funder, "signer": plan.signer, "errors": dict(plan.errors)}


def setup_incomplete(env: Optional[Dict[str, str]] = None) -> bool:
    """True si aún falta la clave o la dirección de fondos (para abrir la configuración guiada en el primer arranque)."""
    env = env if env is not None else read_env()
    return not (env.get("POLY_PRIVATE_KEY") and (env.get("POLY_SAFE_ADDRESS") or env.get("POLY_FUNDER_ADDRESS")))


def _skip_marker():
    return config.data_dir() / ".setup_skipped"


def setup_skipped() -> bool:
    return _skip_marker().exists()


def mark_setup_skipped() -> None:
    try:
        config.data_dir().mkdir(parents=True, exist_ok=True)
        _skip_marker().write_text("1", encoding="utf-8")
    except OSError:
        pass


def next_step(env: Dict[str, str], sig: int) -> str:
    """Qué falta, en una frase (para la cabecera de la pantalla de cuenta)."""
    if not env.get("POLY_PRIVATE_KEY"):
        return "elige cómo entras en Polymarket y pega tu clave privada (paso 1)."
    if wallet_key(sig) != "eoa" and not (env.get("POLY_SAFE_ADDRESS") or env.get("POLY_FUNDER_ADDRESS")):
        return "falta la dirección de tu cuenta de Polymarket: pulsa «Buscar mi dirección» (paso 1)."
    if stored_credentials(env) is None:
        return "genera tus claves CLOB (paso 3, o «Guardar y continuar»)."
    return "ejecuta las comprobaciones (paso 4) y, si todo está ✔, activa el modo real con [M]."


# ── comprobaciones previas ─────────────────────────────────────────
@dataclass
class Check:
    id: str
    level: str  # ok | warn | fail | info
    title: str
    detail: str = ""
    fix: str = ""


ICON = {"ok": "✔", "warn": "⚠", "fail": "✖", "info": "•"}


def _rpc_call(rpc: str, method: str, params: list, timeout: float = 8.0) -> Any:
    c = _client_factory(None, timeout, False)
    try:
        r = c.post(rpc, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        r.raise_for_status()
        j = r.json()
        if "error" in j:
            raise RuntimeError(str(j["error"])[:100])
        return j["result"]
    finally:
        c.close()


_rpc: Callable[..., Any] = _rpc_call  # sustituible en pruebas


def _clob_time(clob_host: str, proxy: Optional[str]) -> Tuple[float, float, float]:
    """(hora del servidor, hora local en el instante de la respuesta, precisión en s)."""
    c = _client_factory(proxy, 10.0)
    try:
        t0 = time.time()
        r = c.get(f"{clob_host}/time")
        t1 = time.time()
    finally:
        c.close()
    return float(r.text.strip().strip('"')), (t0 + t1) / 2, 0.5


def _http_date_time(url: str) -> Tuple[float, float, float]:
    """Hora de un servidor neutral por la cabecera HTTP «Date» (precisión de 1 s)."""
    from email.utils import parsedate_to_datetime

    c = _client_factory(None, 8.0, False)
    try:
        t0 = time.time()
        r = c.head(url, follow_redirects=True)
        t1 = time.time()
    finally:
        c.close()
    d = r.headers.get("date")
    if not d:
        raise RuntimeError("sin cabecera Date")
    return parsedate_to_datetime(d).timestamp() + 0.5, (t0 + t1) / 2, 1.0


def _ntp_time(host: str) -> Tuple[float, float, float]:
    """Consulta SNTP simple por UDP 123 (precisión ~0,1 s). Puede estar bloqueada en redes restringidas."""
    import socket
    import struct

    t0 = time.time()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
        sk.settimeout(4.0)
        sk.sendto(b"\x1b" + 47 * b"\0", (host, 123))
        data, _ = sk.recvfrom(512)
    t1 = time.time()
    secs, frac = struct.unpack("!II", data[40:48])
    return secs - 2208988800 + frac / 2 ** 32, (t0 + t1) / 2, 0.3


CLOCK_FALLBACKS = (
    ("cloudflare.com", lambda: _http_date_time("https://www.cloudflare.com")),
    ("google.com", lambda: _http_date_time("https://www.google.com")),
    ("NTP pool.ntp.org", lambda: _ntp_time("pool.ntp.org")),
    ("NTP time.cloudflare.com", lambda: _ntp_time("time.cloudflare.com")),
)


def check_clock(clob_host: str, proxy: Optional[str]) -> Check:
    """Desfase del reloj. Primero con Polymarket; si no responde (DNS/reset/proxy), con otra fuente de hora neutral."""
    first_err: Optional[BaseException] = None
    sources: List[Tuple[str, Callable[[], Tuple[float, float, float]]]] = [("Polymarket", lambda: _clob_time(clob_host, proxy))]
    sources += list(CLOCK_FALLBACKS)
    for name, fn in sources:
        try:
            server, local, prec = fn()
        except Exception as e:
            first_err = first_err or e
            continue
        skew = local - server
        via = "" if name == "Polymarket" else f" (medido con {name}; Polymarket no respondía: {_short_reason(first_err)})"
        warn_at = 2.0 + prec  # con fuentes de 1 s de precisión no se alarma por ±1 s
        if abs(skew) > 30:
            return Check("clock", "fail", "Reloj del sistema", f"desfase de {skew:+.1f} s{via}: las firmas serán rechazadas",
                         "Sincroniza la hora (timedatectl set-ntp true / ajustes de fecha y hora automática).")
        if abs(skew) > warn_at:
            return Check("clock", "warn", "Reloj del sistema", f"desfase de {skew:+.1f} s{via} (conviene < 2 s)", "Activa la hora automática (NTP): timedatectl set-ntp true")
        return Check("clock", "ok", "Reloj del sistema", f"desfase {skew:+.1f} s{via}")
    return Check("clock", "warn", "Reloj del sistema", f"no se pudo medir con ninguna fuente ({explain_net_error(net_error_kind(first_err), first_err)[:110]})",
                 "Comprueba que la hora automática está activada en tu sistema.")


def check_dns(host: str = "polymarket.com", proxy: Optional[str] = None, scan: bool = False) -> Check:
    """¿Resuelve el DNS de tu red polymarket.com? Si no, comprueba con DNS sobre HTTPS para confirmar que es un bloqueo de DNS."""
    try:
        infos = _resolve(host, 443)
        return Check("dns", "ok", f"DNS de {host}", f"resuelve ({len({i[4][0] for i in infos})} dirección/es)")
    except Exception as e:
        kind = net_error_kind(e)
    # órdenes y claves (httpx): con cualquier proxy el nombre lo resuelve el proxy; solo las lecturas por «requests» distinguen socks5/socks5h
    local_scan_dns = bool(proxy) and scan and proxy.lower().startswith("socks5://")
    doh_ip = None
    try:
        c = _client_factory(None, 8.0, False)
        try:
            r = c.get(DOH_URL, params={"name": host, "type": "A"}, headers={"accept": "application/dns-json"})
            doh_ip = next((a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1), None)
        finally:
            c.close()
    except Exception:
        doh_ip = None
    msg = DNS_HELP if kind == "dns" else f"no se pudo resolver {host} ({explain_net_error(kind)})"
    if doh_ip and kind == "dns":
        msg += " Con otro DNS (DoH) sí resuelve: confirmado."
    if proxy:
        if not local_scan_dns:
            return Check("dns", "info", f"DNS de {host}", msg + " Con tu proxy no importa: lo resuelve el proxy.")
        return Check("dns", "warn", f"DNS de {host}", msg + " Tu proxy es socks5:// y las lecturas de precios por el proxy resolverían el DNS en tu equipo.",
                     "Usa socks5h://… (o desactiva «leer precios por el proxy»).")
    return Check("dns", "fail", f"DNS de {host}", msg, DNS_FIX)


_resolve: Callable[..., Any] = socket.getaddrinfo  # sustituible en pruebas


def check_geo_and_clob(clob_host: str, proxy: Optional[str]) -> List[Check]:
    out: List[Check] = []
    direct = probe_proxy(None, clob_host)
    lvl, txt = describe_probe(direct)
    if proxy:
        out.append(Check("geo_direct", "info", "IP del servidor (sin proxy)", txt))
        p = probe_proxy(proxy, clob_host)
        lvl, txt = describe_probe(p)
        if lvl == "ok":
            fix = ""
        elif p.get("blocked"):
            fix = "Polymarket restringe el trading en algunos países. Debes cumplir sus condiciones de uso y la ley de tu país."
        else:
            fix = "Revisa el proxy (usuario, contraseña, que responda) en Configuración de cuenta → Proxy."
        out.append(Check("geo_proxy", lvl, "Proxy configurado", txt, fix))
    else:
        if lvl == "ok":
            fix = ""
        elif direct.get("blocked"):
            fix = "Polymarket restringe el trading en algunos países. Debes cumplir sus condiciones de uso y la ley de tu país."
        elif direct.get("error_kind") in ("dns", "reset") and not direct.get("ok"):
            fix = DNS_FIX if direct["error_kind"] == "dns" else "Si tu red u operador corta la salida, un proxy HTTP/SOCKS (http://, https:// o socks5h://) es opcional."
        else:
            fix = "Revisa la conexión de red. Si tu operador bloquea el DNS, un proxy HTTP/SOCKS es opcional."
        out.append(Check("geo_direct", lvl, "Acceso a Polymarket (sin proxy)", txt, fix))
    return out


def check_wallet_funds(address: str, rpcs: List[str], chain_id: int, bet_size: float, wallet: str) -> List[Check]:
    from . import checks

    w = checks.check_wallet(address, rpcs, chain_id, bet_size)
    if w.get("error"):
        return [Check("funds", "warn", "Saldo y allowance", f"no se pudo leer: {w['error']}")]
    need = bet_size * 1.03
    bal = w["balance_usdc"] or 0.0
    out = [Check("funds", "ok" if bal >= need else "fail", "Saldo USDC", f"{bal:,.2f} (mínimo {need:,.2f} = apuesta + 3 % de margen para comisiones)",
                 "" if bal >= need else "Deposita USDC en esa dirección desde polymarket.com.")]
    if w["allowance_ok"]:
        out.append(Check("allow", "ok", "Allowance del exchange", f"{w['allowance_usdc']:,.2f} USDC aprobados"))
    else:
        fix = ("Usa «Aprobar allowances» (EOA) en Configuración de cuenta." if wallet == "eoa"
               else "Haz un primer depósito/operación en polymarket.com: Polymarket aprueba los permisos por ti.")
        out.append(Check("allow", "fail", "Allowance del exchange", "sin aprobar: las órdenes darían «not enough balance / allowance»", fix))
    return out


def check_gas(signer: str, rpcs: List[str]) -> Check:
    for rpc in rpcs:
        try:
            pol = int(_rpc(rpc, "eth_getBalance", [signer, "latest"]), 16) / 1e18
            return Check("gas", "ok" if pol >= 0.05 else "warn", "POL para gas (EOA)", f"{pol:.4f} POL",
                         "" if pol >= 0.05 else "Necesitas ~0,05 POL en tu dirección para aprobar permisos.")
        except Exception:
            continue
    return Check("gas", "warn", "POL para gas (EOA)", "no se pudo leer el saldo")


def check_rpcs(rpcs: List[str]) -> Check:
    ok, bad = 0, []
    for rpc in rpcs:
        try:
            t0 = time.time()
            _rpc(rpc, "eth_blockNumber", [])
            ok += 1
        except Exception:
            bad.append(urlparse(rpc).hostname or rpc)
    if ok == 0:
        return Check("rpc", "fail", "RPC de Polygon", "ninguno responde", "Revisa POLYGON_RPCS en Ajustes.")
    if bad:
        return Check("rpc", "warn", "RPC de Polygon", f"{ok}/{len(rpcs)} responden (fallan: {', '.join(bad)})", "Quita o cambia los que fallan.")
    return Check("rpc", "ok", "RPC de Polygon", f"{ok}/{len(rpcs)} responden")


def check_binance(host: str) -> Check:
    try:
        c = _client_factory(None, 8.0, False)
        try:
            r = c.get(host.rstrip("/") + "/api/v3/ping")
        finally:
            c.close()
        if r.status_code in (451, 403):
            return Check("binance", "fail", "Binance", f"{host} responde {r.status_code} (bloqueo regional)",
                         "Cambia BINANCE_HOST en Ajustes (p. ej. https://api.binance.us).")
        r.raise_for_status()
        return Check("binance", "ok", "Binance", f"{host} accesible")
    except Exception as e:
        return Check("binance", "fail", "Binance", f"{host} no accesible ({_clean_err(e)[:60]})", "Revisa BINANCE_HOST o tu red.")


def check_local() -> List[Check]:
    import os

    out = []
    shm = config.shm_dir()
    try:
        shm.mkdir(parents=True, exist_ok=True)
        t = shm / ".caro5m_write_test"
        t.write_text("x", encoding="utf-8")
        t.unlink()
        out.append(Check("shm", "ok", "Carpeta compartida (SHM)", str(shm)))
    except Exception as e:
        out.append(Check("shm", "fail", "Carpeta compartida (SHM)", f"{shm} no escribible ({type(e).__name__})", "Define CARO5M_SHM_DIR en una carpeta escribible."))
    try:
        config.data_dir().mkdir(parents=True, exist_ok=True)
        t = config.data_dir() / ".w"
        t.write_text("x", encoding="utf-8")
        t.unlink()
        out.append(Check("data", "ok", "Carpeta de datos", str(config.data_dir())))
    except Exception as e:
        out.append(Check("data", "fail", "Carpeta de datos", f"no escribible ({type(e).__name__})"))
    ef = config.env_file()
    from . import compat

    if ef.exists() and not compat.permissions_meaningful():
        out.append(Check("envperm", "info", "Permisos del .env", "Windows: sin permisos POSIX; protégelo dejando el proyecto dentro de tu carpeta de usuario"))
    elif ef.exists():
        mode = os.stat(ef).st_mode & 0o777
        out.append(Check("envperm", "ok" if mode == 0o600 else "warn", "Permisos del .env", oct(mode), "" if mode == 0o600 else f"chmod 600 {ef}"))
    return out


def run_preflight(*, with_network: bool = True) -> List[Check]:
    """Todas las comprobaciones (y las deja anotadas en el registro de configuración)."""
    out = _run_preflight(with_network=with_network)
    setuplog.event(f"comprobaciones: {'con' if with_network else 'sin'} red")
    for c in out:
        setuplog.record_check(c)
    return out


def _run_preflight(*, with_network: bool = True) -> List[Check]:
    """Todas las comprobaciones de solo lectura. Usa .env + settings.env actuales."""
    s = config.load_settings()
    env = read_env()
    sig = int(s["POLY_SIGNATURE_TYPE"])
    wallet = wallet_key(sig)
    funder = env.get("POLY_SAFE_ADDRESS") or env.get("POLY_FUNDER_ADDRESS") or ""
    pk = env.get("POLY_PRIVATE_KEY") or ""
    proxy_list = parse_proxy_list(env.get("POLYMARKET_PROXY_LIST", "")) if env.get("POLYMARKET_PROXY_LIST") else []
    proxy = proxy_list[0] if proxy_list else None
    out: List[Check] = list(check_local())
    out.append(Check("wallet", "ok" if (pk and funder) else "warn", "Cartera",
                     f"{WALLET_CHOICES[WALLET_TO_CHOICE[wallet]]['short']} · clave {'configurada' if pk else 'FALTA'} · funder {wizard.mask_address(funder) if funder else 'FALTA'}",
                     "" if (pk and funder) else "Configúralos en «Configuración de cuenta»."))
    if pk and funder and wallet != "eoa":
        signer0 = wizard.signer_address(pk)
        if signer0 and signer0.lower() == funder.lower():
            out.append(Check("funder_is_signer", "warn", "Dirección de fondos",
                             "es la MISMA que la de tu clave; con cartera del navegador / email los fondos suelen estar en otra cuenta (tu perfil de Polymarket)",
                             "Abre «Configuración de cuenta» → «Buscar mi dirección», o elige «cartera propia (EOA)» si de verdad no usas la web."))
    if pk and funder and wallet == "eoa":
        signer = wizard.signer_address(pk)
        if signer and signer.lower() != funder.lower():
            out.append(Check("eoa_match", "fail", "EOA: dirección de fondos", "no coincide con la dirección de tu clave",
                             "Para EOA el funder debe ser la dirección de la clave; o cambia el tipo de cartera."))
    if not with_network:
        return out
    out.append(check_dns("polymarket.com", proxy, bool(s["CARO5M_PROXY_SCAN"])))
    out.append(check_clock(s["CLOB_HOST"], proxy))
    out += check_geo_and_clob(s["CLOB_HOST"], proxy)
    out.append(check_rpcs(s["POLYGON_RPCS"]))
    out.append(check_binance(s["BINANCE_HOST"]))
    creds = stored_credentials(env)
    if not pk:
        out.append(Check("creds", "info", "Claves CLOB", "sin clave privada: no se pueden generar"))
    elif creds is None:
        out.append(Check("creds", "warn", "Claves CLOB", "sin generar (el bot las derivará al pasar a real, pero conviene generarlas aquí)",
                         "Pulsa «Generar claves CLOB»."))
    else:
        v = validate_credentials(pk, sig, funder, creds, proxy=proxy)
        out.append(Check("creds", "ok" if v["ok"] else "fail", "Claves CLOB", f"{mask_secret(creds.api_key)} · " + ("válidas" if v["ok"] else f"rechazadas: {v['error']}"),
                         "" if v["ok"] else "Regenera las claves (nonce nuevo)."))
    if funder:
        out += check_wallet_funds(funder, s["POLYGON_RPCS"], s["CHAIN_ID"], s["BET_SIZE"], wallet)
    if wallet == "eoa" and pk:
        signer = wizard.signer_address(pk)
        if signer:
            out.append(check_gas(signer, s["POLYGON_RPCS"]))
    out.append(Check("mode", "info", "Modo", "PRUEBA por defecto (DRY_RUN=1): nada de esto activa órdenes reales"
                     if s["DRY_RUN"] else "DRY_RUN=0: el bot operará con dinero real al arrancar"))
    return out


def summarize(checks: List[Check]) -> Tuple[int, int, int]:
    return (sum(c.level == "ok" for c in checks), sum(c.level == "warn" for c in checks), sum(c.level == "fail" for c in checks))


def ready_to_trade(checks: List[Check]) -> bool:
    return not any(c.level == "fail" for c in checks)


# ── aprobaciones (permisos) ────────────────────────────────────────
APPROVE_SELECTOR = "0x095ea7b3"  # approve(address,uint256)
SET_APPROVAL_FOR_ALL = "0xa22cb465"  # setApprovalForAll(address,bool)
IS_APPROVED_FOR_ALL = "0xe985e9c5"  # isApprovedForAll(address,address)
MAX_UINT = (1 << 256) - 1


def _pad(addr: str) -> str:
    return addr.lower().replace("0x", "").rjust(64, "0")


def approval_targets(chain_id: int = 137) -> Dict[str, Any]:
    from py_clob_client_v2.config import get_contract_config

    cc = get_contract_config(chain_id)
    spenders = [("Exchange V2", cc.exchange_v2), ("Exchange V2 (neg-risk)", cc.neg_risk_exchange_v2)]
    return {"collateral": cc.collateral, "ctf": cc.conditional_tokens, "spenders": [(n, a) for n, a in spenders if a]}


def approval_status(address: str, rpcs: List[str], chain_id: int = 137) -> List[Dict[str, Any]]:
    """Qué permisos faltan (lectura RPC): USDC.approve y ConditionalTokens.setApprovalForAll por exchange."""
    t = approval_targets(chain_id)
    items: List[Dict[str, Any]] = []

    def call(to: str, data: str) -> int:
        last: Exception = RuntimeError("sin RPC")
        for rpc in rpcs:
            try:
                return int(_rpc(rpc, "eth_call", [{"to": to, "data": data}, "latest"]), 16)
            except Exception as e:
                last = e
        raise last

    for name, sp in t["spenders"]:
        items.append({"kind": "erc20", "name": f"USDC → {name}", "to": t["collateral"], "spender": sp,
                      "done": call(t["collateral"], "0xdd62ed3e" + _pad(address) + _pad(sp)) >= 10 ** 15})  # ≥ 1.000 millones de USDC = aprobación «máxima»
        items.append({"kind": "ctf", "name": f"Tokens (CTF) → {name}", "to": t["ctf"], "spender": sp,
                      "done": call(t["ctf"], IS_APPROVED_FOR_ALL + _pad(address) + _pad(sp)) == 1})
    return items


def build_approval_tx(item: Dict[str, Any]) -> str:
    if item["kind"] == "erc20":
        return APPROVE_SELECTOR + _pad(item["spender"]) + hex(MAX_UINT)[2:].rjust(64, "0")
    return SET_APPROVAL_FOR_ALL + _pad(item["spender"]) + "1".rjust(64, "0")


def approve_onchain(private_key: str, rpcs: List[str], chain_id: int = 137, *, confirmed: bool = False,
                    wait: bool = True, only_missing: bool = True) -> List[Dict[str, Any]]:
    """Envía las transacciones de aprobación (SOLO EOA). Exige ``confirmed=True``; cada tx es explícita y visible.

    Devuelve [{name, tx, status}]. Lanza PermissionError si no hay confirmación y RuntimeError si falta POL.
    """
    if confirmed is not True:
        raise PermissionError("Las aprobaciones on-chain requieren confirmación explícita")
    from eth_account import Account

    pk = wizard.validate_private_key(private_key)
    acct = Account.from_key(pk)
    items = approval_status(acct.address, rpcs, chain_id)
    todo = [i for i in items if not i["done"]] if only_missing else items
    if not todo:
        return []

    def rpc(method: str, params: list) -> Any:
        last: Exception = RuntimeError("sin RPC")
        for u in rpcs:
            try:
                return _rpc(u, method, params, 15.0)
            except Exception as e:
                last = e
        raise last

    gas_price = int(int(rpc("eth_gasPrice", []), 16) * 1.25)
    balance = int(rpc("eth_getBalance", [acct.address, "latest"]), 16)
    nonce = int(rpc("eth_getTransactionCount", [acct.address, "pending"]), 16)
    results: List[Dict[str, Any]] = []
    prepared = []
    for it in todo:
        data = build_approval_tx(it)
        gas = int(int(rpc("eth_estimateGas", [{"from": acct.address, "to": it["to"], "data": data}]), 16) * 1.3)
        prepared.append((it, data, gas))
    need = sum(g for _i, _d, g in prepared) * gas_price
    if balance < need:
        raise RuntimeError(f"POL insuficiente para el gas: tienes {balance / 1e18:.4f} y hacen falta ≈ {need / 1e18:.4f} POL")
    for it, data, gas in prepared:
        tx = {"chainId": chain_id, "to": it["to"], "value": 0, "data": data, "gas": gas, "gasPrice": gas_price, "nonce": nonce}
        raw = acct.sign_transaction(tx)
        raw_bytes = getattr(raw, "raw_transaction", None) or getattr(raw, "rawTransaction")
        txh = rpc("eth_sendRawTransaction", ["0x" + bytes(raw_bytes).hex()])
        nonce += 1
        status = "enviada"
        if wait:
            status = "pendiente"
            for _ in range(45):
                rc = rpc("eth_getTransactionReceipt", [txh])
                if rc:
                    status = "confirmada" if int(rc.get("status", "0x0"), 16) == 1 else "FALLIDA"
                    break
                time.sleep(2)
        results.append({"name": it["name"], "tx": txh, "status": status})
    return results


def approvals_help(wallet: str) -> str:
    if wallet == "eoa":
        return ("EOA: los permisos se aprueban on-chain con TU clave (cuesta unos céntimos de POL). Se envían 4 transacciones como máximo "
                "(USDC y tokens para cada exchange). Solo se hace si lo confirmas con S.")
    return ("Safe / Magic: Polymarket aprueba los permisos por ti la primera vez que depositas u operas en polymarket.com. "
            "Este asistente NO envía transacciones para esos tipos de cartera; solo puede pedir al CLOB que vuelva a leer el estado (gratis).")
