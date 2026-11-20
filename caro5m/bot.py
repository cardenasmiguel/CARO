#!/usr/bin/env python3
"""caro5m — bot de Polymarket para los mercados Up/Down de 5 minutos (BTC, ETH, XRP, SOL, DOGE, BNB, HYPE, ZEC).

La lógica de decisión (filtros, vetos, TP/SL, freno de seguridad) es la del bot original.
Novedades del paquete publicado: modo PRUEBA (DRY_RUN, por defecto), ajustes por entorno,
canal de estado/control con el TUI (``status.json`` + carpeta ``control/``) y venta manual.

Escribe los ficheros compartidos ``chainlink_<coin>.json`` y ``binance_<coin>.json`` en CARO5M_SHM_DIR
(monedas: ajuste COINS, por defecto btc,eth; tabla en ``caro5m.coins``).
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import ApiCreds, MarketOrderArgsV2, OrderArgsV2, OrderType
from py_clob_client_v2.order_builder.constants import BUY, SELL

from . import __version__, coins as coins_mod, compat, config, messages, redeem
from . import status as status_mod

# ── paths / constants ──────────────────────────────────────────────
CFG = config.load_settings()
ENV_PATH = config.env_file()  # secretos (modo 0600); solo lo lee el bot
STATE_PATH = config.state_path("dry" if CFG["DRY_RUN"] else "live")
LOG_PATH = config.log_path()
SHM_DIR = str(config.shm_dir())

CLOB_HOST = CFG["CLOB_HOST"]
GAMMA_HOST = CFG["GAMMA_HOST"]
BINANCE_HOST = CFG["BINANCE_HOST"]
CHAIN_ID = CFG["CHAIN_ID"]
SIGNATURE_TYPE = CFG["POLY_SIGNATURE_TYPE"]
COINS = tuple(CFG["COINS"])  # ajuste COINS (por defecto btc,eth); en caliente: maybe_reload_settings
COIN_SCALE: Dict[str, float] = dict(CFG["COIN_SCALE"])  # opcional; 1.0 = sin cambios
MAX_LAST_ORDERS = 50
POSITION_TIMEOUT = CFG["POSITION_TIMEOUT"]  # s; al vencer se intenta vender al mejor bid y, si el mercado está resuelto, cobrar
AUTO_REDEEM = bool(CFG["AUTO_REDEEM"])  # cobrar on-chain solo en EOA (firma 0); en PRUEBA se simula

BET_SIZE = CFG["BET_SIZE"]  # capital preserve: was 1.50
MIN_TOKEN = CFG["MIN_TOKEN"]
MAX_TOKEN = CFG["MAX_TOKEN"]  # capital preserve: was 0.85 — high-ask R:R toxic
RICH_ASK_THRESHOLD = 0.62  # above this: soft band to MAX_TOKEN
RICH_ASK_SOFT_DELTA_MIN = 0.10  # selective loosen: was hard 0.15 for all rich
RICH_ASK_DELTA_MIN = 0.15  # kept for docs/future ask>0.70 if MAX rises
RICH_ASK_BIN1M_MIN = 0.03
# Lane A — cheap quality early (Binance lead)
LANE_A_MAX_ASK = 0.60
LANE_A_DELTA_MIN = 0.06  # allow |d_twap| in [0.06, DELTA_MIN)
LANE_A_SECS_MAX = 90
LANE_A_BIN1M_MIN = 0.04  # stronger same-sign; no mild/flat
# Anti-fakeout (2026-09-09 plan phases 1–5)
HIST_MAX_SAMPLES = 24  # ~60s at LOOP_SLEEP 2.5s
BIN1M_PERSIST_SECS = 30.0
BIN1M_PERSIST_MIN = 0.02
BIN1M_WHIPSAW = 0.08
MOMENTUM_DECAY_SECS = 20.0
ASK_FADE_SECS = 20.0
ASK_FADE_DROP = 0.05
LANE_A_RSI_COLD = 40.0
DELTA_EXHAUSTION = 0.18
EXHAUSTION_BIN1M_STRONG = 0.08
ENTRY_SECS_MIN = 45
ENTRY_SECS_MAX = 125  # P1: was 150 — cut late chases
ENTRY_SECS_LEFT_MIN = 120
DELTA_MIN = 0.10
DELTA_MAX = 0.30
DELTA_STRONG = 0.15  # P0: needed for flat/weak bin1m exception
BINANCE_STALE_MAX = 10
CHAINLINK_STALE_MAX = 45
SPREAD_SKIP = 0.08
BIN1M_CONTRA_MAX = 0.03  # P0: was 0.05 (0.025 would kill win @+0.0287)
BIN1M_CONFIRM_MIN = 0.03  # was 0.02 — require stronger same-sign
BIN1M_FLAT_MAX = 0.01  # P0: |bin1m|<=this needs |d_twap|>=DELTA_STRONG
TP_OFFSET = CFG["TP_OFFSET"]
SL_OFFSET = CFG["SL_OFFSET"]  # P1: was 0.15 — fewer noise stops
SL_MIN_HOLD_SECS = 20  # P1: no SL in first 20s after fill
RSI_UP_MAX = 70  # P1: was 80
RSI_DOWN_MIN = 10  # was 20 — rsi_veto_down was FN on DOWN HITs
RSI_DOWN_HIGH_MAX = 75  # P1: veto hot DOWN (missed FOK had rsi 83.7)
BIN1M_UP_FLOOR = -0.02  # P2-1: UP reject stronger negative bin1m
BIN1M_DOWN_MILD_OPP = 0.01  # P2-2: with cold RSI
RSI_DOWN_COLD_MAX = 28  # P2-2: DOWN+mild_opp+rsi below this → veto
FLAT_EARLY_SECS = 60  # P2-3: early flat exception window
FLAT_EARLY_DELTA = 0.11  # P2-3: |d_twap| min for early flat
CB_LOSSES = CFG["CB_LOSSES"]  # was 4 — stop faster after losses
CB_COOLDOWN = CFG["CB_COOLDOWN"]  # was 1800 — 1h cooldown
LOOP_SLEEP = 2.5
NEAR_ZERO = 1e-6

# Per-coin tick history for anti-fakeout neurons
_COIN_HIST: Dict[str, Deque[dict]] = {c: deque(maxlen=HIST_MAX_SAMPLES) for c in COINS}


def record_market_tick(
    coin: str,
    now: float,
    bin1m: float,
    d_twap: float,
    up_ask: float,
    dn_ask: float,
) -> None:
    _COIN_HIST.setdefault(coin, deque(maxlen=HIST_MAX_SAMPLES)).append(
        {
            "ts": float(now),
            "bin1m": float(bin1m),
            "d_twap": float(d_twap),
            "up_ask": float(up_ask),
            "dn_ask": float(dn_ask),
        }
    )


def _hist_window(coin: str, now: float, secs: float) -> List[dict]:
    out: List[dict] = []
    for h in _COIN_HIST.get(coin, []):
        age = float(now) - float(h["ts"])
        if 0 <= age <= secs:
            out.append(h)
    return out


def anti_fakeout_veto(
    coin: str,
    direction: str,
    bin1m: float,
    delta_twap: float,
    abs_d: float,
    ask: float,
    rsi14: float,
    lane: str,
    now: float,
) -> Optional[str]:
    """Filter spike/bounce fakeouts. Returns veto reason or None."""
    # Phase 1: bin1m persistence / whipsaw (prior contra strong)
    w30 = _hist_window(coin, now, BIN1M_PERSIST_SECS)
    if len(w30) >= 3:
        bins = [float(h["bin1m"]) for h in w30]
        if direction == "UP":
            prior_min = min(bins)
            if prior_min <= -BIN1M_WHIPSAW:
                return f"bin1m_whipsaw UP prior_min={prior_min:+.4f}"
            recent = [float(h["bin1m"]) for h in w30 if now - h["ts"] <= 15.0]
            if recent:
                avg15 = sum(recent) / len(recent)
                if avg15 < BIN1M_PERSIST_MIN:
                    return f"bin1m_not_persistent UP avg15={avg15:+.4f}"
        else:
            prior_max = max(bins)
            if prior_max >= BIN1M_WHIPSAW:
                return f"bin1m_whipsaw DOWN prior_max={prior_max:+.4f}"
            recent = [float(h["bin1m"]) for h in w30 if now - h["ts"] <= 15.0]
            if recent:
                avg15 = sum(recent) / len(recent)
                if avg15 > -BIN1M_PERSIST_MIN:
                    return f"bin1m_not_persistent DOWN avg15={avg15:+.4f}"

    # Phase 2: momentum decay — |d_twap| up, |bin1m| down
    w20 = _hist_window(coin, now, MOMENTUM_DECAY_SECS)
    if len(w20) >= 4:
        older = w20[0]
        old_b = float(older["bin1m"])
        old_abs_d = abs(float(older["d_twap"]))
        if abs_d >= old_abs_d + 0.03 and abs(bin1m) + 0.03 < abs(old_b):
            if direction == "UP" and delta_twap > 0 and bin1m < old_b - 0.02:
                return (
                    f"momentum_decay UP bin={old_b:+.4f}->{bin1m:+.4f} "
                    f"|d|={old_abs_d:.4f}->{abs_d:.4f}"
                )
            if direction == "DOWN" and delta_twap < 0 and bin1m > old_b + 0.02:
                return (
                    f"momentum_decay DOWN bin={old_b:+.4f}->{bin1m:+.4f} "
                    f"|d|={old_abs_d:.4f}->{abs_d:.4f}"
                )

    # Phase 3: ask fade on signal side
    wask = _hist_window(coin, now, ASK_FADE_SECS)
    if len(wask) >= 3 and ask > 0:
        if direction == "UP":
            peak = max(float(h["up_ask"]) for h in wask)
            if ask <= peak - ASK_FADE_DROP:
                return f"ask_fade_up ask={ask:.3f} peak={peak:.3f}"
        else:
            peak = max(float(h["dn_ask"]) for h in wask)
            if ask <= peak - ASK_FADE_DROP:
                return f"ask_fade_down ask={ask:.3f} peak={peak:.3f}"

    # Phase 4: Lane A RSI cold on UP
    if lane == "A" and direction == "UP" and rsi14 < LANE_A_RSI_COLD:
        return f"lane_a_rsi_cold rsi={rsi14:.1f}"

    # Phase 5: Lane B delta exhaustion without strong persistent bin1m
    if lane == "B" and abs_d >= DELTA_EXHAUSTION:
        strong = (direction == "UP" and bin1m >= EXHAUSTION_BIN1M_STRONG) or (
            direction == "DOWN" and bin1m <= -EXHAUSTION_BIN1M_STRONG
        )
        if not strong:
            return f"delta_exhaustion |d|={abs_d:.4f} bin1m={bin1m:+.4f}"

    return None


POLYGON_RPCS = list(CFG["POLYGON_RPCS"])
# Contratos PÚBLICOS de los precios Chainlink <COIN>/USD en Polygon (no son cuentas de nadie); ver caro5m/coins.py.
# Una moneda sin agregador en Polygon (HYPE) usa el precio de Binance spot (modo degradado).
CHAINLINK_CONTRACTS = {c: s.chainlink_polygon for c, s in coins_mod.COIN_TABLE.items() if s.chainlink_polygon}
BINANCE_SYMBOL = {c: s.binance for c, s in coins_mod.COIN_TABLE.items()}
TWAP_WINDOW = 30

# ── early Polymarket price-to-beat (caro5m.ptb, proceso aparte) ──
# c2 = mean of RTDS Chainlink 1s points ts in [T-62,T-3] (validated vs Gamma priceToBeat).
# Used ONLY as open_twap (the d_twap reference) of the current window; anything odd -> legacy.
PM_PTB_PATH = str(config.ptb_shm_path())  # el mismo fichero que escribe caro5m.ptb
PM_PTB_ENABLE = bool(CFG["CARO5M_USE_PTB"])  # CARO5M_USE_PTB=0 -> legacy only
PM_PTB_KILL_FILE = str(config.ptb_kill_path())  # touch -> legacy from the next window, no restart
PM_PTB_QUALITIES = ("ok", "degraded")  # c2 "fallback" (Binance) is NOT used -> legacy
PM_PTB_UPGRADE_SECS = 25  # late swap only while secs<=25 (d_twap hist used by entries at 45s = last 20s)
PM_PTB_EMIT_MIN_REL = -10.0  # c2 must have been emitted in [T-10s, T+5s]
PM_PTB_EMIT_MAX_REL = 5.0
PM_PTB_MAX_FEED_AGE = 8.0  # RTDS feed age at emit (s)
PM_PTB_MAX_DEV_PCT = 0.5  # sanity: |c2 - on-chain price| / price, in %


def read_pm_ptb(
    coin: str, iv: int, now: float, ref_price: Optional[float] = None, path: str = PM_PTB_PATH
) -> Tuple[Optional[float], str]:
    """Return (c2 PTB, info) for window iv, or (None, reason). Never raises."""
    try:
        if not PM_PTB_ENABLE:
            return None, "disabled_env"
        if os.path.exists(PM_PTB_KILL_FILE):
            return None, "kill_file"
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        rec = (d.get("corrected") or {}).get(coin) or {}
        T = int(rec.get("T") or 0)
        if T != int(iv):
            return None, f"T_mismatch file_T={T}"
        q = str(rec.get("c2_quality") or "")
        if q not in PM_PTB_QUALITIES:
            return None, f"quality={q or 'none'}"
        ptb = float(rec.get("c2_ptb"))
        if not (math.isfinite(ptb) and ptb > 0):
            return None, "bad_value"
        emit_ts = float(rec.get("c2_emit_ts"))
        emit_rel = emit_ts - T
        if not (PM_PTB_EMIT_MIN_REL <= emit_rel <= PM_PTB_EMIT_MAX_REL):
            return None, f"emit_rel={emit_rel:+.1f}s"
        if emit_ts > now + 2.0:
            return None, "emit_in_future"
        fa = rec.get("c2_rtds_feed_age_s")
        if fa is None or float(fa) > PM_PTB_MAX_FEED_AGE:
            return None, f"feed_age={fa}"
        if ref_price:
            dev = abs(ptb - float(ref_price)) / float(ref_price) * 100.0
            if dev > PM_PTB_MAX_DEV_PCT:
                return None, f"dev_vs_onchain={dev:.3f}%"
        return ptb, (
            f"q={q} emit_rel={emit_rel:+.2f}s n={rec.get('c2_n_pts')} interp={rec.get('c2_n_interp')}"
        )
    except FileNotFoundError:
        return None, "no_file"
    except Exception as e:
        return None, f"read_err {type(e).__name__}"

# ── logging: stdout (systemd/tee guardan el fichero); CARO5M_LOG_TO_FILE=1 lo escribe también aquí ──
WARNINGS: Deque[Dict[str, Any]] = deque(maxlen=40)  # avisos recientes para el TUI
_LOG_TO_FILE = os.environ.get("CARO5M_LOG_TO_FILE", "0") == "1"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    line = f"[{_ts()}] {msg}"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError, ValueError):  # consola sin UTF-8 (Windows) o cerrada: el fichero de log sigue
        pass
    if _LOG_TO_FILE:
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
    try:
        if messages.is_warning_line(msg):
            level, text = messages.friendly(msg)
            if WARNINGS and WARNINGS[-1]["text"] == text:
                WARNINGS[-1]["count"] += 1
                WARNINGS[-1]["ts"] = time.time()
            else:
                WARNINGS.append({"ts": time.time(), "level": level, "text": text, "count": 1})
    except Exception:
        pass


# ── env / proxy ────────────────────────────────────────────────────
def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


SECRET_KEYS = (
    "POLY_PRIVATE_KEY", "POLY_SAFE_ADDRESS", "POLY_FUNDER_ADDRESS", "POLYMARKET_PROXY_LIST",
    "POLY_CLOB_API_KEY", "CLOB_API_KEY", "POLY_CLOB_API_SECRET", "POLY_CLOB_SECRET", "CLOB_API_SECRET",
    "POLY_CLOB_API_PASSPHRASE", "POLY_CLOB_PASSPHRASE", "CLOB_API_PASSPHRASE", "POLY_CLOB_NONCE",
)


def read_secrets(quiet: bool = False) -> dict[str, str]:
    """Secretos de ENV_PATH (si existe) y, con prioridad, del entorno del proceso. Nunca se imprimen."""
    out: dict[str, str] = {}
    try:
        out.update(load_env(ENV_PATH))
    except FileNotFoundError:
        if not quiet:
            log(f"env file not found: {ENV_PATH.name} (válido en modo prueba; usa el asistente del TUI para crearlo)")
    for k in SECRET_KEYS:
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def funder_address(env: dict[str, str]) -> str:
    return env.get("POLY_SAFE_ADDRESS") or env.get("POLY_FUNDER_ADDRESS") or ""


def keys_in_env(env: dict[str, str]) -> bool:
    """¿Hay clave privada (y dirección Safe/funder si la firma la necesita)? No dice nada del valor."""
    return bool(env.get("POLY_PRIVATE_KEY")) and (SIGNATURE_TYPE == 0 or bool(funder_address(env)))


def pick_proxy(proxy_list: str) -> str:
    """Elige el primer proxy de la lista (orden configurado). Vacío = sin proxy. Sin preferencia por país."""
    items = [x.strip() for x in proxy_list.replace(";", ",").split(",") if x.strip()]
    if not items:
        return ""
    first = items[0]
    return first if "://" in first else "http://" + first


def apply_proxy(proxy_url: str) -> None:
    # Do NOT set HTTP_PROXY/HTTPS_PROXY process-wide — that forces SCAN through proxy.
    # Clear any inherited proxy env so requests.Session SCAN stays direct.
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "all_proxy"):
        os.environ.pop(k, None)
    # py_clob_client creates module-level httpx.Client at import time.
    # Rebuild so create_order/post_order use the configured proxy only (SCAN stays direct unless CARO5M_PROXY_SCAN).
    try:
        import httpx
        from py_clob_client_v2.http_helpers import helpers as clob_helpers
        try:
            clob_helpers._http_client.close()
        except Exception:
            pass
        clob_helpers._http_client = httpx.Client(http2=True, proxy=proxy_url, timeout=30.0)
        log(f"clob_httpx_proxy_rebuilt host={urlparse(proxy_url).hostname}")
    except Exception as e:
        log(f"clob_httpx_proxy_rebuild_failed: {e}")
        raise RuntimeError(f"no se pudo aplicar el proxy: {e}")  # fallar fuerte: sin proxy las órdenes saldrían por la IP del servidor


# ── state ──────────────────────────────────────────────────────────
def default_state() -> dict[str, Any]:
    return {
        "positions": {},
        "loss_streak": 0,
        "cooldown_until": 0,
        "last_orders": [],
        "fills": [],
        "trades": [],
    }


def set_state_mode(mode: str) -> None:
    global STATE_PATH
    STATE_PATH = config.state_path(mode)


def load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        try:
            st = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            for k, v in default_state().items():
                st.setdefault(k, v)
            return st
        except Exception:
            pass
    return default_state()


def save_state(st: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    st["last_orders"] = st.get("last_orders", [])[-MAX_LAST_ORDERS:]
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2, sort_keys=True), encoding="utf-8")
    compat.replace(tmp, STATE_PATH)


def atomic_write_json(path: str, obj: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    compat.replace(tmp, path)


def read_json(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def feed_age(ts: float, now: float) -> float:
    if not ts:
        return 9999.0
    if ts > 1e12:
        ts = ts / 1000.0
    return max(0.0, now - ts)


# ── lean SHM feeds (replaces caro-5m writers) ──────────────────────
def rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    gains = []
    losses = []
    for i in range(-period, 0):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains) / period
    al = sum(losses) / period
    if al <= 1e-12:
        return 100.0
    rs = ag / al
    return 100.0 - (100.0 / (1.0 + rs))


class FeedWriter(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="caro5m-feeds")
        self._stop_evt = threading.Event()
        self.rpc_i = 0
        self.price_buf: Dict[str, Deque[Tuple[int, float]]] = {c: deque() for c in COINS}
        self.open_spot: Dict[str, float] = {}
        self.open_twap: Dict[str, float] = {}
        self.interval: Dict[str, int] = {}
        self.last_price: Dict[str, float] = {}
        self.ptb_iv: Dict[str, int] = {}  # window for which ptb_src is tracked
        self.ptb_src: Dict[str, str] = {}  # "pm_c2" | "legacy"
        self.ptb_why: Dict[str, str] = {}  # last reason pm_c2 was not used
        self.ptb_logged: Dict[str, int] = {}  # window already logged as legacy
        self.bn_spot: Dict[str, Tuple[float, int]] = {}  # último precio Binance (para monedas sin Chainlink en Polygon)
        self.sess = requests.Session()
        self.sess.headers.update({"User-Agent": "caro5m-feeds/1.0"})
        self.coins: List[str] = list(COINS)  # monedas con feed (configuradas + las que aún tienen posición abierta)
        self._preload: List[str] = []  # monedas añadidas en caliente: cargar su apertura de Binance
        self._lock = threading.Lock()

    def stop(self) -> None:
        self._stop_evt.set()

    def set_coins(self, coins: List[str]) -> Tuple[List[str], List[str]]:
        """Cambia en caliente las monedas con feed. Devuelve (añadidas, quitadas). Nunca bloquea el bucle principal."""
        new = [c for c in coins if c in BINANCE_SYMBOL]
        with self._lock:
            added = [c for c in new if c not in self.coins]
            removed = [c for c in self.coins if c not in new]
            if not added and not removed:
                return [], []
            self.coins = new
            self._preload.extend(added)
            for c in added:
                self.price_buf.setdefault(c, deque())
        if added or removed:
            log(f"FEED coins={','.join(new)}" + (f" +{','.join(added)}" if added else "") + (f" -{','.join(removed)}" if removed else ""))
        return added, removed

    def _snapshot(self) -> Tuple[List[str], List[str]]:
        with self._lock:
            pre, self._preload = self._preload, []
            return list(self.coins), pre

    def chainlink_price(self, coin: str) -> Optional[Tuple[float, int]]:
        contract = CHAINLINK_CONTRACTS.get(coin)
        if not contract:  # sin agregador en Polygon: precio de Binance spot (modo degradado)
            got = self.bn_spot.get(coin)
            if got and time.time() - got[1] <= BINANCE_STALE_MAX:
                return got
            return None
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": contract, "data": "0xfeaf968c"}, "latest"],
            "id": 1,
        }
        for _ in range(len(POLYGON_RPCS)):
            rpc = POLYGON_RPCS[self.rpc_i % len(POLYGON_RPCS)]
            self.rpc_i += 1
            try:
                r = self.sess.post(rpc, json=payload, timeout=5)
                r.raise_for_status()
                hex_data = r.json().get("result") or ""
                if not isinstance(hex_data, str) or len(hex_data) < 322:
                    continue
                h = hex_data[2:]
                answer = int(h[64:128], 16)
                updated_at = int(h[192:256], 16)
                return answer / 1e8, updated_at
            except Exception:
                continue
        return None

    def preload_open_binance(self, coin: str) -> None:
        sym = BINANCE_SYMBOL[coin]
        try:
            r = self.sess.get(
                f"{BINANCE_HOST}/api/v3/klines",
                params={"symbol": sym, "interval": "5m", "limit": 2},
                timeout=8,
            )
            r.raise_for_status()
            rows = r.json()
            if rows:
                open_px = float(rows[-1][1])
                self.open_spot[coin] = open_px
                self.open_twap[coin] = open_px
                self.interval[coin] = int(rows[-1][0]) // 1000
                log(f"FEED preload {coin} open={coins_mod.fmt_px(coin, open_px)} iv={self.interval[coin]}")
        except Exception as e:
            log(f"FEED preload_err {coin}: {e}")

    def apply_pm_ptb(self, coin: str, iv: int, now: int, price: float) -> None:
        """Swap open_twap for the early Polymarket PTB (c2) when valid; else keep legacy. Never raises."""
        try:
            first = self.ptb_iv.get(coin) != iv  # first chainlink write of this window
            if first:
                self.ptb_iv[coin] = iv
                self.ptb_src[coin] = "legacy"
                self.ptb_why[coin] = "past_upgrade_window"
            if self.ptb_src.get(coin) == "pm_c2":
                return
            secs_into = now - iv
            # first write: nothing of this window has been scanned yet (cl_iv mismatch), so swap at any secs
            if first or secs_into <= PM_PTB_UPGRADE_SECS:
                ptb, why = read_pm_ptb(coin, iv, time.time(), ref_price=price)
                if ptb is not None:
                    legacy = float(self.open_twap.get(coin) or 0.0)
                    self.open_twap[coin] = ptb
                    self.ptb_src[coin] = "pm_c2"
                    diff = ((legacy - ptb) / ptb * 1e4) if legacy > 0 else 0.0
                    log(
                        f"PTB_SRC=pm_c2 {coin} iv={iv} secs={secs_into} ptb={ptb:.4f} "
                        f"legacy_open_twap={legacy:.4f} legacy_minus_c2_bps={diff:+.2f} {why}"
                    )
                    return
                self.ptb_why[coin] = why
            elif self.ptb_logged.get(coin) != iv:
                self.ptb_logged[coin] = iv
                log(
                    f"PTB_SRC=legacy {coin} iv={iv} open_twap={float(self.open_twap.get(coin) or 0.0):.4f} "
                    f"reason={self.ptb_why.get(coin)}"
                )
        except Exception:
            pass

    def write_chainlink(self, coin: str) -> None:
        got = self.chainlink_price(coin)
        if not got:
            return
        price, updated_at = got
        now = int(time.time())
        iv = now - (now % 300)
        buf = self.price_buf.setdefault(coin, deque())
        buf.append((now, price))
        cutoff = now - (TWAP_WINDOW + 2)
        while buf and buf[0][0] < cutoff:
            buf.popleft()

        # TWAP-30s (time-weighted approx via trapezoid on samples)
        if len(buf) == 1:
            twap = price
        else:
            area = 0.0
            dur = 0.0
            for i in range(1, len(buf)):
                t0, p0 = buf[i - 1]
                t1, p1 = buf[i]
                dt = float(t1 - t0)
                if dt <= 0:
                    continue
                area += (p0 + p1) * 0.5 * dt
                dur += dt
            twap = (area / dur) if dur > 0 else price

        prev_iv = self.interval.get(coin)
        if prev_iv is None:
            self.interval[coin] = iv
            self.open_spot.setdefault(coin, price)
            self.open_twap.setdefault(coin, twap)
        elif iv != prev_iv:
            # open = last price of previous interval
            self.open_spot[coin] = self.last_price.get(coin, price)
            self.open_twap[coin] = twap
            self.interval[coin] = iv
            log(f"FEED new_interval {coin} iv={iv} open={coins_mod.fmt_px(coin, self.open_spot[coin])} open_twap={coins_mod.fmt_px(coin, twap)}")

        self.apply_pm_ptb(coin, iv, now, price)

        open_spot = self.open_spot.get(coin, price)
        open_twap = self.open_twap.get(coin, twap)
        delta_pct = ((price - open_spot) / open_spot * 100.0) if open_spot > 0 else 0.0
        delta_twap = ((twap - open_twap) / open_twap * 100.0) if open_twap > 0 else 0.0
        secs_into = now - iv
        secs_left = max(0, 300 - secs_into)
        stale = max(0, now - int(updated_at or now))
        self.last_price[coin] = price

        obj = {
            "price": coins_mod.round_px(coin, price, 4),
            "open_price_5m": coins_mod.round_px(coin, open_spot, 4),
            "direction": "UP" if price >= open_spot else "DOWN",
            "delta_pct": round(delta_pct, 6),
            "twap_30s": coins_mod.round_px(coin, twap, 4),
            "open_twap_5m": coins_mod.round_px(coin, open_twap, 4),
            "delta_twap_pct": round(delta_twap, 6),
            "direction_twap": "UP" if twap >= open_twap else "DOWN",
            "updated_at": int(updated_at or now),
            "stale_secs": int(stale),
            "interval_start": iv,
            "secs_remaining": secs_left,
            "secs_into": secs_into,
            "ts": now,
            "ptb_src": self.ptb_src.get(coin, "legacy"),
        }
        if coin not in CHAINLINK_CONTRACTS:
            obj["price_src"] = "binance"  # modo degradado (no hay Chainlink de esta moneda en Polygon)
        atomic_write_json(f"{SHM_DIR}/chainlink_{coin}.json", obj)

    def write_binance(self, coin: str) -> None:
        sym = BINANCE_SYMBOL[coin]
        try:
            t = self.sess.get(
                f"{BINANCE_HOST}/api/v3/ticker/price",
                params={"symbol": sym},
                timeout=5,
            )
            t.raise_for_status()
            price = float(t.json()["price"])
            self.bn_spot[coin] = (price, int(time.time()))
            k = self.sess.get(
                f"{BINANCE_HOST}/api/v3/klines",
                params={"symbol": sym, "interval": "1m", "limit": 60},
                timeout=8,
            )
            k.raise_for_status()
            rows = k.json()
            closes = [float(r[4]) for r in rows]
            # change_1m from close of previous completed 1m vs now
            if len(closes) >= 2:
                ref = closes[-2]
                change_1m = ((price - ref) / ref * 100.0) if ref else 0.0
            else:
                change_1m = 0.0
            if len(closes) >= 4:
                ref3 = closes[-4]
                change_3m = ((price - ref3) / ref3 * 100.0) if ref3 else 0.0
            else:
                change_3m = 0.0
            if len(closes) >= 6:
                ref5 = closes[-6]
                change_5m = ((price - ref5) / ref5 * 100.0) if ref5 else 0.0
            else:
                change_5m = 0.0
            rsi14 = rsi(closes, 14)
            now = int(time.time())
            obj = {
                "price": coins_mod.round_px(coin, price, 2),
                "change_1m": round(change_1m, 4),
                "change_3m": round(change_3m, 4),
                "change_5m": round(change_5m, 4),
                "atr": 0.0,
                "volatile": False,
                "ts": now,
                "mro": 0.0,
                "ema50": coins_mod.round_px(coin, sum(closes[-50:]) / min(50, len(closes)), 2) if closes else price,
                "rsi14": round(rsi14, 2),
                "macd_hist": 0.0,
                "wick_u_5m": 0.0,
                "wick_l_5m": 0.0,
                "vol_spike_pct": 0.0,
                "rsi14_15m": round(rsi14, 2),
                "macd_15m": 0.0,
                "buy_vol_ratio_5m": 0.5,
                "z_score": 0.0,
            }
            atomic_write_json(f"{SHM_DIR}/binance_{coin}.json", obj)
        except Exception as e:
            log(f"FEED binance_err {coin}: {e}")

    def _feed_one(self, c: str) -> None:
        try:
            self.write_chainlink(c)
        except Exception as e:
            log(f"FEED cl_err {c}: {e}")
        try:
            self.write_binance(c)
        except Exception as e:
            log(f"FEED bn_err {c}: {e}")

    def run(self) -> None:
        log(f"FEED writer starting (chainlink RPC x{len(POLYGON_RPCS)} + binance REST {BINANCE_HOST})")
        for c in self._snapshot()[0]:
            self.preload_open_binance(c)
        pool = None
        while not self._stop_evt.is_set():
            coins, pre = self._snapshot()  # la lista puede cambiar en caliente (ajuste COINS)
            for c in pre:
                self.preload_open_binance(c)
            if len(coins) > 2 and pool is None:  # con más monedas, cada una en su hilo: los feeds no se quedan viejos (BTC/ETH solos: igual que antes)
                from concurrent.futures import ThreadPoolExecutor

                pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="caro5m-feed")
            if pool is not None and len(coins) > 2:
                list(pool.map(self._feed_one, coins))
            else:
                for c in coins:
                    self._feed_one(c)
            self._stop_evt.wait(2.0)
        if pool is not None:
            pool.shutdown(wait=False)
        log("FEED writer stopped")


# ── market discovery / book ────────────────────────────────────────
def parse_gamma_market(raw: dict, slug: str = "", interval: Optional[int] = None) -> Optional[dict]:
    """Normaliza un mercado de Gamma (abierto o ya resuelto). None si faltan tokens UP/DOWN."""
    if not raw:
        return None
    outcomes, tokens = raw.get("outcomes"), raw.get("clobTokenIds") or raw.get("clob_token_ids")
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    if not outcomes or not tokens or len(outcomes) != len(tokens):
        return None
    up_id = down_id = None
    for o, tok in zip(outcomes, tokens):
        ol = str(o).lower()
        if ol == "up":
            up_id = str(tok)
        elif ol == "down":
            down_id = str(tok)
    if not up_id or not down_id:
        return None
    cid = raw.get("conditionId") or raw.get("condition_id") or ""
    if cid and not str(cid).startswith("0x"):
        cid = "0x" + str(cid)
    return {
        "slug": raw.get("slug") or slug,
        "up_token": up_id,
        "down_token": down_id,
        "interval": interval,
        "condition_id": str(cid).lower() if cid else "",
        "outcomes": list(outcomes),
        "accepting_orders": bool(raw.get("acceptingOrders")),
        "closed": bool(redeem.market_is_closed(raw)),
        "outcome_prices": redeem.parse_outcome_prices(raw.get("outcomePrices") or raw.get("outcome_prices")),
        "neg_risk": bool(raw.get("negRisk") or raw.get("neg_risk") or raw.get("enableNegRisk")),
        "raw": raw,
    }


def fetch_market_by_slug(session: requests.Session, slug: str, timeout: float = 8) -> Optional[dict]:
    """Lee un mercado por slug aunque ya no acepte órdenes (hace falta para cobrar al resolver)."""
    r = session.get(f"{GAMMA_HOST}/markets", params={"slug": slug}, timeout=timeout)
    r.raise_for_status()
    HEALTH.update(gamma_ok=True, gamma_ts=time.time())
    data = r.json()
    if not data:
        return None
    return parse_gamma_market(data[0], slug)


def discover_market(session: requests.Session, coin: str, now: int) -> Optional[dict]:
    base = now - (now % 300)
    for off in (0, 300, -300):
        slug = coins_mod.slug(coin, base + off)
        try:
            info = fetch_market_by_slug(session, slug)
            if not info or not info.get("accepting_orders"):
                continue
            info["interval"] = base + off
            return info
        except Exception as e:
            HEALTH.update(gamma_ok=False, gamma_ts=time.time())
            log(f"DISCOVER err {slug}: {e}")
    return None


def best_bid_ask(session: requests.Session, token_id: str) -> Tuple[Optional[float], Optional[float]]:
    try:
        r = session.get(f"{CLOB_HOST}/book", params={"token_id": token_id}, timeout=8)
        r.raise_for_status()
        HEALTH.update(clob_ok=True, clob_ts=time.time())
        book = r.json()
        asks = book.get("asks") or []
        bids = book.get("bids") or []
        best_ask = min((float(a["price"]) for a in asks), default=None)
        best_bid = max((float(b["price"]) for b in bids), default=None)
        return best_bid, best_ask
    except Exception as e:
        HEALTH.update(clob_ok=False, clob_ts=time.time())
        log(f"BOOK err: {e}")
        return None, None


# ── signal ─────────────────────────────────────────────────────────
def evaluate_signal(
    coin: str,
    cl: dict,
    bn: dict,
    now: float,
    up_ask: Optional[float] = None,
    dn_ask: Optional[float] = None,
) -> Tuple[Optional[str], str, dict]:
    meta: dict[str, Any] = {}
    secs_elapsed = float(cl.get("secs_into") or 0)
    secs_left = float(cl.get("secs_remaining") or (300 - secs_elapsed))
    meta.update(secs_elapsed=secs_elapsed, secs_left=secs_left)

    cl_reported = float(cl.get("stale_secs") if cl.get("stale_secs") is not None else 0)
    cl_age = feed_age(float(cl.get("updated_at") or cl.get("ts") or 0), now)
    cl_stale = max(cl_reported, cl_age)
    bn_age = feed_age(float(bn.get("ts") or 0), now)
    meta.update(cl_stale=cl_stale, bn_age=bn_age)

    if bn_age > BINANCE_STALE_MAX:
        return None, f"binance_stale={bn_age:.0f}s", meta
    if float(cl_stale) > CHAINLINK_STALE_MAX:
        return None, f"chainlink_stale={cl_stale:.0f}s", meta

    if not (ENTRY_SECS_MIN <= secs_elapsed <= ENTRY_SECS_MAX and secs_left >= ENTRY_SECS_LEFT_MIN):
        return None, f"window secs={secs_elapsed:.0f} left={secs_left:.0f}", meta

    delta_twap = float(cl.get("delta_twap_pct") or 0.0)
    delta_spot = float(cl.get("delta_pct") or 0.0)
    abs_d = abs(delta_twap)
    meta.update(delta_twap=delta_twap, delta_spot=delta_spot)

    direction = (cl.get("direction_twap") or "").upper()
    if direction not in ("UP", "DOWN"):
        if delta_twap > NEAR_ZERO:
            direction = "UP"
        elif delta_twap < -NEAR_ZERO:
            direction = "DOWN"
        else:
            return None, "no_direction", meta
    meta["direction"] = direction

    if abs_d > DELTA_MAX:
        return None, f"|d_twap|={abs_d:.4f} > {DELTA_MAX}", meta

    lane_a = (
        LANE_A_DELTA_MIN <= abs_d < DELTA_MIN
        and secs_elapsed <= LANE_A_SECS_MAX
    )
    if abs_d < DELTA_MIN and not lane_a:
        return None, f"|d_twap|={abs_d:.4f} not in [{DELTA_MIN},{DELTA_MAX}]", meta

    if abs(delta_spot) > NEAR_ZERO and abs(delta_twap) > NEAR_ZERO:
        if (delta_spot > 0) != (delta_twap > 0):
            return None, f"twap_spot_disagree spot={delta_spot:+.4f} twap={delta_twap:+.4f}", meta

    bin1m = float(bn.get("change_1m") or 0.0)
    meta["bin1m"] = bin1m
    # P0: hard contra veto
    if direction == "UP" and bin1m < -BIN1M_CONTRA_MAX:
        return None, f"bin1m_contra UP bin1m={bin1m:+.4f}", meta
    if direction == "DOWN" and bin1m > BIN1M_CONTRA_MAX:
        return None, f"bin1m_contra DOWN bin1m={bin1m:+.4f}", meta
    if direction == "UP" and bin1m < BIN1M_UP_FLOOR:
        return None, f"bin1m_up_floor UP bin1m={bin1m:+.4f} floor={BIN1M_UP_FLOOR}", meta

    if lane_a:
        # Lane A: no mild/flat — only strong same-sign Binance lead
        same_a = (direction == "UP" and bin1m >= LANE_A_BIN1M_MIN) or (
            direction == "DOWN" and bin1m <= -LANE_A_BIN1M_MIN
        )
        if not same_a:
            return None, (
                f"lane_a_no_bin1m {direction} bin1m={bin1m:+.4f} "
                f"need_same>={LANE_A_BIN1M_MIN} |d|={abs_d:.4f}"
            ), meta
        meta["lane"] = "A"
    else:
        same_sign = (direction == "UP" and bin1m >= BIN1M_CONFIRM_MIN) or (
            direction == "DOWN" and bin1m <= -BIN1M_CONFIRM_MIN
        )
        flat_ok = abs(bin1m) <= BIN1M_FLAT_MAX and (
            abs_d >= DELTA_STRONG
            or (secs_elapsed <= FLAT_EARLY_SECS and abs_d >= FLAT_EARLY_DELTA)
        )
        if not same_sign and not flat_ok:
            return None, (
                f"bin1m_no_confirm {direction} bin1m={bin1m:+.4f} "
                f"|d|={abs_d:.4f} secs={secs_elapsed:.0f} "
                f"need_same>={BIN1M_CONFIRM_MIN} or flat early/strong"
            ), meta
        meta["lane"] = "B"

    rsi14 = float(bn.get("rsi14") or 50.0)
    meta["rsi14"] = rsi14
    if direction == "UP" and rsi14 > RSI_UP_MAX:
        return None, f"rsi_veto_up rsi={rsi14:.1f}", meta
    if direction == "DOWN" and rsi14 < RSI_DOWN_MIN:
        return None, f"rsi_veto_down rsi={rsi14:.1f}", meta
    if direction == "DOWN" and rsi14 > RSI_DOWN_HIGH_MAX:
        return None, f"rsi_veto_down_hot rsi={rsi14:.1f}", meta
    if (
        direction == "DOWN"
        and bin1m > BIN1M_DOWN_MILD_OPP
        and rsi14 < RSI_DOWN_COLD_MAX
    ):
        return None, (
            f"down_mild_opp_cold bin1m={bin1m:+.4f} rsi={rsi14:.1f}"
        ), meta

    lane = meta.get("lane", "B")
    ask_side: Optional[float] = None
    if direction == "UP" and up_ask is not None:
        ask_side = float(up_ask)
    elif direction == "DOWN" and dn_ask is not None:
        ask_side = float(dn_ask)

    if ask_side is not None:
        veto = anti_fakeout_veto(
            coin=coin,
            direction=direction,
            bin1m=bin1m,
            delta_twap=delta_twap,
            abs_d=abs_d,
            ask=ask_side,
            rsi14=rsi14,
            lane=str(lane),
            now=now,
        )
        if veto:
            meta["anti_fakeout"] = veto
            return None, veto, meta

    tag = "SIGNAL_LANE_A" if lane == "A" else "SIGNAL"
    # Phase 0: trace for later audits
    log(
        f"NEURON_TRACE {coin.upper()} {direction} lane={lane} "
        f"d_twap={delta_twap:+.4f} bin1m={bin1m:+.4f} rsi={rsi14:.1f} "
        f"ask={ask_side if ask_side is not None else -1:.3f} hist_n={len(_COIN_HIST.get(coin, []))}"
    )
    return (
        direction,
        f"{tag} {direction} d_twap={delta_twap:+.4f} bin1m={bin1m:+.4f} rsi={rsi14:.1f}",
        meta,
    )


# ── clob client ────────────────────────────────────────────────────
def build_client(env: dict[str, str]) -> ClobClient:
    """Cliente CLOB (SOLO modo real). Sin credenciales L2 en el env las deriva de la clave (llamada autenticada)."""
    pk = env.get("POLY_PRIVATE_KEY") or ""
    funder = funder_address(env)
    if not pk:
        raise RuntimeError("falta POLY_PRIVATE_KEY")
    if SIGNATURE_TYPE != 0 and not funder:
        raise RuntimeError("falta POLY_SAFE_ADDRESS (o POLY_FUNDER_ADDRESS)")

    key = env.get("POLY_CLOB_API_KEY") or env.get("CLOB_API_KEY") or ""
    secret = (
        env.get("POLY_CLOB_API_SECRET")
        or env.get("POLY_CLOB_SECRET")
        or env.get("CLOB_API_SECRET")
        or ""
    )
    phrase = (
        env.get("POLY_CLOB_API_PASSPHRASE")
        or env.get("POLY_CLOB_PASSPHRASE")
        or env.get("CLOB_API_PASSPHRASE")
        or ""
    )

    client = ClobClient(
        CLOB_HOST,
        chain_id=CHAIN_ID,
        key=pk,
        signature_type=SIGNATURE_TYPE,
        funder=funder or None,
    )
    if key and secret and phrase:
        client.set_api_creds(ApiCreds(api_key=key, api_secret=secret, api_passphrase=phrase))
        log("CLOB creds loaded from env")
    else:
        log("CLOB creds missing/partial — deriving from key")
        nonce = env.get("POLY_CLOB_NONCE")
        creds = client.create_or_derive_api_key(nonce=int(nonce)) if (nonce or "").isdigit() else client.create_or_derive_api_key()  # v2 1.1.0: no existe ..._api_creds
        client.set_api_creds(creds)
        log("CLOB creds derived OK")
    return client


MAX_PRICE = 0.99  # precio máximo que acepta Polymarket (tick 0.01)
MIN_PRICE = 0.01


def tp_target(entry: float, offset: Optional[float] = None) -> float:
    """Objetivo de take-profit efectivo: entrada + TP, pero nunca por encima de 0.99 (no existe precio 1.00 en el libro).
    Si entrada + TP >= 0.99, en la práctica se vende a 0.99 si el bid llega, o se mantiene hasta la resolución/tiempo máximo."""
    off = TP_OFFSET if offset is None else float(offset)
    return min(float(entry) + off, MAX_PRICE)


def sl_target(entry: float, offset: Optional[float] = None) -> float:
    """Stop-loss: entrada − SL. Si queda por debajo de 0.01 el SL no salta nunca (equivale a desactivarlo)."""
    off = SL_OFFSET if offset is None else float(offset)
    return float(entry) - off


def place_fok(client: Optional[ClobClient], token_id: str, side: str, price: float, size: float, dry: bool = False) -> dict:
    # FOK buys must be market orders: maker (USDC) max 2dp, taker (shares) max 4dp.
    # Limit+FOK was rejected with invalid amounts when price*size had >2 maker decimals.
    price = round(float(price), 4)
    if price <= 0:
        raise ValueError(f"bad order price={price}")
    if price > MAX_PRICE:  # nunca una orden con precio inválido (>= 1): se limita a 0.99
        price = MAX_PRICE
    side_u = str(side).upper()
    if side_u == str(BUY).upper() or side_u == "BUY":
        amount = round(float(BET_SIZE), 2)  # USDC
        if amount <= 0:
            raise ValueError(f"bad buy amount={amount}")
        if dry:  # PRUEBA: no se firma ni se envía nada; relleno simulado al precio visto
            return {"status": "matched", "dry_run": True, "makingAmount": str(amount), "takingAmount": str(round(amount / price, 4))}
        args = MarketOrderArgsV2(
            token_id=str(token_id),
            amount=amount,
            side=BUY,
            price=price,
            order_type=OrderType.FOK,
        )
    else:
        amount = round(float(size), 4)  # shares
        if amount <= 0:
            raise ValueError(f"bad sell amount={amount}")
        if dry:
            return {"status": "matched", "dry_run": True, "makingAmount": str(amount), "takingAmount": str(round(amount * price, 4))}
        args = MarketOrderArgsV2(
            token_id=str(token_id),
            amount=amount,
            side=SELL,
            price=price,
            order_type=OrderType.FOK,
        )
    if client is None:
        raise RuntimeError("cliente CLOB no inicializado (modo real sin credenciales)")
    signed = client.create_market_order(args)
    resp = client.post_order(signed, OrderType.FOK)
    return resp if isinstance(resp, dict) else {"raw": resp}


def order_filled(resp: dict) -> bool:
    if not resp:
        return False
    status = str(resp.get("status") or resp.get("orderStatus") or "").lower()
    if status in ("matched", "filled"):
        return True
    if float(resp.get("takingAmount") or 0) > 0 or float(resp.get("makingAmount") or 0) > 0:
        return True
    return False


# ── runtime: modo, controles, estado para el TUI ────────────────────
HEALTH: Dict[str, Any] = {"gamma_ok": None, "clob_ok": None, "gamma_ts": 0.0, "clob_ts": 0.0}


class Runtime:
    def __init__(self, mode: str) -> None:
        self.mode = mode  # "dry" | "live"
        self.client: Optional[ClobClient] = None
        self.paused = False
        self.sell_coins: set = set()
        self.cs: Dict[str, Dict[str, Any]] = {c: {} for c in COINS}
        self.cur_bid: Dict[str, float] = {}
        self.wallet: Dict[str, Any] = {"configured": False, "balance_usdc": None, "allowance_ok": None, "error": None, "checked_ts": 0}
        self.probe: Dict[str, Any] = {"latency_ms": None, "ok": None, "ts": 0.0}
        self.proxy_host: Optional[str] = None
        self.started_at = time.time()
        self.settings_mtime = config.settings_mtime()
        self.loop_ms = 0.0
        self.mode_request: Optional[Dict[str, Any]] = None  # última petición de modo del TUI y su resultado
        self.keys_present = False  # hay clave privada (y Safe si hace falta) en el .env
        self.boot_cfg: Dict[str, Any] = dict(CFG)  # ajustes con los que arrancó (para saber qué exige reiniciar)
        self.applied_cfg: Dict[str, Any] = dict(CFG)  # últimos ajustes aplicados
        self.restart_pending: Dict[str, str] = {}  # clave -> etiqueta: cambios guardados que solo se aplican al reiniciar
        self.settings_reload: Optional[Dict[str, Any]] = None  # {"ts", "changed", "hot", "restart"} del último recargado
        self.env_mtime = _env_mtime()
        self.boot_proxy = ""  # POLYMARKET_PROXY_LIST con la que arrancó (el proxy se fija al arrancar)
        self.client_fp = ""  # huella (sha256) de clave/cartera/claves CLOB con la que se creó rt.client
        self.feeds: Any = None  # FeedWriter (para cambiar las monedas en caliente)


def _env_mtime() -> float:
    try:
        return ENV_PATH.stat().st_mtime
    except OSError:
        return 0.0


def secrets_fingerprint(env: Dict[str, str]) -> str:
    """Huella (sha256) de lo que usa el cliente CLOB. Solo sirve para saber si ha cambiado; nunca se muestra ni guarda."""
    import hashlib

    parts = [env.get(k, "") for k in ("POLY_PRIVATE_KEY", "POLY_SAFE_ADDRESS", "POLY_FUNDER_ADDRESS", "POLY_CLOB_API_KEY",
                                      "POLY_CLOB_API_SECRET", "POLY_CLOB_API_PASSPHRASE", "POLY_CLOB_NONCE")]
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


# Ajustes que NO se pueden cambiar con el bot en marcha (se publican en status.restart_pending y el panel ofrece [R]).
RESTART_KEYS = ("POLY_SIGNATURE_TYPE", "CARO5M_PROXY_SCAN", "CHAIN_ID", "CLOB_HOST")
RESTART_LABELS = {"proxy": "Proxy", "keys": "Cartera / claves CLOB"}


def active_coins(state: Optional[Dict[str, Any]] = None) -> List[str]:
    """Monedas que el bucle recorre: las de COINS y, además, las quitadas que aún tienen una posición abierta
    (se siguen gestionando — TP/SL/timeout/venta manual — hasta cerrarla; nunca se abandonan)."""
    out = list(COINS)
    for c in (state or {}).get("positions") or {}:
        if c not in out and c in coins_mod.COIN_TABLE:
            out.append(c)
    return out


def apply_settings(cfg: Dict[str, Any]) -> None:
    """Aplica en caliente los ajustes hot (incluidas las monedas). Los de RESTART_KEYS los decide maybe_reload_settings."""
    global BET_SIZE, TP_OFFSET, SL_OFFSET, CB_LOSSES, CB_COOLDOWN, MIN_TOKEN, MAX_TOKEN, POSITION_TIMEOUT, AUTO_REDEEM, PM_PTB_ENABLE
    global COINS, COIN_SCALE, POLYGON_RPCS, BINANCE_HOST, GAMMA_HOST, CFG
    COINS = tuple(cfg["COINS"])
    COIN_SCALE = dict(cfg["COIN_SCALE"])
    POLYGON_RPCS = list(cfg["POLYGON_RPCS"]) or POLYGON_RPCS
    BINANCE_HOST = cfg["BINANCE_HOST"]
    GAMMA_HOST = cfg["GAMMA_HOST"]
    keep = {k: CFG[k] for k in ("DRY_RUN", *RESTART_KEYS) if k in CFG}  # el modo y lo de reinicio no cambian aquí
    CFG = {**cfg, **keep}
    BET_SIZE = float(cfg["BET_SIZE"])
    TP_OFFSET = float(cfg["TP_OFFSET"])
    SL_OFFSET = float(cfg["SL_OFFSET"])
    CB_LOSSES = int(cfg["CB_LOSSES"])
    CB_COOLDOWN = int(cfg["CB_COOLDOWN"])
    MIN_TOKEN = float(cfg["MIN_TOKEN"])
    MAX_TOKEN = float(cfg["MAX_TOKEN"])
    POSITION_TIMEOUT = int(cfg["POSITION_TIMEOUT"])
    AUTO_REDEEM = bool(cfg["AUTO_REDEEM"])
    PM_PTB_ENABLE = bool(cfg["CARO5M_USE_PTB"])


def _client_rebuildable(rt: Runtime) -> bool:
    """En PRUEBA el cliente CLOB no se usa para órdenes: se puede tirar y se recrea al pasar a REAL con [M]."""
    return rt.client is None or rt.mode == "dry"


def maybe_reload_settings(rt: Runtime, state: Optional[Dict[str, Any]] = None) -> List[str]:
    """Si cambió config/settings.env: aplica en caliente lo posible y apunta lo que exige reiniciar. Devuelve las claves cambiadas.

    El modo (PRUEBA/REAL) NUNCA cambia aquí: DRY_RUN solo decide el modo del próximo arranque; ahora se cambia con [M]."""
    global SIGNATURE_TYPE
    m = config.settings_mtime()
    if m == rt.settings_mtime:
        return []
    rt.settings_mtime = m
    cfg = config.load_settings()
    old = rt.applied_cfg
    changed = [k for k in cfg if cfg[k] != old.get(k)]
    old_coins = list(COINS)
    apply_settings(cfg)
    rt.applied_cfg = dict(cfg)
    restart: List[str] = []
    for k in RESTART_KEYS:
        if cfg[k] == rt.boot_cfg.get(k):
            rt.restart_pending.pop(k, None)
            continue
        if k == "POLY_SIGNATURE_TYPE" and _client_rebuildable(rt):
            SIGNATURE_TYPE = int(cfg[k])  # aún no hay cliente real: se usará el nuevo tipo de firma al pasar a REAL
            CFG[k] = cfg[k]
            rt.boot_cfg[k] = cfg[k]
            if rt.client is not None:
                rt.client = None
                log("SETTINGS tipo de firma cambiado en PRUEBA: el cliente CLOB se recreará al pasar a REAL")
            rt.restart_pending.pop(k, None)
            continue
        rt.restart_pending[k] = config.SPEC_BY_KEY[k].label
        if k in changed:
            restart.append(k)
    hot = [k for k in changed if k not in RESTART_KEYS and k != "DRY_RUN"]
    rt.settings_reload = {"ts": time.time(), "changed": changed, "hot": hot, "restart": restart}
    if changed:
        log(f"SETTINGS reloaded changed={','.join(changed)} hot={','.join(hot) or '-'} restart={','.join(restart) or '-'} "
            f"bet={BET_SIZE} tp={TP_OFFSET} sl={SL_OFFSET} cb={CB_LOSSES} range=[{MIN_TOKEN},{MAX_TOKEN}] coins={','.join(COINS)}")
    else:
        log("SETTINGS reloaded (sin cambios efectivos)")
    if "DRY_RUN" in changed:
        log(f"SETTINGS DRY_RUN solo decide el modo del próximo arranque; el modo actual ({MODE_NAMES.get(rt.mode, rt.mode)}) no cambia: usa [M]")
    if restart:
        log(f"CONTROL_WARN Reinicia el bot para aplicar: {', '.join(config.SPEC_BY_KEY[k].label for k in restart)}")
    if list(COINS) != old_coins:
        _apply_coins(rt, state or {}, old_coins)
    return changed


def _apply_coins(rt: Runtime, state: Dict[str, Any], old_coins: List[str]) -> None:
    added = [c for c in COINS if c not in old_coins]
    removed = [c for c in old_coins if c not in COINS]
    held = [c for c in removed if c in (state.get("positions") or {})]
    for c in added:
        rt.cs.setdefault(c, {})
        _COIN_HIST.setdefault(c, deque(maxlen=HIST_MAX_SAMPLES))
    if rt.feeds is not None and hasattr(rt.feeds, "set_coins"):
        rt.feeds.set_coins(active_coins(state))
    log(f"COINS hot-reload coins={','.join(COINS)}" + (f" añadidas={','.join(added)}" if added else "")
        + (f" quitadas={','.join(removed)}" if removed else "")
        + (f" (con posición abierta, se sigue gestionando hasta cerrarla: {','.join(held)})" if held else ""))


def maybe_reload_secrets(rt: Runtime) -> None:
    """Si cambió el .env (asistente del panel): decide qué se aplica solo y qué exige reiniciar. Nunca muestra valores."""
    m = _env_mtime()
    if m == rt.env_mtime:
        return
    rt.env_mtime = m
    env = read_secrets(quiet=True)
    rt.keys_present = keys_in_env(env)
    proxy = (env.get("POLYMARKET_PROXY_LIST") or "").strip()
    if proxy != rt.boot_proxy:
        if "proxy" not in rt.restart_pending:
            log("CONTROL_WARN Proxy cambiado en .env: reinicia el bot para aplicarlo (las órdenes siguen por la conexión con la que arrancó)")
        rt.restart_pending["proxy"] = RESTART_LABELS["proxy"]
    else:
        rt.restart_pending.pop("proxy", None)
    fp = secrets_fingerprint(env)
    if fp == rt.client_fp:
        rt.restart_pending.pop("keys", None)
        return
    if _client_rebuildable(rt):
        if rt.client is not None:
            log("SETTINGS cartera/claves cambiadas en PRUEBA: el cliente CLOB se recreará al pasar a REAL")
        rt.client = None
        rt.client_fp = fp
        rt.restart_pending.pop("keys", None)
    else:
        if "keys" not in rt.restart_pending:
            log("CONTROL_WARN Cartera/claves CLOB cambiadas con el bot en REAL: reinicia el bot para aplicarlas")
        rt.restart_pending["keys"] = RESTART_LABELS["keys"]


def _background_probes(rt: Runtime, env: Dict[str, str]) -> None:
    """Hilo: latencia del camino de órdenes (proxy) y comprobación de saldo/allowance (solo lectura)."""
    from . import checks

    last_wallet = 0.0
    while True:
        env = read_secrets(quiet=True)  # el asistente del TUI puede crear/cambiar el .env con el bot en marcha
        funder = funder_address(env)
        rt.keys_present = keys_in_env(env)
        try:
            t0 = time.time()
            from py_clob_client_v2.http_helpers import helpers as clob_helpers

            r = clob_helpers._http_client.get(f"{CLOB_HOST}/time", timeout=10)
            rt.probe = {"latency_ms": round((time.time() - t0) * 1000), "ok": r.status_code == 200, "ts": time.time()}
        except Exception as e:
            rt.probe = {"latency_ms": None, "ok": False, "ts": time.time(), "err": str(e)[:80]}
        if funder and time.time() - last_wallet > 60:
            last_wallet = time.time()
            try:
                rt.wallet = checks.check_wallet(funder, POLYGON_RPCS, CHAIN_ID, BET_SIZE)
            except Exception as e:
                rt.wallet = {"configured": True, "balance_usdc": None, "allowance_ok": None, "error": str(e)[:100], "checked_ts": time.time()}
        time.sleep(30)


def ensure_live(rt: Runtime, env: Dict[str, str]) -> Tuple[bool, str]:
    """Construye el cliente CLOB y exige comprobación previa de saldo/allowance. (ok, motivo)."""
    env = read_secrets(quiet=True)
    funder = funder_address(env)
    if not env.get("POLY_PRIVATE_KEY") or (SIGNATURE_TYPE != 0 and not funder):
        return False, "faltan clave privada o dirección Safe: usa el asistente de cuenta (panel, tecla A)"
    from . import checks

    try:
        w = checks.check_wallet(funder or "", POLYGON_RPCS, CHAIN_ID, BET_SIZE) if funder else {}
    except Exception as e:
        return False, f"no se pudo comprobar el saldo ({str(e)[:60]})"
    rt.wallet = w or rt.wallet
    if w:
        if w.get("error"):
            return False, "no se pudo comprobar saldo/allowance: " + str(w["error"])[:80]
        if w.get("balance_usdc") is not None and w["balance_usdc"] < BET_SIZE * 1.03:
            return False, f"saldo insuficiente ({w['balance_usdc']:.2f} USDC; hacen falta ≥ {BET_SIZE * 1.03:.2f})"
        if w.get("allowance_ok") is False:
            return False, "falta la aprobación (allowance) de USDC para el exchange"
    if rt.client is None:
        try:
            rt.client = build_client(env)
            rt.client_fp = secrets_fingerprint(env)
        except Exception as e:
            return False, f"no se pudo crear el cliente CLOB: {str(e)[:100]}"
    return True, "ok"


def switch_mode(rt: Runtime, state: Dict[str, Any], new_mode: str) -> Dict[str, Any]:
    save_state(state)
    rt.mode = new_mode
    set_state_mode(new_mode)
    st = load_state()
    log(f"MODE switched to {new_mode.upper()} positions={list(st.get('positions', {}))} loss_streak={st.get('loss_streak')}")
    return st


MODE_NAMES = {"live": "REAL", "dry": "PRUEBA"}


def _mode_result(rt: Runtime, target: str, result: str, reason: str = "") -> None:
    """Publica en status.json el resultado de la petición de modo (el TUI lo muestra tras pulsar [M])."""
    rt.mode_request = {"target": target, "result": result, "reason": reason, "ts": time.time()}


def handle_controls(rt: Runtime, env: Dict[str, str], state: Dict[str, Any]) -> Dict[str, Any]:
    rt.paused = status_mod.is_paused()
    req = status_mod.get_mode_request()
    if req:
        status_mod.clear_mode_request()
        log(f"CONTROL mode request received -> {req} (actual: {rt.mode})")
        name = MODE_NAMES.get(req, req)
        if req == rt.mode:
            _mode_result(rt, req, "ok", f"el bot ya estaba en modo {name}")
            log(f"MODE unchanged: ya estaba en {req.upper()}")
        elif state.get("positions"):
            why = "hay una posición abierta (ciérrala antes)"
            _mode_result(rt, req, "refused", why)
            log(f"CONTROL_WARN No se cambió al modo {name}: {why}")
        elif req == "live":
            ok, why = ensure_live(rt, env)
            if ok:
                state = switch_mode(rt, state, "live")
                _mode_result(rt, "live", "ok", "modo REAL activado")
            else:
                _mode_result(rt, "live", "refused", why)
                log(f"LIVE_UNAVAILABLE No se activó el modo REAL: {why}")
        else:
            state = switch_mode(rt, state, "dry")
            _mode_result(rt, "dry", "ok", "modo PRUEBA activado")
    sell = status_mod.consume_sell()
    if sell:
        rt.sell_coins = set(sell.get("coins") or active_coins(state))
        log(f"CONTROL sell_now requested coins={sorted(rt.sell_coins)}")
    return state


def today_stats(state: Dict[str, Any], now: float) -> Dict[str, Any]:
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(CFG["CARO5M_TZ"])
        start = datetime.fromtimestamp(now, tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    except Exception:
        start = now - (now % 86400)
    pnl = 0.0
    wins = losses = 0
    for t in state.get("trades", []):
        if float(t.get("ts_close") or 0) >= start and t.get("pnl") is not None:
            pnl += float(t["pnl"])
            if t["pnl"] > 0:
                wins += 1
            elif t["pnl"] < 0:
                losses += 1
    return {"pnl": round(pnl, 4), "wins": wins, "losses": losses}


def record_trade(state: Dict[str, Any], pos: Dict[str, Any], exit_price: Optional[float], result: str, now: float) -> None:
    entry = float(pos.get("price") or 0)
    size = float(pos.get("size") or 0)
    pnl = None if exit_price is None else round((float(exit_price) - entry) * size, 4)
    state.setdefault("trades", []).append(
        {
            "ts_open": pos.get("ts"), "ts_close": now, "coin": pos.get("coin"), "side": pos.get("side"),
            "entry": entry, "exit": exit_price, "size": size, "pnl": pnl, "result": result,
            "dry": bool(pos.get("dry")), "slug": pos.get("slug"),
        }
    )
    state["trades"] = state["trades"][-200:]


def push_order(state: Dict[str, Any], rec: Dict[str, Any]) -> None:
    state.setdefault("last_orders", []).append(rec)
    state["last_orders"] = state["last_orders"][-MAX_LAST_ORDERS:]


def ptb_file_quality(coin: str, now: float) -> Tuple[str, Optional[float]]:
    if not PM_PTB_ENABLE:
        return "off", None
    if os.path.exists(PM_PTB_KILL_FILE):
        return "off", None
    try:
        with open(PM_PTB_PATH, encoding="utf-8") as f:
            d = json.load(f)
        rec = (d.get("corrected") or {}).get(coin) or {}
        age = now - float(d.get("updated") or 0)
        return str(rec.get("c2_quality") or "none"), age
    except Exception:
        return "none", None


def build_status(rt: Runtime, state: Dict[str, Any], now: float, window_iv: int) -> Dict[str, Any]:
    coins: Dict[str, Any] = {}
    health_cl = health_bn = None
    ptb_q: Dict[str, Any] = {}
    for c in active_coins(state):
        d = dict(rt.cs.get(c, {}))
        q, qage = ptb_file_quality(c, now)
        ptb_q[c] = {"quality": q, "file_age": qage}
        if d.get("cl_age") is not None:
            health_cl = d["cl_age"] if health_cl is None else max(health_cl, d["cl_age"])
        if d.get("bn_age") is not None:
            health_bn = d["bn_age"] if health_bn is None else max(health_bn, d["bn_age"])
        coins[c] = d
    positions: Dict[str, Any] = {}
    for c, pos in (state.get("positions") or {}).items():
        entry = float(pos.get("price") or 0)
        bid = rt.cur_bid.get(c)
        pending = bool(pos.get("pending_redeem"))
        positions[c] = {
            "coin": c, "side": pos.get("side"), "entry": entry, "bid": bid,
            "tp": tp_target(entry), "sl": sl_target(entry), "tp_capped": entry + TP_OFFSET >= MAX_PRICE,
            "pct": ((bid - entry) / entry * 100.0) if (bid is not None and entry) else None,
            "age": now - float(pos.get("ts") or now), "size": pos.get("size"), "dry": bool(pos.get("dry")),
            "slug": pos.get("slug"), "timeout": POSITION_TIMEOUT,
            "pending_redeem": pending,
            "claim_hint": redeem.PENDING_CLAIM_MSG if pending else None,
        }
    trades = []
    for t in (state.get("trades") or [])[-8:][::-1]:
        trades.append(t)
    w = dict(rt.wallet or {})
    keys_ok = bool(w.get("configured"))
    keys_present = bool(rt.keys_present or keys_ok)
    return {
        "v": 1, "ts": now, "pid": os.getpid(), "version": __version__, "started_at": rt.started_at,
        "tz": CFG["CARO5M_TZ"], "mode": rt.mode, "paused": rt.paused,
        "cooldown_until": float(state.get("cooldown_until") or 0), "loss_streak": int(state.get("loss_streak") or 0),
        "cb_losses": CB_LOSSES, "bet_size": BET_SIZE,
        "settings": {"tp": TP_OFFSET, "sl": SL_OFFSET, "min_token": MIN_TOKEN, "max_token": MAX_TOKEN, "timeout": POSITION_TIMEOUT},
        "signature_type": SIGNATURE_TYPE,
        "wallet": {"configured": keys_ok, "balance_usdc": w.get("balance_usdc"), "allowance_ok": w.get("allowance_ok"),
                   "error": w.get("error"), "checked_ts": w.get("checked_ts"), "signature_type": SIGNATURE_TYPE},
        "live_available": bool(rt.client is not None or keys_ok),
        "keys_present": keys_present,  # hay clave (y Safe): el modo real se puede pedir aunque el saldo aún no se haya leído
        "mode_request": rt.mode_request,
        "coins_enabled": list(COINS),
        "coins_managing": [c for c in active_coins(state) if c not in COINS],  # quitadas con posición abierta (solo gestión)
        "restart_pending": [{"key": k, "label": v} for k, v in rt.restart_pending.items()],
        "settings_reload": rt.settings_reload,
        "coins_degraded": coins_mod.degraded_coins(COINS),
        "window": {"start": window_iv, "end": window_iv + 300},
        "coins": coins, "positions": positions, "today": today_stats(state, now), "trades": trades,
        "health": {
            "chainlink_age": health_cl, "binance_age": health_bn, "ptb": ptb_q,
            "proxy": {"configured": bool(rt.proxy_host), "latency_ms": rt.probe.get("latency_ms"), "ok": rt.probe.get("ok")},
            "clob_ok": HEALTH.get("clob_ok"), "gamma_ok": HEALTH.get("gamma_ok"), "loop_ms": rt.loop_ms,
        },
        "warnings": _status_warnings(now, positions),
    }


def _status_warnings(now: float, positions: Dict[str, Any]) -> list:
    out = list(WARNINGS)[-12:][::-1]
    for c, p in positions.items():
        if p.get("pending_redeem"):
            out.insert(0, {"ts": now, "level": "warn", "text": f"{c.upper()}: {redeem.PENDING_CLAIM_MSG}", "count": 1})
    return out


def _set_sig(cs: Dict[str, Any], ok: bool, side: Optional[str], word: str, reason: str = "", ask: Optional[float] = None) -> None:
    cs["signal"] = {"ok": ok, "side": side, "word": word, "reason": str(reason)[:120], "ask": ask}


def do_sell(rt: Runtime, state: Dict[str, Any], coin: str, pos: Dict[str, Any], token: str, bid: float, kind: str, now: float,
            result: str = "MANUAL") -> bool:
    """Vende la posición (FOK). Devuelve True si se ejecutó. Actualiza estado/trades; NO toca loss_streak."""
    pdry = bool(pos.get("dry"))
    size = float(pos.get("size") or 0)
    resp = place_fok(rt.client, token, SELL, max(bid, 0.01), size, dry=pdry)
    filled = order_filled(resp)
    log(f"{kind} {coin.upper()} {pos.get('side')} bid={bid:.3f} filled={filled} status={resp.get('status')}{' DRY_RUN' if pdry else ''}")
    push_order(state, {"ts": now, "kind": kind.lower(), "coin": coin, "filled": filled})
    if filled:
        record_trade(state, pos, bid, result, now)
        state["positions"].pop(coin, None)
    save_state(state)
    return filled


def _lookup_resolution(session: requests.Session, pos: Dict[str, Any], market: Optional[dict]) -> Tuple[str, Optional[dict]]:
    """('open'|'won'|'lost'|'unknown', market_info). market puede ser el de discover (solo si aún acepta órdenes)."""
    info = None
    slug = (market or {}).get("slug") or pos.get("slug") or ""
    if market and market.get("condition_id"):
        info = market
    if slug and (info is None or not info.get("closed")):
        try:
            got = fetch_market_by_slug(session, slug)
            if got:
                info = got
        except Exception as e:
            log(f"RESOLVE_LOOKUP_ERR {pos.get('coin', '?')}: {e}")
    if not info:
        return "unknown", None
    return redeem.resolution_for_side(info.get("raw") or info, pos.get("side") or ""), info


def _position_ids(pos: Dict[str, Any], info: Optional[dict]) -> Tuple[str, int]:
    cid = pos.get("condition_id") or (info or {}).get("condition_id") or ""
    if pos.get("index_set"):
        idx = int(pos["index_set"])
    else:
        idx = redeem.index_set_for_side(pos.get("side") or "", (info or {}).get("outcomes"))
    return str(cid), idx


def _holder_address(env: dict, sig: int) -> str:
    if sig == 0:
        pk = env.get("POLY_PRIVATE_KEY") or ""
        if pk:
            try:
                from . import wizard
                return wizard.signer_address(pk) or ""
            except Exception:
                return ""
        return ""
    return funder_address(env)


def close_on_timeout(rt: Runtime, state: Dict[str, Any], session: requests.Session,
                     coin: str, pos: Dict[str, Any], token: Optional[str], bid: Optional[float],
                     now: float, market: Optional[dict] = None) -> str:
    """Al vencer POSITION_TIMEOUT: intenta vender al mejor bid; si no, cobra o deja pendiente de cobrar.

    Devuelve: sold | redeemed | redeemed_ext | lost | pending | retry
    No descarta en silencio una posición ganadora.
    """
    side = pos.get("side")
    entry = float(pos.get("price") or 0)
    pdry = bool(pos.get("dry")) or rt.mode == "dry"
    already_pending = bool(pos.get("pending_redeem"))

    # 1) Venta agresiva al mejor bid (FOK de mercado), salvo si ya está marcada para cobrar.
    if not already_pending and bid is not None:
        if not pdry and rt.client is None:
            log("CONTROL_WARN posición real sin cliente CLOB: no se puede vender al vencer el tiempo")
        else:
            try:
                if do_sell(rt, state, coin, pos, token or "", bid, "EXIT_TIMEOUT", now, result="TIMEOUT"):
                    return "sold"
                log(f"EXIT_TIMEOUT_MISS {coin.upper()} {side} bid={bid:.3f} (sin fill; se mira si el mercado está resuelto)")
            except Exception as e:
                log(f"EXIT_TIMEOUT_ERR {coin.upper()}: {e}")
                push_order(state, {"ts": now, "coin": coin, "side": side, "error": str(e)[:200]})

    outcome, info = _lookup_resolution(session, pos, market)
    cid, index_set = _position_ids(pos, info)
    if info:
        if not pos.get("condition_id") and cid:
            pos["condition_id"] = cid
        if not pos.get("index_set"):
            pos["index_set"] = index_set

    if outcome == "open":
        log(f"TIMEOUT_WAIT {coin.upper()} {side} entry={entry:.3f} last_bid={None if bid is None else f'{bid:.3f}'} "
            "el mercado sigue abierto; se mantiene la posición")
        return "retry"

    if outcome == "lost":
        log(f"TIMEOUT_LOST {coin.upper()} {side} entry={entry:.3f} (mercado resuelto en contra; no hay nada que cobrar)")
        record_trade(state, pos, 0.0, "TIMEOUT", now)
        state["positions"].pop(coin, None)
        save_state(state)
        return "lost"

    if outcome != "won":
        log(f"TIMEOUT_UNKNOWN {coin.upper()} {side} no se confirma el resultado; se mantiene la posición")
        return "retry"

    # Ganadora resuelta: ¿ya está cobrada en la cadena?
    env = read_secrets()
    holder = _holder_address(env, SIGNATURE_TYPE)
    token_id = token or pos.get("token_id") or ""
    if holder and token_id and not pdry:
        bal = redeem.ctf_balance(holder, token_id, POLYGON_RPCS, CHAIN_ID)
        if bal == 0:
            log(f"REDEEM_EXT {coin.upper()} {side} saldo CTF=0 (cobrada fuera, p. ej. en polymarket.com)")
            record_trade(state, pos, 1.0, "REDEEM", now)
            state["positions"].pop(coin, None)
            save_state(state)
            return "redeemed_ext"

    if info and info.get("neg_risk"):
        log(f"REDEEM_SKIP {coin.upper()} mercado neg-risk: no se cobra on-chain automáticamente")
        pos["pending_redeem"] = True
        save_state(state)
        log(f"REDEEM_PENDING {coin.upper()} {side} {redeem.PENDING_CLAIM_MSG}")
        return "pending"

    can_onchain = redeem.redeem_supported(SIGNATURE_TYPE) and AUTO_REDEEM and cid
    if pdry and can_onchain:
        # No se pasa ninguna clave: redeem_eoa en dry no firma ni contacta la cadena.
        redeem.redeem_eoa("", cid or ("0x" + "00" * 32), index_set, [], dry=True)
        log(f"REDEEM_DRY {coin.upper()} {side} condition={cid or '?'} index_set={index_set} (simulado, no se envía nada)")
        record_trade(state, pos, 1.0, "REDEEM", now)
        state["positions"].pop(coin, None)
        save_state(state)
        return "redeemed"

    if can_onchain and not pdry:
        pk = env.get("POLY_PRIVATE_KEY") or ""
        if not pk:
            log(f"REDEEM_ERR {coin.upper()} no hay clave privada para cobrar (EOA)")
        else:
            log(f"REDEEM_ATTEMPT {coin.upper()} {side} condition={cid} index_set={index_set}")
            r = redeem.redeem_eoa(pk, cid, index_set, POLYGON_RPCS, CHAIN_ID, dry=False)
            if r.get("ok"):
                log(f"REDEEM {coin.upper()} {side} tx={r.get('tx')} status={r.get('status')}")
                record_trade(state, pos, 1.0, "REDEEM", now)
                state["positions"].pop(coin, None)
                save_state(state)
                return "redeemed"
            log(f"REDEEM_ERR {coin.upper()} {r.get('error') or r.get('status')}")

    # Safe / Magic, AUTO_REDEEM=0, o fallo: no se descarta; se avisa para cobrar en la web.
    pos["pending_redeem"] = True
    save_state(state)
    why = ("cartera Safe/Magic: cobra en polymarket.com" if not redeem.redeem_supported(SIGNATURE_TYPE)
           else "AUTO_REDEEM=0" if not AUTO_REDEEM
           else "falta condition_id" if not cid
           else "no se pudo enviar el cobro")
    log(f"REDEEM_PENDING {coin.upper()} {side} {redeem.PENDING_CLAIM_MSG} ({why})")
    return "pending"


# ── main loop ──────────────────────────────────────────────────────
def main() -> None:
    config.data_dir().mkdir(parents=True, exist_ok=True)
    config.control_dir().mkdir(parents=True, exist_ok=True)
    os.makedirs(SHM_DIR, exist_ok=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log(f"=== caro5m {__version__} starting ===")  # CARO = Circuit-Armored Risk Optimizer; 5m = mercados de 5 minutos
    env = read_secrets()
    status_mod.clear_mode_request()  # el modo real se debe confirmar en cada arranque (o fijar DRY_RUN=0)
    mode = "dry" if CFG["DRY_RUN"] else "live"
    rt = Runtime(mode)
    rt.keys_present = keys_in_env(env)
    rt.boot_proxy = (env.get("POLYMARKET_PROXY_LIST") or "").strip()
    rt.client_fp = secrets_fingerprint(env)
    log(f"coins={','.join(COINS)}" + (f" degradadas(sin Chainlink en Polygon, precio Binance)={','.join(coins_mod.degraded_coins(COINS))}" if coins_mod.degraded_coins(COINS) else "")
        + (f" escala={COIN_SCALE}" if COIN_SCALE else ""))
    log(f"mode={'PRUEBA (DRY_RUN: no se envían órdenes)' if mode == 'dry' else 'EN VIVO (órdenes reales)'}")

    proxy_list = env.get("POLYMARKET_PROXY_LIST", "")
    if proxy_list.strip():
        proxy = pick_proxy(proxy_list)
        rt.proxy_host = urlparse(proxy).hostname or "?"
        log(f"scan=direct order_proxy={rt.proxy_host}")
        apply_proxy(proxy)
    else:
        log("scan=direct order_proxy=none (sin proxy: las órdenes salen por la IP de este servidor)")

    feeds = FeedWriter()
    rt.feeds = feeds
    feeds.start()
    time.sleep(3)  # give feeds a moment so first SCAN is fresh

    session = requests.Session()
    session.headers.update({"User-Agent": "caro5m/" + __version__})  # SCAN / gamma / CLOB book / feeds: directo
    if proxy_list.strip() and CFG["CARO5M_PROXY_SCAN"]:  # opcional: lecturas de Gamma y libro también por el proxy (feeds siguen directos)
        session.proxies.update({"http": proxy, "https": proxy})
        log("CARO5M_PROXY_SCAN=1: Gamma y libro de órdenes salen por el proxy")

    if mode == "live":
        ok, why = ensure_live(rt, env)
        if not ok:
            raise RuntimeError(f"modo real no disponible: {why}")
    set_state_mode(mode)
    state = load_state()
    log(f"state positions={list(state.get('positions', {}))} loss_streak={state.get('loss_streak')}")
    threading.Thread(target=_background_probes, args=(rt, env), daemon=True, name="caro5m-probes").start()

    while True:
        loop_t0 = time.time()
        now = time.time()
        now_i = int(now)
        window_iv = now_i - (now_i % 300)
        try:
            maybe_reload_settings(rt, state)
            maybe_reload_secrets(rt)
            state = handle_controls(rt, env, state)

            # ── venta manual ([S] del TUI): no depende de Gamma ni del cooldown ──
            for coin in sorted(rt.sell_coins):
                pos = state.get("positions", {}).get(coin)
                if not pos:
                    continue
                if not pos.get("dry") and rt.client is None:
                    log("CONTROL_WARN venta manual imposible: modo real sin cliente CLOB")
                    continue
                try:
                    token = pos.get("token_id")
                    bid, _a = best_bid_ask(session, token) if token else (None, None)
                    if bid is None:
                        log(f"SELL_ERR {coin.upper()} sin precio de compra (bid) para vender")
                        continue
                    if not do_sell(rt, state, coin, pos, token, bid, "SELL_MANUAL", now):
                        log(f"SELL_ERR {coin.upper()} la venta manual no se ejecutó; reintenta con [S]")
                except Exception as e:
                    log(f"SELL_ERR {coin.upper()}: {e}")
            rt.sell_coins = set()

            # Freno (CB): bloquea SOLO las entradas nuevas. Las posiciones abiertas se siguen gestionando (TP/SL/timeout).
            cd_left = float(state.get("cooldown_until") or 0) - now
            if cd_left > 0 and now_i % 30 < 3:
                held = sorted(state.get("positions", {}))
                log(f"COOLDOWN {int(cd_left)}s left (loss_streak={state.get('loss_streak')}) solo bloquea entradas nuevas; gestionando posiciones={held}")

            loop_coins = active_coins(state)
            if hasattr(feeds, "set_coins"):
                feeds.set_coins(loop_coins)  # una moneda quitada deja de tener feed cuando se cierra su posición
            for c in [c for c in rt.cs if c not in loop_coins]:
                rt.cs.pop(c, None)
                log(f"COINS {c.upper()} retirada: sin posición abierta, deja de vigilarse")
            for coin in loop_coins:
                cs = rt.cs.setdefault(coin, {})
                cs.update({"updated": now, "market": None, "up_bid": None, "up_ask": None, "dn_bid": None, "dn_ask": None})
                _set_sig(cs, False, None, "datos", "starting")
                pos = state.get("positions", {}).get(coin)
                cl = read_json(f"{SHM_DIR}/chainlink_{coin}.json")
                bn = read_json(f"{SHM_DIR}/binance_{coin}.json")
                if cl:
                    cs.update(
                        chainlink=cl.get("price"), ptb=cl.get("open_twap_5m"), ptb_src=cl.get("ptb_src"),
                        delta_pct=cl.get("delta_pct"), delta_twap=cl.get("delta_twap_pct"),
                        cl_age=max(float(cl.get("stale_secs") or 0), feed_age(float(cl.get("updated_at") or cl.get("ts") or 0), now)),
                        secs_into=cl.get("secs_into"), secs_left=cl.get("secs_remaining"),
                    )
                if bn:
                    cs["bn_age"] = feed_age(float(bn.get("ts") or 0), now)
                    cs["binance"] = bn.get("price")
                if cl:
                    cs["feed_src"] = cl.get("price_src") or "chainlink"
                if not cl or not bn:
                    log(f"SCAN {coin.upper()} missing_feeds cl={bool(cl)} bn={bool(bn)}")
                    _set_sig(cs, False, None, "datos", "missing_feeds")
                    continue
                # umbrales relativos (%); escala opcional por moneda (COIN_SCALE). 1.0 = mismos objetos, sin cambios.
                cl, bn = coins_mod.scale_feeds(cl, bn, float(COIN_SCALE.get(coin, 1.0)))
                if not pos and now < float(state.get("cooldown_until") or 0):
                    # comprobado aquí (no al inicio del ciclo) para que un freno activado por la otra moneda en este mismo ciclo también bloquee
                    _set_sig(cs, False, None, "cooldown", "cooldown")
                    continue

                market = discover_market(session, coin, now_i)
                if not market:
                    log(f"SCAN {coin.upper()} no_market")
                    _set_sig(cs, False, None, "datos", "no_market")
                    continue
                cs["market"] = market["slug"]

                up_bid, up_ask = best_bid_ask(session, market["up_token"])
                dn_bid, dn_ask = best_bid_ask(session, market["down_token"])
                cs.update(up_bid=up_bid, up_ask=up_ask, dn_bid=dn_bid, dn_ask=dn_ask)
                if up_ask is None or dn_ask is None:
                    log(f"SCAN {coin.upper()} slug={market['slug']} book_missing")
                    _set_sig(cs, False, None, "datos", "book_missing")
                    continue

                mid_spread = 1.0 - (up_ask + dn_ask)

                if pos:
                    side = pos["side"]
                    entry = float(pos["price"])
                    token = pos.get("token_id") or (
                        market["up_token"] if side == "UP" else market["down_token"]
                    )
                    pdry = bool(pos.get("dry"))
                    bid, _ask = best_bid_ask(session, token)
                    if bid is None:
                        log(f"HOLD {coin.upper()} {side} entry={entry:.3f} no_bid")
                        continue
                    rt.cur_bid[coin] = bid
                    _set_sig(cs, False, side, "posición", "in_position")
                    tp = tp_target(entry)
                    sl = sl_target(entry)
                    if entry + TP_OFFSET >= MAX_PRICE and not pos.get("tp_cap_logged"):
                        pos["tp_cap_logged"] = True
                        log(f"TP_CAP {coin.upper()} {side} entry={entry:.3f} tp={entry + TP_OFFSET:.3f} >= {MAX_PRICE}: "
                            f"objetivo limitado a {MAX_PRICE} (se vende si el bid llega a {MAX_PRICE}; si no, se mantiene "
                            "hasta la resolución o el tiempo máximo)")
                    age = now - float(pos.get("ts") or now)
                    if age > POSITION_TIMEOUT or pos.get("pending_redeem"):
                        tok = token
                        close_on_timeout(rt, state, session, coin, pos, tok, bid, now, market)
                        continue
                    if not pdry and rt.client is None:
                        log("CONTROL_WARN posición real sin cliente CLOB: no se puede vender")
                        continue
                    if bid >= tp:
                        try:
                            size = float(pos.get("size") or 0)
                            resp = place_fok(rt.client, token, SELL, min(bid, MAX_PRICE), size, dry=pdry)
                            filled = order_filled(resp)
                            log(f"EXIT_TP {coin.upper()} {side} bid={bid:.3f} tp={tp:.3f} filled={filled} status={resp.get('status')}{' DRY_RUN' if pdry else ''}")
                            push_order(state, {"ts": now, "kind": "exit_tp", "coin": coin, "filled": filled})
                            if filled:
                                state["loss_streak"] = 0
                                record_trade(state, pos, min(bid, MAX_PRICE), "TP", now)
                                state["positions"].pop(coin, None)
                                save_state(state)
                        except Exception as e:
                            log(f"EXIT_TP_ERR {coin.upper()}: {e}")
                            push_order(state, {"ts": now, "coin": coin, "side": side, "error": str(e)[:200]})
                        continue
                    if bid <= sl:
                        if age < SL_MIN_HOLD_SECS:
                            log(
                                f"HOLD_SL_WAIT {coin.upper()} {side} age={age:.0f}s<{SL_MIN_HOLD_SECS} "
                                f"bid={bid:.3f} sl={sl:.3f} slug={market['slug']}"
                            )
                            continue
                        try:
                            size = float(pos.get("size") or 0)
                            resp = place_fok(rt.client, token, SELL, max(bid, 0.01), size, dry=pdry)
                            filled = order_filled(resp)
                            log(f"EXIT_SL {coin.upper()} {side} bid={bid:.3f} sl={sl:.3f} filled={filled} status={resp.get('status')}{' DRY_RUN' if pdry else ''}")
                            push_order(state, {"ts": now, "kind": "exit_sl", "coin": coin, "filled": filled})
                            if filled:
                                state["loss_streak"] = int(state.get("loss_streak") or 0) + 1
                                if state["loss_streak"] >= CB_LOSSES:
                                    state["cooldown_until"] = now + CB_COOLDOWN
                                    log(f"CIRCUIT_BREAKER streak={state['loss_streak']} cooldown={CB_COOLDOWN}s")
                                record_trade(state, pos, max(bid, 0.01), "SL", now)
                                state["positions"].pop(coin, None)
                                save_state(state)
                        except Exception as e:
                            log(f"EXIT_SL_ERR {coin.upper()}: {e}")
                            push_order(state, {"ts": now, "coin": coin, "side": side, "error": str(e)[:200]})
                        continue
                    log(
                        f"HOLD {coin.upper()} {side} entry={entry:.3f} bid={bid:.3f} tp={tp:.3f} sl={sl:.3f} slug={market['slug']}"
                    )
                    continue

                if coin not in COINS:  # moneda quitada en caliente: solo se gestiona su posición, nunca entradas nuevas
                    _set_sig(cs, False, None, "retirada", "coin_removed")
                    continue

                cl_iv = int(cl.get("interval_start") or 0)
                if cl_iv and int(market["interval"]) != cl_iv:
                    log(
                        f"SCAN {coin.upper()} slug={market['slug']} cl_iv={cl_iv} mkt_iv={market['interval']} | interval_mismatch"
                    )
                    _set_sig(cs, False, None, "ventana", "interval_mismatch")
                    continue

                record_market_tick(
                    coin,
                    now,
                    float(bn.get("change_1m") or 0.0),
                    float(cl.get("delta_twap_pct") or 0.0),
                    float(up_ask or 0.0),
                    float(dn_ask or 0.0),
                )
                side, reason, meta = evaluate_signal(
                    coin, cl, bn, now, up_ask=up_ask, dn_ask=dn_ask
                )
                log(
                    f"SCAN {coin.upper()} slug={market['slug']} "
                    f"secs={meta.get('secs_elapsed', 0):.0f}/{meta.get('secs_left', 0):.0f} "
                    f"d_twap={float(cl.get('delta_twap_pct') or 0):+.4f} "
                    f"d_spot={float(cl.get('delta_pct') or 0):+.4f} "
                    f"bin1m={float(bn.get('change_1m') or 0):+.4f} "
                    f"rsi={float(bn.get('rsi14') or 0):.1f} "
                    f"up_ask={up_ask:.3f} dn_ask={dn_ask:.3f} spread={mid_spread:+.3f} "
                    f"| {reason}"
                )

                if side is None:
                    _set_sig(cs, False, None, messages.short_reason(reason), reason)
                    continue
                if rt.paused:
                    log(f"SKIP {coin.upper()} {side} bot en pausa")
                    _set_sig(cs, False, side, "pausa", "paused")
                    continue
                if mid_spread > SPREAD_SKIP:
                    log(f"SKIP {coin.upper()} wide_spread {mid_spread:.3f}")
                    _set_sig(cs, False, side, "spread", "wide_spread")
                    continue

                ask = up_ask if side == "UP" else dn_ask
                token = market["up_token"] if side == "UP" else market["down_token"]
                if ask is None or not (MIN_TOKEN <= ask <= MAX_TOKEN):
                    log(f"SKIP {coin.upper()} ask={ask} not in [{MIN_TOKEN},{MAX_TOKEN}]")
                    _set_sig(cs, False, side, "precio", "ask_range", ask)
                    continue

                lane = meta.get("lane") or ("A" if str(reason).startswith("SIGNAL_LANE_A") else "B")
                if lane == "A":
                    if ask > LANE_A_MAX_ASK:
                        log(
                            f"SKIP {coin.upper()} lane_a_ask ask={ask:.3f}>{LANE_A_MAX_ASK}"
                        )
                        _set_sig(cs, False, side, "precio", "lane_a_ask", ask)
                        continue
                    secs_now = float(meta.get("secs_elapsed") or 0)
                    if secs_now > LANE_A_SECS_MAX:
                        log(
                            f"SKIP {coin.upper()} lane_a_late secs={secs_now:.0f}>{LANE_A_SECS_MAX}"
                        )
                        _set_sig(cs, False, side, "ventana", "lane_a_late", ask)
                        continue

                # Selective: ask in (0.62, MAX] OK if |d|≥0.10 + same-sign bin1m≥0.03
                # (today's audit: 3/3 HIT in this band; hard |d|≥0.15 starved fills)
                abs_d_now = abs(float(meta.get("delta_twap") or cl.get("delta_twap_pct") or 0))
                bin1m_now = float(bn.get("change_1m") or 0)
                if lane != "A" and ask > RICH_ASK_THRESHOLD:
                    soft_delta = RICH_ASK_SOFT_DELTA_MIN
                    if abs_d_now < soft_delta:
                        log(
                            f"SKIP {coin.upper()} rich_ask_weak_delta ask={ask:.3f} "
                            f"|d|={abs_d_now:.4f}<{soft_delta}"
                        )
                        _set_sig(cs, False, side, "precio", "rich_ask_weak_delta", ask)
                        continue
                    need = RICH_ASK_BIN1M_MIN if side == "UP" else -RICH_ASK_BIN1M_MIN
                    ok_bin = (side == "UP" and bin1m_now >= need) or (
                        side == "DOWN" and bin1m_now <= need
                    )
                    if not ok_bin:
                        log(
                            f"SKIP {coin.upper()} rich_ask_weak_bin1m ask={ask:.3f} "
                            f"bin1m={bin1m_now:+.4f} need={'≥' if side=='UP' else '≤'}{need:+.2f}"
                        )
                        _set_sig(cs, False, side, "precio", "rich_ask_weak_bin1m", ask)
                        continue

                # P2-4: re-validate full gates on the tick that sends FOK
                side2, reason2, meta2 = evaluate_signal(
                    coin, cl, bn, now, up_ask=up_ask, dn_ask=dn_ask
                )
                if side2 != side:
                    log(
                        f"RECHECK_SKIP {coin.upper()} was={side} now={side2} "
                        f"reason={reason2}"
                    )
                    _set_sig(cs, False, side, "recheck", "recheck", ask)
                    continue
                reason = reason2
                meta = meta2

                _set_sig(cs, True, side, "ok", reason, ask)
                dry = rt.mode == "dry"
                if not dry and rt.client is None:
                    log("LIVE_UNAVAILABLE modo real sin cliente CLOB; no se envía la orden")
                    continue
                size = round(round(BET_SIZE, 2) / ask, 4)
                try:
                    log(f"ORDER_ATTEMPT BUY {coin.upper()} {side} ask={ask:.4f} size={size} usdc={BET_SIZE}{' DRY_RUN' if dry else ''}")
                    resp = place_fok(rt.client, token, BUY, ask, size, dry=dry)
                    filled = order_filled(resp)
                    status = resp.get("status") if isinstance(resp, dict) else None
                    log(f"ORDER_RESULT {coin.upper()} {side} filled={filled} status={status}{' DRY_RUN' if dry else ''}")
                    rec = {
                        "ts": now,
                        "coin": coin,
                        "side": side,
                        "price": ask,
                        "size": size,
                        "slug": market["slug"],
                        "entry_reason": reason,
                        "filled": filled,
                        "status": status,
                        "dry": dry,
                    }
                    push_order(state, rec)
                    if filled:
                        pos_rec = {
                            "coin": coin,
                            "side": side,
                            "price": ask,
                            "size": size,
                            "slug": market["slug"],
                            "ts": now,
                            "entry_reason": reason,
                            "token_id": token,
                            "dry": dry,
                            "condition_id": market.get("condition_id") or "",
                            "index_set": redeem.index_set_for_side(side, market.get("outcomes")),
                        }
                        state.setdefault("positions", {})[coin] = pos_rec
                        state.setdefault("fills", []).append(pos_rec)
                        state["fills"] = state["fills"][-100:]
                        log(f"FILL {coin.upper()} {side} @{ask:.4f} size={size} slug={market['slug']}{' DRY_RUN' if dry else ''}")
                    save_state(state)
                except Exception as e:
                    log(f"ORDER_ERR {coin.upper()} {side}: {e}")
                    push_order(state, {"ts": now, "coin": coin, "side": side, "error": str(e)[:200]})
                    save_state(state)

        except Exception as e:
            log(f"LOOP_ERR {e}")
            log(" | ".join(traceback.format_exc().splitlines()[-3:]))

        try:
            rt.loop_ms = (time.time() - loop_t0) * 1000
            status_mod.write_status(build_status(rt, state, time.time(), window_iv))
        except Exception as e:  # el TUI es accesorio: nunca tumba el bot
            log(f"LOOP_ERR status_write {e}")
        time.sleep(LOOP_SLEEP)


class _Stop(Exception):
    pass


def _install_stop_signals() -> None:
    """SIGTERM/SIGHUP (panel, instalador, systemd) y SIGBREAK (Windows): salida limpia con línea en el log.
    El estado (posiciones) ya está guardado en disco en cada cambio; al volver a arrancar se siguen gestionando."""

    def handler(signum, frame):  # noqa: ARG001
        raise _Stop(compat.signal_name(signum))

    compat.install_signal_handlers(handler)


def run() -> None:
    compat.ensure_utf8_stdio()
    _install_stop_signals()
    try:
        main()
    except _Stop as e:
        log(f"stopped by signal {e.args[0] if e.args else ''} (panel/instalador/servicio)")
    except KeyboardInterrupt:
        log("stopped by user")
    except Exception as e:
        log(f"FATAL {e}")
        log(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    run()
