"""Tabla de monedas de los mercados Up/Down de 5 minutos de Polymarket (sin red, sin claves).

Verificado el 2026-11-20 (Gamma API, series ``<coin>-up-or-down-5m``): BTC, ETH, XRP, SOL, DOGE, BNB, HYPE y ZEC.
Cada mercado se resuelve con el stream Chainlink ``<coin>-usd-twap-60s`` (data.chain.link/streams).

Precio en vivo del bot: el agregador Chainlink <COIN>/USD de **Polygon** (proxy oficial, 8 decimales; comprobado
con ``description()``/``decimals()`` on-chain y con el directorio oficial de Chainlink ``feeds-matic-mainnet``).
HYPE no tiene agregador en Polygon y el de ZEC existe (0xBC08c639e579a391C4228F20d0C29d0690092DF0) pero solo se
actualiza con un 1 % de desviación o cada 24 h (en la prueba en vivo llevaba >5 min sin cambiar), inservible para
ventanas de 5 min. Ambas funcionan en **modo degradado**: precio de Binance spot en lugar de Chainlink (el PTB
temprano c2 sí existe porque Polymarket publica ``hype/usd`` y ``zec/usd`` en su WebSocket RTDS).

Todos los umbrales de la estrategia son relativos (Δ en %, momento de Binance en %, precios de token 0..1), así que
sirven igual para monedas baratas (XRP, DOGE). ``scale`` permite exigir movimientos mayores a monedas más volátiles
(los Δ y el momento se dividen por ``scale`` antes de compararlos). Por defecto 1.0 en todas: BTC/ETH no cambian nada.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional


@dataclass(frozen=True)
class CoinSpec:
    coin: str  # prefijo del slug de Polymarket
    name: str
    chainlink_polygon: Optional[str]  # proxy del agregador <COIN>/USD en Polygon (None = no existe -> Binance)
    binance: str  # símbolo spot de Binance
    rtds: str  # símbolo en el WebSocket RTDS de Polymarket (topic crypto_prices_chainlink)
    chainlink_decimals: int = 8
    chainlink_polygon_slow: Optional[str] = None  # agregador que existe pero no sirve para 5 min (solo documentación)


# Direcciones PÚBLICAS de contratos de precios (no son cuentas de nadie).
COIN_TABLE: Dict[str, CoinSpec] = {
    "btc": CoinSpec("btc", "Bitcoin", "0xc907E116054Ad103354f2D350FD2514433D57F6f", "BTCUSDT", "btc/usd"),
    "eth": CoinSpec("eth", "Ethereum", "0xF9680D99D6C9589e2a93a78A04A279e509205945", "ETHUSDT", "eth/usd"),
    "xrp": CoinSpec("xrp", "XRP", "0x785ba89291f676b5386652eB12b30cF361020694", "XRPUSDT", "xrp/usd"),
    "sol": CoinSpec("sol", "Solana", "0x10C8264C0935b3B9870013e057f330Ff3e9C56dC", "SOLUSDT", "sol/usd"),
    "doge": CoinSpec("doge", "Dogecoin", "0xbaf9327b6564454F4a3364C33eFeEf032b4b4444", "DOGEUSDT", "doge/usd"),
    "bnb": CoinSpec("bnb", "BNB", "0x82a6c4AF830caa6c97bb504425f6A66165C2c26e", "BNBUSDT", "bnb/usd"),
    "hype": CoinSpec("hype", "Hyperliquid", None, "HYPEUSDT", "hype/usd"),
    # ZEC/USD en Polygon: heartbeat 86400 s y desviación 1 % (directorio oficial de Chainlink) -> Binance spot.
    "zec": CoinSpec("zec", "Zcash", None, "ZECUSDT", "zec/usd",
                    chainlink_polygon_slow="0xBC08c639e579a391C4228F20d0C29d0690092DF0"),
}
SUPPORTED: List[str] = list(COIN_TABLE)
DEFAULT_COINS: List[str] = ["btc", "eth"]
LEGACY_COINS = ("btc", "eth")  # redondeos y formatos idénticos a las versiones anteriores


def parse_coins(raw: str | Iterable[str]) -> List[str]:
    """«btc, ETH;xrp» -> ['btc','eth','xrp'] (orden de la tabla, sin repetidos). ValueError si alguna no existe."""
    items = raw if not isinstance(raw, str) else str(raw).replace(";", ",").replace(" ", ",").split(",")
    want = {str(x).strip().lower() for x in items if str(x).strip()}
    if not want:
        raise ValueError("elige al menos una moneda")
    bad = sorted(want - set(SUPPORTED))
    if bad:
        raise ValueError(f"moneda no soportada: {', '.join(bad)} (válidas: {', '.join(SUPPORTED)})")
    return [c for c in SUPPORTED if c in want]


def parse_scales(raw: str) -> Dict[str, float]:
    """«xrp:1.5, doge:2» -> {'xrp': 1.5, 'doge': 2.0}. ValueError si el formato o la moneda no valen."""
    out: Dict[str, float] = {}
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"«{part}»: usa moneda:factor (p. ej. xrp:1.5)")
        c, v = part.split(":", 1)
        c = c.strip().lower()
        if c not in COIN_TABLE:
            raise ValueError(f"moneda no soportada: {c}")
        try:
            f = float(v.strip().replace(",", "."))
        except ValueError:
            raise ValueError(f"«{v.strip()}» no es un número")
        if not (0.2 <= f <= 10.0):
            raise ValueError(f"factor de {c} fuera de rango (0.2–10)")
        out[c] = f
    return out


def slug(coin: str, interval_start: int) -> str:
    return f"{coin}-updown-5m-{int(interval_start)}"


def has_chainlink(coin: str) -> bool:
    return bool(COIN_TABLE[coin].chainlink_polygon)


def degraded_coins(coins: Iterable[str]) -> List[str]:
    return [c for c in coins if c in COIN_TABLE and not has_chainlink(c)]


def round_px(coin: str, x: float, legacy_nd: int) -> float:
    """BTC/ETH: el mismo redondeo de siempre; resto: 8 decimales (DOGE ~0,05 perdería precisión con 2-4)."""
    return round(x, legacy_nd) if coin in LEGACY_COINS else round(x, 8)


def fmt_px(coin: str, x: float) -> str:
    """Formato de precio para el log: BTC/ETH igual que antes (.2f); el resto con 6 cifras significativas."""
    return f"{x:.2f}" if coin in LEGACY_COINS else f"{x:.6g}"


def display_decimals(coin: str, price: Optional[float]) -> int:
    """Decimales para el panel: BTC 2, ETH 3 (como antes); el resto según su precio."""
    if coin == "btc":
        return 2
    if coin == "eth":
        return 3
    p = abs(price or 0.0)
    if p >= 100:
        return 2
    if p >= 1:
        return 3
    if p >= 0.1:
        return 4
    return 5


def scale_feeds(cl: dict, bn: dict, scale: float) -> tuple:
    """Normaliza Δ (chainlink) y momento (binance) por ``scale``. Con 1.0 devuelve los mismos objetos (sin tocar nada)."""
    if scale == 1.0:
        return cl, bn
    cl2, bn2 = dict(cl), dict(bn)
    for k in ("delta_pct", "delta_twap_pct"):
        if cl2.get(k) is not None:
            cl2[k] = float(cl2[k]) / scale
    for k in ("change_1m", "change_3m", "change_5m"):
        if bn2.get(k) is not None:
            bn2[k] = float(bn2[k]) / scale
    return cl2, bn2


def title(coins: Iterable[str]) -> str:
    return "/".join(c.upper() for c in coins)
