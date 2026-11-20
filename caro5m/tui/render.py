"""Dibujo del panel (funciones puras: estado -> objetos Rich). Sin red, sin claves, fácil de probar."""

from __future__ import annotations

import shutil
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from rich.console import Group
from rich.table import Table
from rich.text import Text

from .. import coins as coins_mod, status as status_mod

DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
GREEN, YELLOW, RED, DIM = "green", "yellow", "red", "grey62"


def num(x: Optional[float], nd: int = 2, sign: bool = False) -> str:
    """Formato español: 1.234,56"""
    if x is None:
        return "—"
    s = f"{x:+,.{nd}f}" if sign else f"{x:,.{nd}f}"
    return s.replace(",", "§").replace(".", ",").replace("§", ".")


def money(x: Optional[float], sign: bool = False) -> str:
    return "—" if x is None else f"{num(x, 2, sign)} USDC"


def clock(now: float, tz: str) -> str:
    try:
        d = datetime.fromtimestamp(now, ZoneInfo(tz))
    except Exception:
        d = datetime.fromtimestamp(now)
        tz = "local"
    return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]} {d:%H:%M:%S} ({tz})"


def hhmm(ts: float, tz: str) -> str:
    try:
        return datetime.fromtimestamp(ts, ZoneInfo(tz)).strftime("%H:%M:%S")
    except Exception:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")


def mmss(secs: float) -> str:
    secs = max(0, int(secs))
    return f"{secs // 60:02d}:{secs % 60:02d}"


def bar(frac: float, width: int = 30, color: str = GREEN) -> Text:
    frac = max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    t = Text()
    t.append("█" * n, style=color)
    t.append("░" * (width - n), style=DIM)
    return t


def dot(color: str) -> Text:
    return Text("● ", style=f"bold {color}")


# ── cabecera ───────────────────────────────────────────────────────
def coin_list(st: Optional[Dict[str, Any]]) -> List[str]:
    """Monedas que opera el bot (status.coins_enabled; bots antiguos: claves de coins; sin datos: btc, eth)."""
    st = st or {}
    got = list(st.get("coins_enabled") or (st.get("coins") or {}).keys())
    got += [c for c in (st.get("coins_managing") or []) if c not in got]  # quitadas con posición abierta (solo gestión)
    return [str(c) for c in got] or list(coins_mod.DEFAULT_COINS)


def mode_badge(st: Optional[Dict[str, Any]], running: bool, starting: bool = False) -> Text:
    if starting and not running:
        return Text("● ARRANCANDO…", style="bold cyan")
    if not st or not running:
        return Text("● DETENIDO", style="bold red")
    if st.get("paused"):
        return Text("● PAUSADO · " + ("EN VIVO" if st.get("mode") == "live" else "PRUEBA"), style="bold dark_orange")
    if st.get("mode") == "live":
        return Text("● EN VIVO", style="bold green")
    return Text("● PRUEBA", style="bold yellow")


def header(st: Optional[Dict[str, Any]], running: bool, now: float, tz: str, width: int = 100, starting: bool = False) -> Text:
    left = Text(" caro5m ", style="bold white on blue")
    title = coins_mod.title(coin_list(st))
    if len(title) > 32:
        title = title[:31] + "…"
    left.append(f"  Polymarket 5m · {title}", style="bold")
    badge = mode_badge(st, running, starting)
    right = Text(clock(now, tz), style="bold")
    pad = max(2, width - len(left.plain) - len(badge.plain) - len(right.plain) - 4)
    t = Text()
    t.append_text(left)
    t.append(" " * (pad // 2))
    t.append_text(badge)
    t.append(" " * (pad - pad // 2))
    t.append_text(right)
    return t


BALANCE_LABELS_ES = {0: "Saldo cartera", 1: "Saldo cuenta (email)", 2: "Saldo Safe"}
BALANCE_LABELS_EN = {0: "Wallet balance", 1: "Account balance (email)", 2: "Safe balance"}


def balance_label(signature_type: Any = 2, lang: str = "es") -> str:
    """Etiqueta del saldo según el tipo de cartera (0 EOA, 1 Magic/email, 2 Safe)."""
    table = BALANCE_LABELS_EN if lang == "en" else BALANCE_LABELS_ES
    try:
        sig = int(signature_type)
    except (TypeError, ValueError):
        sig = 2
    return table.get(sig, table[2])


def status_line(st: Optional[Dict[str, Any]], now: float) -> Text:
    t = Text()
    if not st:
        t.append("Sin datos del bot todavía. ", style=DIM)
        t.append("Pulsa [B] para arrancar el bot o [A] para configurar la cuenta.", style=DIM)
        return t
    w = st.get("wallet") or {}
    bal = w.get("balance_usdc")
    sig = st.get("signature_type", w.get("signature_type", 2))
    t.append(balance_label(sig) + ": ", style=DIM)
    if bal is None:
        t.append("n/d" + (" (sin clave)" if not w.get("configured") else ""), style=DIM)
    else:
        t.append(money(bal), style="bold " + (RED if bal < float(st.get("bet_size") or 1) * 1.03 else GREEN))
    t.append("  │  Apuesta: ", style=DIM)
    t.append(money(st.get("bet_size")), style="bold")
    td = st.get("today") or {}
    pnl = float(td.get("pnl") or 0)
    t.append("  │  Hoy: ", style=DIM)
    t.append(money(pnl, True), style="bold " + (GREEN if pnl > 0 else RED if pnl < 0 else "white"))
    t.append(f" ({td.get('wins', 0)} ✔ / {td.get('losses', 0)} ✖)")
    t.append("  │  Racha: ", style=DIM)
    ls, cb = int(st.get("loss_streak") or 0), int(st.get("cb_losses") or 0)
    t.append(f"{ls}/{cb}", style="bold " + (RED if ls >= cb else YELLOW if ls else GREEN))
    cd = float(st.get("cooldown_until") or 0) - now
    if cd > 0:
        t.append(f"  ■ FRENO {mmss(cd)}", style="bold red")
    return t


# ── ventana actual ─────────────────────────────────────────────────
def _arrow(d: Optional[float]) -> Text:
    if d is None:
        return Text("—", style=DIM)
    if abs(d) < 1e-9:
        return Text("▬ 0,000%", style=DIM)
    up = d > 0
    return Text(f"{'▲' if up else '▼'} {num(d, 3, True)}%", style=f"bold {GREEN if up else RED}")


def _pair(bid: Optional[float], ask: Optional[float]) -> str:
    return f"{num(bid, 2)} / {num(ask, 2)}"


def _price(coin: str, x: Optional[float]) -> str:
    return num(x, coins_mod.display_decimals(coin, x))


def window_panel(st: Optional[Dict[str, Any]], now: float, tz: str, running: bool) -> Group:
    if not st:
        return Group(Text("Esperando datos…", style=DIM))
    win = st.get("window") or {}
    start, end = float(win.get("start") or 0), float(win.get("end") or 0)
    left = max(0.0, end - now)
    frac = 1 - left / 300.0 if end else 0
    head = Text()
    head.append(f"{hhmm(start, tz)} → {hhmm(end, tz)}   ", style="bold")
    head.append(f"faltan {mmss(left)}  ", style="bold cyan")
    head.append_text(bar(frac, 32, "cyan"))
    head.append(f" {int(frac * 100):3d}%")
    if not running:
        head.append(f"   (datos de hace {int(now - float(st.get('ts') or now))} s)", style="red")
    tb = Table(box=None, pad_edge=False, expand=True, show_edge=False)
    for col, just in (("Activo", "left"), ("PTB", "right"), ("Chainlink", "right"), ("Δ vs PTB", "right"),
                      ("UP  compra/venta", "center"), ("DOWN compra/venta", "center"), ("Señal", "left")):
        tb.add_column(col, justify=just, style="bold" if col == "Activo" else None, no_wrap=True)
    degraded = []
    for coin in coin_list(st):
        c = (st.get("coins") or {}).get(coin) or {}
        if c.get("feed_src") == "binance" or coin in (st.get("coins_degraded") or []):
            degraded.append(coin)
        sig = c.get("signal") or {}
        if sig.get("ok"):
            s = Text(f"✔ {sig.get('side')} {num(sig.get('ask'), 2)}", style="bold green")
        elif sig.get("side") and sig.get("word") not in ("posición",):
            s = Text(f"✖ {sig.get('word')} ", style="red")
            s.append(f"({sig.get('side')})", style=DIM)
        elif sig.get("word") == "posición":
            s = Text(f"● en posición {sig.get('side')}", style="cyan")
        else:
            s = Text(f"✖ {sig.get('word') or 'datos'}", style="yellow" if sig.get("word") in ("datos", "ventana", "cooldown", "pausa") else "red")
        if coin in (st.get("coins_managing") or []):
            s = Text("● solo gestión", style="cyan")  # moneda quitada: se gestiona su posición y se retira al cerrarla
        tb.add_row(coin.upper() + ("*" if coin in degraded else ""), _price(coin, c.get("ptb")), _price(coin, c.get("chainlink")), _arrow(c.get("delta_twap")),
                   _pair(c.get("up_bid"), c.get("up_ask")), _pair(c.get("dn_bid"), c.get("dn_ask")), s)
    parts: List[Any] = [head, Text(""), tb]
    managing = [c for c in (st.get("coins_managing") or [])]
    if managing:
        parts.append(Text(", ".join(c.upper() for c in managing) + ": moneda quitada de COINS; se sigue gestionando su posición "
                          "hasta cerrarla y luego se retira (sin entradas nuevas).", style="cyan"))
    if degraded:
        parts.append(Text("* " + ", ".join(c.upper() for c in degraded)
                          + ": sin Chainlink útil en Polygon, usa el precio de Binance (modo degradado).", style=DIM))
    return Group(*parts)


# ── posición abierta ───────────────────────────────────────────────
def pos_bar(entry: float, sl: float, tp: float, cur: Optional[float], width: int = 28) -> Text:
    """Barra SL ←──◆──→ TP con la posición actual."""
    t = Text()
    t.append(f"SL {num(sl, 2)} ", style=RED)
    if cur is None or tp <= sl:
        t.append("░" * width, style=DIM)
    else:
        frac = max(0.0, min(1.0, (cur - sl) / (tp - sl)))
        mid = (entry - sl) / (tp - sl)
        pos = int(round(frac * (width - 1)))
        midi = int(round(mid * (width - 1)))
        for i in range(width):
            if i == pos:
                t.append("◆", style="bold white")
            elif i == midi:
                t.append("│", style=DIM)
            elif i < midi:
                t.append("─", style=RED if i >= pos else "grey37")
            else:
                t.append("─", style=GREEN if i <= pos else "grey37")
    t.append(f" TP {num(tp, 2)}", style=GREEN)
    return t


def position_panel(st: Optional[Dict[str, Any]], now: float, tz: str) -> Group:
    pos = (st or {}).get("positions") or {}
    if not pos:
        return Group(Text("sin posición", style=DIM))
    rows: List[Any] = []
    if float((st or {}).get("cooldown_until") or 0) > now:
        rows.append(Text("■ Freno activo: no se abren entradas nuevas, pero se gestiona esta posición (TP/SL/tiempo).", style="yellow"))
    for coin, p in pos.items():
        pct = p.get("pct")
        col = GREEN if (pct or 0) > 0 else RED if (pct or 0) < 0 else "white"
        l1 = Text()
        l1.append(f"{coin.upper()} {p.get('side')}", style="bold")
        l1.append(f"  entrada {num(p.get('entry'), 2)}  ahora {num(p.get('bid'), 2)}  ")
        l1.append(f"{num(pct, 1, True)}%" if pct is not None else "—", style=f"bold {col}")
        l1.append(f"   en posición {mmss(p.get('age') or 0)} / máx {mmss(p.get('timeout') or 360)}", style="cyan")
        if p.get("dry"):
            l1.append("   (simulada)", style="yellow")
        rows.append(l1)
        if p.get("pending_redeem"):
            rows.append(Text(p.get("claim_hint") or "posición resuelta pendiente de cobrar en polymarket.com", style="bold yellow"))
        rows.append(pos_bar(float(p.get("entry") or 0), float(p.get("sl") or 0), float(p.get("tp") or 0), p.get("bid")))
    return Group(*rows)


# ── últimas operaciones ────────────────────────────────────────────
def trades_panel(st: Optional[Dict[str, Any]], tz: str) -> Any:
    trades = (st or {}).get("trades") or []
    if not trades:
        return Text("Sin operaciones todavía", style=DIM)
    tb = Table(box=None, pad_edge=False, show_edge=False, expand=True)
    for col, just in (("Hora", "left"), ("Activo", "left"), ("Entr→Sal", "right"), ("PnL", "right"), ("Res.", "left")):
        tb.add_column(col, justify=just, no_wrap=True)
    for t in trades[:6]:
        pnl = t.get("pnl")
        col = DIM if pnl is None else GREEN if pnl > 0 else RED if pnl < 0 else "white"
        res = str(t.get("result") or "")
        tb.add_row(hhmm(float(t.get("ts_close") or 0), tz), f"{str(t.get('coin') or '').upper()} {t.get('side')}",
                   f"{num(t.get('entry'), 2)}→{num(t.get('exit'), 2)}", Text(num(pnl, 2, True) if pnl is not None else "—", style=col),
                   Text(res + ("·sim" if t.get("dry") else ""), style=GREEN if res in ("TP", "REDEEM") else RED if res == "SL" else YELLOW))
    return tb


# ── estado del sistema ─────────────────────────────────────────────
def _grade(v: Optional[float], good: float, warn: float) -> str:
    if v is None:
        return DIM
    return GREEN if v <= good else YELLOW if v <= warn else RED


def disk_percent(path: str = "/") -> Optional[float]:
    try:
        u = shutil.disk_usage(path)
        return u.used / u.total * 100.0
    except Exception:
        return None


PTB_HOW = {"panel": "en marcha (panel{pid})", "systemd": "en marcha (servicio)", "externo": "en marcha (otra terminal)", "parado": "parado"}


def ptb_proc_text(pp: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """(color, texto) del proceso PTB según launcher.ptb_state()."""
    if not pp:
        return DIM, "—"
    how = str(pp.get("how") or "parado")
    txt = PTB_HOW.get(how, how).replace("{pid}", f", pid {pp['pid']}" if pp.get("pid") else "")
    return (GREEN if pp.get("running") else YELLOW), txt


def system_panel(st: Optional[Dict[str, Any]], now: float, running: bool, disk_pct: Optional[float],
                 ptb_proc: Optional[Dict[str, Any]] = None) -> Any:
    lines: List[Text] = []

    def row(color: str, label: str, value: str) -> None:
        t = dot(color)
        t.append(f"{label:<16}", style=DIM)
        t.append(value, style="bold")
        lines.append(t)

    if not st:
        row(RED, "Bot", "detenido / sin datos")
        if ptb_proc is not None:
            col, txt = ptb_proc_text(ptb_proc)
            row(col, "PTB proceso", txt)
    else:
        h = st.get("health") or {}
        row(GREEN if running else RED, "Bot", f"en marcha (pid {st.get('pid')})" if running else f"detenido (hace {int(now - float(st.get('ts') or now))} s)")
        cl = h.get("chainlink_age")
        row(_grade(cl, 30, 45), "Chainlink", "—" if cl is None else f"{cl:.0f} s")
        bn = h.get("binance_age")
        row(_grade(bn, 5, 10), "Binance", "—" if bn is None else f"{bn:.0f} s")
        ptb = h.get("ptb") or {}
        qs = {c: (v or {}).get("quality") for c, v in ptb.items()}
        vals = set(qs.values()) or {"none"}
        if vals == {"off"}:
            row(DIM, "PTB", "desactivado")
        else:
            worst = "ok" if vals <= {"ok"} else "degraded" if vals <= {"ok", "degraded"} else "none"
            row({"ok": GREEN, "degraded": YELLOW}.get(worst, YELLOW), "PTB (c2)", {"ok": "ok", "degraded": "degradado"}.get(worst, "sin datos (modo legado)"))
        if ptb_proc is not None:
            col, txt = ptb_proc_text(ptb_proc)
            row(col, "PTB proceso", txt)
        px = h.get("proxy") or {}
        if not px.get("configured"):
            lat = px.get("latency_ms")
            row(_grade(lat, 400, 1500) if px.get("ok") else YELLOW, "Red CLOB", f"directa, sin proxy · {lat} ms" if lat else "directa, sin proxy")
        elif px.get("ok") is False or px.get("latency_ms") is None:
            row(RED, "Proxy", "sin respuesta")
        else:
            row(_grade(px["latency_ms"], 400, 1500), "Proxy", f"{px['latency_ms']} ms")
        row(GREEN if h.get("clob_ok") else RED if h.get("clob_ok") is False else DIM, "CLOB", "ok" if h.get("clob_ok") else "error" if h.get("clob_ok") is False else "—")
        row(GREEN if h.get("gamma_ok") else RED if h.get("gamma_ok") is False else DIM, "Gamma", "ok" if h.get("gamma_ok") else "error" if h.get("gamma_ok") is False else "—")
    row(_grade(disk_pct, 80, 90), "Disco", "—" if disk_pct is None else f"{disk_pct:.0f} % usado")
    return Group(*lines)


# ── avisos ─────────────────────────────────────────────────────────
def restart_banner(st: Optional[Dict[str, Any]], running: bool = True) -> Optional[Text]:
    """Banda amarilla con los cambios guardados que solo se aplican al reiniciar (status.restart_pending)."""
    pend = [str(x.get("label")) for x in ((st or {}).get("restart_pending") or [])] if running else []
    if not pend:
        return None
    t = Text("⟳ Reinicia el bot para aplicarlo: ", style="bold black on yellow")
    t.append(", ".join(pend), style="bold black on yellow")
    t.append("  · pulsa [R] (mantiene el modo actual)", style="black on yellow")
    return t


def process_banner(st: Optional[Dict[str, Any]], running: bool, installed: str, procs: Optional[List[Any]] = None) -> Optional[Text]:
    """Banda amarilla: bot en marcha de otra versión (R lo reinicia), bots duplicados o un bot que no actualiza su estado."""
    lines: List[str] = []
    v = str((st or {}).get("version") or "")
    if running and v and v != installed:
        lines.append(f"⟳ El bot en marcha es la versión {v}; tienes instalada la {installed}. Pulsa R para reiniciarlo")
    own = [p for p in (procs or []) if getattr(p, "kind", "") == "bot" and not getattr(p, "systemd", False)]
    pids = ", ".join(str(p.pid) for p in own)
    if len(own) > 1:
        lines.append(f"⚠ Hay {len(own)} bots de esta carpeta en marcha a la vez (pid {pids}): pulsa B para pararlos todos")
    elif own and not running:
        lines.append(f"⚠ Hay un bot de esta carpeta en marcha (pid {pids}) que no actualiza su estado (¿versión antigua o "
                     "colgado?): pulsa B para pararlo")
    if not lines:
        return None
    return Text("\n".join(lines), style="bold black on yellow")


def warnings_panel(st: Optional[Dict[str, Any]], tz: str, limit: int = 4, running: bool = True, extra: Optional[Text] = None) -> Any:
    ws = (st or {}).get("warnings") or []
    banner = restart_banner(st, running)
    if extra is not None:
        banner = Text.assemble(extra, "\n", banner) if banner else extra
    if not ws:
        return Group(banner, Text("✔ Sin avisos", style=GREEN)) if banner else Text("✔ Sin avisos", style=GREEN)
    out: List[Text] = [banner] if banner else []
    for w in ws[:limit]:
        col = RED if w.get("level") == "error" else YELLOW
        t = Text()
        t.append(f"{hhmm(float(w.get('ts') or 0), tz)} ", style=DIM)
        t.append("✖ " if w.get("level") == "error" else "⚠ ", style=f"bold {col}")
        t.append(str(w.get("text")))
        if int(w.get("count") or 1) > 1:
            t.append(f"  (×{w['count']})", style=DIM)
        out.append(t)
    return Group(*out)


def footer_text(st: Optional[Dict[str, Any]], paused: bool, running: bool = True, restart: bool = False) -> Text:
    t = Text()
    items: List[Tuple[str, str]] = [("P", "Reanudar" if paused else "Pausar"), ("S", "Vender ya"), ("M", "Modo"),
                                    ("B", "Parar bot" if running else "Arrancar bot")]
    if running and ((st or {}).get("restart_pending") or restart):
        items.append(("R", "Reiniciar (pendiente)"))
    items += [("L", "Log del bot"), ("C", "Ajustes"),
              ("A", "Cuenta"), ("E", "Errores"), ("Q", "Salir"), ("^L", "Redibujar")]
    for k, label in items:
        t.append(f"[{k}]", style="bold black on grey70")
        t.append(f" {label}  ")
    return t
