"""Textos en español para el usuario: avisos amigables y razones de «no entrar» en una palabra."""

from __future__ import annotations

import re
from typing import Optional, Tuple

# (patrón en minúsculas, nivel, mensaje) — el primero que coincide gana
_RULES = [
    (r"not enough balance|insufficient balance|balance.*allowance|not enough funds",
     "error", "Saldo insuficiente para la orden (hace falta algo más de 1,03 USDC por la comisión). Añade fondos al Safe."),
    (r"allowance",
     "error", "Falta aprobar el USDC (allowance) para operar. Hazlo una vez desde la web de Polymarket."),
    (r"post[- ]?only|cancel[- ]?only|trading is (currently )?disabled|\b503\b",
     "warn", "Polymarket está en modo «solo cancelar/post-only» (503): no admite órdenes ahora. Se reintentará solo."),
    (r"\b425\b|too early|restart",
     "warn", "El motor de órdenes de Polymarket se está reiniciando (425). Reintento automático."),
    (r"\b429\b|too many requests|rate limit",
     "warn", "Demasiadas peticiones a Polymarket (429). El bot espera y sigue."),
    (r"geoblock|restricted|region|\b403\b.*(order|clob)|blocked",
     "error", "Polymarket restringe el trading en algunos países (403). Debes cumplir sus condiciones de uso y la ley de tu país; caro5m no evita esas restricciones."),
    (r"couldn.?t be fully filled|fok|no match|not fully filled|fully filled",
     "warn", "Orden no ejecutada: no había liquidez suficiente a ese precio. No se ha gastado nada."),
    (r"invalid amounts?|invalid.*decimals|invalid size|bad (order|buy|sell)",
     "error", "Importe de orden no válido (decimales). Revisa la apuesta."),
    (r"invalid signature|invalid api key|unauthorized|\b401\b",
     "error", "Credenciales o firma no válidas (401). Revisa el asistente [W]: clave, Safe y tipo de firma."),
    (r"timed? ?out|timeout|read timed out|connecttimeout",
     "warn", "Tiempo de espera agotado hablando con el servidor. Reintentando."),
    (r"connection|name resolution|dns|network is unreachable|max retries",
     "warn", "Problema de red al conectar con el servidor. Reintentando."),
    (r"circuit_breaker",
     "error", "Freno de seguridad activado: demasiadas pérdidas seguidas. El bot descansa un rato."),
    (r"binance_err|bn_err",
     "warn", "No se pueden leer los precios de Binance (¿host bloqueado? prueba BINANCE_HOST)."),
    (r"cl_err|chainlink",
     "warn", "No se puede leer Chainlink por los RPC de Polygon. Comprueba la lista de RPC en Ajustes."),
    (r"clob_httpx_proxy_rebuild_failed|proxy",
     "error", "El proxy no se ha podido aplicar; las órdenes reales no saldrían por él."),
    (r"fatal",
     "error", "Error fatal: el bot se ha detenido. Mira el log con [L]."),
    (r"loop_err",
     "warn", "Error inesperado en el ciclo del bot (sigue funcionando). Mira el log con [L]."),
]


def friendly(raw: str) -> Tuple[str, str]:
    """(nivel, texto en español) para una línea de error/aviso del bot."""
    if raw.startswith(("LIVE_UNAVAILABLE ", "CONTROL_WARN ")):
        txt = raw.split(" ", 1)[1][:160].strip()
        return "error", (txt[:1].upper() + txt[1:]) if txt else txt  # sin .capitalize(): respeta «REAL», «USDC»…
    low = raw.lower()
    for pat, level, msg in _RULES:
        if re.search(pat, low):
            return level, msg
    return "warn", re.sub(r"\s+", " ", raw)[:160]


_WARN_PREFIXES = (
    "ORDER_ERR", "EXIT_TP_ERR", "EXIT_SL_ERR", "EXIT_TIMEOUT_ERR", "SELL_ERR", "REDEEM_ERR", "LOOP_ERR", "FATAL", "CIRCUIT_BREAKER",
    "FEED cl_err", "FEED bn_err", "FEED binance_err", "FEED preload_err", "clob_httpx_proxy_rebuild_failed",
    "LIVE_UNAVAILABLE", "CONTROL_WARN",
)


def is_warning_line(msg: str) -> bool:
    return msg.startswith(_WARN_PREFIXES)


def short_reason(reason: Optional[str], extra: str = "") -> str:
    """Razón de descarte del bot -> una palabra en español para la tabla."""
    r = (reason or extra or "").strip()
    low = r.lower()
    table = [
        ("cooldown", "cooldown"),
        ("paus", "pausa"),
        ("racha", "racha"),
        ("circuit", "racha"),
        ("wide_spread", "margen"),
        ("spread", "margen"),
        ("|d_twap|", "delta"),
        ("not in [", "precio"),
        ("lane_a_ask", "precio"),
        ("rich_ask", "precio"),
        ("ask=", "precio"),
        ("binance_stale", "datos"),
        ("chainlink_stale", "datos"),
        ("missing_feeds", "datos"),
        ("book_missing", "datos"),
        ("no_market", "datos"),
        ("interval_mismatch", "ventana"),
        ("window", "ventana"),
        ("lane_a_late", "ventana"),
        ("twap_spot_disagree", "PTB"),
        ("no_direction", "PTB"),
        ("delta_exhaustion", "delta"),
        ("bin1m", "momento"),
        ("momentum", "momento"),
        ("whipsaw", "momento"),
        ("mild_opp", "momento"),
        ("not_persistent", "momento"),
        ("rsi", "rsi"),
        ("ask_fade", "precio"),
        ("recheck", "recheck"),
    ]
    for needle, word in table:
        if needle in low:
            return word
    return "filtro"
