"""PTB temprano para los mercados Up/Down de 5 min de Polymarket (monedas del ajuste COINS). SOLO LECTURA: sin claves, sin órdenes.

Proceso aparte (``python -m caro5m.ptb``) que adelanta la referencia «price to beat»:
  Fuente primaria: Polymarket live-data WS (topic crypto_prices_chainlink, <coin>/usd, 1 punto/s).
  c2(T) = media de la rejilla de 60 s con ts en [T-62, T-3] (huecos interpolados) -> se emite ~T-1,8 s.
  Fallback: TWAP-60 s de Binance (bookTicker WS) x USDT/USD de Coinbase (el bot NO usa el fallback).
Salidas: ``<data>/ptb/*.ndjson`` (auditoría) y el fichero compartido ``pm_ptb_latest.json`` (CARO5M_SHM_DIR o PM_PTB_SHM)
que lee el bot. Kill-switch sin reiniciar: crear ``pm_ptb_disable`` en la carpeta compartida.
El bot funciona igual (método legado) si este proceso no está en marcha.
"""
import asyncio, json, os, statistics, subprocess, sys, time, collections
import aiohttp, websockets

from . import coins as coins_mod, compat, config

OUT = str(config.ptb_out_dir()); os.makedirs(OUT, exist_ok=True)
COINS = list(config.load_settings()["COINS"])  # mismas monedas que el bot (ajuste COINS)
RTDS_SYMBOLS = {coins_mod.COIN_TABLE[c].rtds: c for c in COINS}
LOOKBACK = 60
DEADLINE_S = 2.5
STALE_S = 8.0
RESUB_GRACE_S = 3.0
MIN_PTS_OK = 58
C2_A, C2_B = -62, -3          # corrected window, inclusive ts offsets relative to T (60 one-second points)
C2_EARLY_S = 6.0              # launch corrected emitter this many seconds before T
UA = {"User-Agent": "caro5m-ptb/1.0 (read-only)"}
SEED_FB_BIAS = {c: 0.0 for c in COINS}

rt = {c: {} for c in COINS}
rt_last = {c: None for c in COINS}
mid = {c: [] for c in COINS}; bn_last = {c: None for c in COINS}
usdt = []
fb_bias = {c: collections.deque(maxlen=100) for c in COINS}
pending = {}
latest = {}
waiters = {}
pending_corr = {}
latest_corr = {}
corr_waiters = {}   # (coin, T) -> Event, set when a point with ts >= T+C2_B arrives
ws_holder = {"ws": None, "force_close": False, "reconnects": 0, "resubscribes": 0}

def now(): return time.time()
def wlog(name, rec):
    with open(f"{OUT}/{name}", "a", encoding="utf-8") as f: f.write(json.dumps(rec) + "\n")
def elog(w, e): wlog("errors.log", {"t": round(now(), 3), "where": w, "err": str(e)[:300]})
def atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, indent=1)
    compat.replace(tmp, path)

SUB = {"action": "subscribe", "subscriptions": [{"topic": "crypto_prices_chainlink", "type": "*", "filters": ""}]}

async def rtds_ws():
    backoff = 1.0
    while True:
        try:
            async with websockets.connect("wss://ws-live-data.polymarket.com", ping_interval=20, ping_timeout=20,
                                          close_timeout=5, max_size=2**22, proxy=None) as ws:
                ws_holder["ws"] = ws; ws_holder["force_close"] = False
                await ws.send(json.dumps(SUB))
                wlog("reconnect.log", {"event": "connected", "madrid": time.strftime("%Y-%m-%d %H:%M:%S"),
                                       "reconnects": ws_holder["reconnects"], "resubscribes": ws_holder["resubscribes"]})
                backoff = 1.0
                async def pinger():
                    while True:
                        await asyncio.sleep(5)
                        try: await ws.send("PING")
                        except Exception: return
                async def watchdog():
                    """Per-coin staleness: resubscribe once, then force-close for full reconnect."""
                    pending_force = {}  # coin -> when we resubscribed
                    while True:
                        await asyncio.sleep(1.0)
                        t = now()
                        for c in COINS:
                            age = None if rt_last[c] is None else t - rt_last[c]
                            if age is not None and age <= STALE_S:
                                pending_force.pop(c, None); continue
                            if age is None and ws_holder["reconnects"] == 0 and t - START_T < 15:
                                continue  # still warming up
                            if c not in pending_force:
                                try:
                                    await ws.send(json.dumps(SUB))
                                    ws_holder["resubscribes"] += 1
                                    pending_force[c] = t
                                    wlog("reconnect.log", {"event": "resubscribe", "coin": c, "age_s": None if age is None else round(age, 2),
                                                           "madrid": time.strftime("%Y-%m-%d %H:%M:%S"), "n": ws_holder["resubscribes"]})
                                except Exception as e:
                                    elog("resub", e); ws_holder["force_close"] = True; return
                            elif t - pending_force[c] >= RESUB_GRACE_S:
                                wlog("reconnect.log", {"event": "force_reconnect", "coin": c,
                                                       "age_s": None if age is None else round(age, 2),
                                                       "madrid": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                       "after_resub_s": round(t - pending_force[c], 2)})
                                ws_holder["force_close"] = True
                                try: await ws.close()
                                except Exception: pass
                                return
                pt = asyncio.create_task(pinger()); wd = asyncio.create_task(watchdog())
                try:
                    async for m in ws:
                        if ws_holder["force_close"]: break
                        if not m or m[0] != "{": continue
                        p = (json.loads(m).get("payload") or {}); s = p.get("symbol")
                        if s in RTDS_SYMBOLS and p.get("timestamp") is not None:
                            c = RTDS_SYMBOLS[s]; t = now(); ts = int(p["timestamp"]) // 1000
                            rt[c][ts] = (float(p["value"]), t); rt_last[c] = t
                            if len(rt[c]) > 1200:
                                for k in sorted(rt[c])[:600]: del rt[c][k]
                            ev = waiters.get((c, ts + 1))
                            if ev: ev.set()
                            for (cc, TT), cev in list(corr_waiters.items()):
                                if cc == c and ts >= TT + C2_B: cev.set()
                finally:
                    pt.cancel(); wd.cancel(); ws_holder["ws"] = None
        except Exception as e:
            elog("rtds_ws", e)
        ws_holder["reconnects"] += 1
        wlog("reconnect.log", {"event": "disconnected", "madrid": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "reconnects": ws_holder["reconnects"], "backoff_s": round(backoff, 1)})
        await asyncio.sleep(backoff)
        backoff = min(30.0, backoff * 1.5)

async def binance_ws():
    url = os.environ.get("PTB_BINANCE_WS", "wss://data-stream.binance.vision") + "/stream?streams=" + "/".join(
        f"{coins_mod.COIN_TABLE[c].binance.lower()}@bookTicker" for c in COINS)
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, proxy=None) as ws:
                async for m in ws:
                    t = now(); d = json.loads(m)["data"]; c = d["s"][:-4].lower()
                    if c not in mid: continue
                    bn_last[c] = t
                    v = (float(d["b"]) + float(d["a"])) / 2; L = mid[c]
                    if not L or L[-1][1] != v: L.append((t, v))
                    if L and t - L[0][0] > 900: mid[c] = [x for x in L if x[0] >= t - 600] or L[-1:]
        except Exception as e:
            elog("binance_ws", e); await asyncio.sleep(2)

async def usdt_poll(sess):
    while True:
        try:
            async with sess.get("https://api.exchange.coinbase.com/products/USDT-USD/ticker", headers=UA,
                                timeout=aiohttp.ClientTimeout(total=5)) as r:
                j = await r.json(content_type=None)
            if "bid" not in j: raise ValueError("coinbase: " + json.dumps(j)[:120])
            usdt.append((now(), (float(j["bid"]) + float(j["ask"])) / 2)); del usdt[:-50]
        except Exception as e: elog("usdt", e)
        await asyncio.sleep(15)

def twap(L, a, b):
    i = max((k for k in range(len(L)) if L[k][0] <= a), default=-1)
    if i < 0: return None
    area = 0.0; t0, v = a, L[i][1]
    for t, x in L[i + 1:]:
        if t > b: break
        area += v * (t - t0); t0, v = t, x
    return (area + v * (b - t0)) / (b - a)

def fallback(c, T, end=None):
    """TWAP-60s Binance mid x USDTUSD; if no 60s history, last mid x USDTUSD (never null if any mid exists).
    end: optional window end (default T); the window is [end-LOOKBACK, end]."""
    e_ = T if end is None else end
    tw = twap(mid[c], e_ - LOOKBACK, e_); mode = "twap60"
    if tw is None:
        L = mid[c]
        if not L: return None, {"fallback_mode": "none"}
        i = max((k for k in range(len(L)) if L[k][0] <= T), default=len(L) - 1)
        tw = L[i][1]; mode = "last_mid"
    U = [v for t, v in usdt if t >= T - 120] or [v for t, v in usdt[-1:]]
    u = (sum(U) / len(U)) if U else 1.0
    b = statistics.median(fb_bias[c]) if len(fb_bias[c]) >= 5 else SEED_FB_BIAS[c]
    return tw * u / (1 + b / 1e4), {"fallback_mode": mode, "usdtusd": round(u, 6), "bias_bps_used": round(b, 4),
                                    "bn_feed_age_s": round(T - bn_last[c], 3) if bn_last[c] else None}

async def emit(c, T):
    """Hard deadline: never wait past T+DEADLINE_S, even if the task started late."""
    deadline = T + DEADLINE_S
    rem = deadline - now()
    if rem > 0:
        ev = waiters.setdefault((c, T), asyncio.Event())
        if (T - 1) in rt[c]: ev.set()
        try: await asyncio.wait_for(ev.wait(), timeout=rem)
        except asyncio.TimeoutError: pass
        waiters.pop((c, T), None)
    te = now()
    R = rt[c]
    secs = [s for s in range(T - LOOKBACK, T) if s in R]
    n = len(secs); missing = [s - T for s in range(T - LOOKBACK, T) if s not in R]
    gaps = [b - a for a, b in zip(secs, secs[1:])]
    feed_age = round(te - rt_last[c], 3) if rt_last[c] else None
    q = {"n_pts": n, "missing_offsets": missing[:20], "max_gap_s": max(gaps) if gaps else None,
         "rtds_feed_age_s": feed_age, "last_pt_recv_after_T_s": round(R[T - 1][1] - T, 3) if (T - 1) in R else None}
    stale = feed_age is None or feed_age > STALE_S
    if n >= MIN_PTS_OK and not stale:
        ptb, src, flag = sum(R[s][0] for s in secs) / n, "rtds_chainlink_avg60", "ok"
    elif n >= 45 and not stale:
        ptb, src, flag = sum(R[s][0] for s in secs) / n, "rtds_chainlink_avg60", "degraded"
    else:
        ptb, extra = fallback(c, T); src, flag = "fallback_binance_usd", ("fallback" if ptb is not None else "none"); q.update(extra)
    rec = {"T": T, "madrid": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(T)), "coin": c, "ptb": ptb,
           "source": src, "quality": flag, "emit_ts": round(te, 3), "emit_latency_s": round(te - T, 3), **q}
    fb, _ = fallback(c, T); rec["fallback_shadow"] = fb
    wlog("emits.ndjson", rec)
    latest[c] = {k: rec[k] for k in ("T", "madrid", "ptb", "source", "quality", "emit_ts", "emit_latency_s", "n_pts", "max_gap_s", "rtds_feed_age_s")}
    write_latest()
    pending[(c, T)] = rec
    asyncio.get_running_loop().call_later(8, lambda c=c, T=T: wlog("rt_points.ndjson", {"T": T, "coin": c,
        "pts": {str(s - T): [rt[c][s][0], round(rt[c][s][1] - T, 3)] for s in range(T - 75, T + 6) if s in rt[c]}}))

SHM_PATH = str(config.ptb_shm_path())   # fichero que lee el bot (PM_PTB_SHM o <CARO5M_SHM_DIR>/pm_ptb_latest.json)

def write_latest():
    obj = {"updated": round(now(), 3), "lookback_s": LOOKBACK, "alignment": "[T-60,T)", **latest,
           "corrected_alignment": f"ts in [T{C2_A},T{C2_B}] (60 pts, gaps interpolated)",
           "corrected": latest_corr}
    atomic(f"{OUT}/pm_ptb_latest.json", obj)
    if SHM_PATH:
        try:
            tmp = SHM_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: json.dump(obj, f, separators=(",", ":"))
            compat.replace(tmp, SHM_PATH)
        except Exception as e: elog("shm_write", e)

def c2_value(R, T, upto):
    """Corrected window over ts grid [T+C2_A, T+C2_B]; uses points with ts <= upto (upto>=T+C2_B) for gap filling."""
    A, B = T + C2_A, T + C2_B
    avail = {s: R[s][0] for s in list(R) if A - 30 <= s <= upto}
    real = [s for s in range(A, B + 1) if s in avail]
    if not real: return None, None, real, []
    keys = sorted(avail); xs = []; interp = []
    for s in range(A, B + 1):
        if s in avail: xs.append(avail[s]); continue
        prev = max((k for k in keys if k < s), default=None); nxt = min((k for k in keys if k > s), default=None)
        if prev is not None and nxt is not None:
            w = (s - prev) / (nxt - prev); xs.append(avail[prev] * (1 - w) + avail[nxt] * w)
        else: xs.append(avail[prev if prev is not None else nxt])
        interp.append(s - T)
    return sum(xs) / len(xs), sum(avail[s] for s in real) / len(real), real, interp

async def emit_corr(c, T):
    """Corrected method (shadow): emit as soon as a point with ts >= T+C2_B arrives; hard deadline T+DEADLINE_S."""
    try:
        deadline = T + DEADLINE_S
        rem = deadline - now()
        if rem > 0:
            ev = corr_waiters.setdefault((c, T), asyncio.Event())
            if any(s >= T + C2_B for s in list(rt[c])): ev.set()
            try: await asyncio.wait_for(ev.wait(), timeout=rem)
            except asyncio.TimeoutError: pass
            corr_waiters.pop((c, T), None)
        te = now(); R = rt[c]
        after = [s for s in list(R) if s >= T + C2_B]
        upto = min(after) if after else T + C2_B   # trigger point: first point at/after T-3
        val, plain, real, interp = c2_value(R, T, upto)
        n = len(real); feed_age = round(te - rt_last[c], 3) if rt_last[c] else None
        stale = feed_age is None or feed_age > STALE_S
        gaps = [b - a for a, b in zip(real, real[1:])]
        trig = R.get(upto)
        rec = {"T": T, "madrid": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(T)), "coin": c,
               "c2_window": f"[T{C2_A},T{C2_B}]", "c2_emit_ts": round(te, 3), "c2_emit_latency_s": round(te - T, 3),
               "c2_n_pts": n, "c2_n_interp": len(interp), "c2_interp_offsets": interp[:20],
               "c2_max_gap_s": max(gaps) if gaps else None, "c2_rtds_feed_age_s": feed_age,
               "c2_trigger_offset": (upto - T) if trig else None,
               "c2_trigger_recv_rel_T_s": round(trig[1] - T, 3) if trig else None,
               "c2_last_pt_recv_rel_T_s": round(R[T + C2_B][1] - T, 3) if (T + C2_B) in R else None}
        if n >= MIN_PTS_OK and not stale and val is not None:
            rec.update(c2_ptb=val, c2_ptb_plain=plain, c2_source="rtds_chainlink_avg60_m62m3", c2_quality="ok")
        elif n >= 45 and not stale and val is not None:
            rec.update(c2_ptb=val, c2_ptb_plain=plain, c2_source="rtds_chainlink_avg60_m62m3", c2_quality="degraded")
        else:
            fb, extra = fallback(c, T, end=T + C2_B + 1)
            rec.update(c2_ptb=fb, c2_ptb_plain=plain, c2_source="fallback_binance_usd",
                       c2_quality="fallback" if fb is not None else "none", **{"c2_" + k: v for k, v in extra.items()})
        wlog("emits_corr.ndjson", rec)
        latest_corr[c] = {k: rec.get(k) for k in ("T", "madrid", "c2_ptb", "c2_source", "c2_quality", "c2_emit_ts",
                                                  "c2_emit_latency_s", "c2_n_pts", "c2_n_interp", "c2_rtds_feed_age_s")}
        write_latest()
        pending_corr[(c, T)] = {k: v for k, v in rec.items() if k.startswith("c2_")}
    except Exception as e:
        elog("emit_corr", e)

async def gamma_follow(sess, c, T):
    slug = coins_mod.slug(c, T); first = {}; ev = None
    await asyncio.sleep(300)
    while now() < T + 2400:
        try:
            async with sess.get("https://gamma-api.polymarket.com/events", params={"slug": slug}, headers=UA,
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
                j = await r.json(content_type=None)
            if j:
                ev = j[0]; md = ev.get("eventMetadata") or {}
                for k in ("priceToBeat", "finalPrice"):
                    if md.get(k) is not None and k not in first: first[k] = round(now() - T, 1)
                if len(first) == 2: break
        except Exception as e: elog("gamma " + slug, e)
        await asyncio.sleep(15)
    E = pending.pop((c, T), None) or {}
    C2 = pending_corr.pop((c, T), None) or {}
    md = (ev or {}).get("eventMetadata") or {}; m0 = ((ev or {}).get("markets") or [{}])[0]
    ptb, fin = md.get("priceToBeat"), md.get("finalPrice")
    rec = {**E, **C2, "pm_ptb": ptb, "pm_final": fin, "pm_ptb_first_seen_s": first.get("priceToBeat"),
           "pm_final_first_seen_s": first.get("finalPrice"), "cfg_id": m0.get("cryptoMarketConfigId"),
           "twap_lookback_s": (m0.get("cryptoMarketConfig") or {}).get("twapLookbackSeconds")}
    if ptb and E.get("ptb"):
        rec["err_bps"] = round(1e4 * (E["ptb"] - ptb) / ptb, 5)
        if fin is not None: rec["dir_agree"] = (fin >= E["ptb"]) == (fin >= ptb)
    if ptb and E.get("fallback_shadow"):
        e = 1e4 * (E["fallback_shadow"] - ptb) / ptb; rec["fallback_err_bps"] = round(e, 4)
        if fin is not None: rec["fallback_dir_agree"] = (fin >= E["fallback_shadow"]) == (fin >= ptb)
        b = statistics.median(fb_bias[c]) if len(fb_bias[c]) >= 5 else SEED_FB_BIAS[c]
        fb_bias[c].append(e + b)
    for key, pre in (("c2_ptb", "c2_"), ("c2_ptb_plain", "c2_plain_")):
        if ptb and C2.get(key):
            rec[pre + "err_bps"] = round(1e4 * (C2[key] - ptb) / ptb, 5)
            if fin is not None: rec[pre + "dir_agree"] = (fin >= C2[key]) == (fin >= ptb)
    wlog("windows.ndjson", rec); summarize()

def summarize():
    rows = [json.loads(l) for l in open(f"{OUT}/windows.ndjson", encoding="utf-8")]
    S = {"updated_madrid": time.strftime("%Y-%m-%d %H:%M:%S"), "by_coin": {},
         "ws": {"reconnects": ws_holder["reconnects"], "resubscribes": ws_holder["resubscribes"]}}
    for c in COINS + ["all"]:
        R = [r for r in rows if c == "all" or r.get("coin") == c]
        E = [r for r in R if "err_bps" in r]; a = sorted(abs(r["err_bps"]) for r in E); D = [r["dir_agree"] for r in E if "dir_agree" in r]
        lat = [r["emit_latency_s"] for r in R if "emit_latency_s" in r]
        F = sorted(abs(r["fallback_err_bps"]) for r in R if "fallback_err_bps" in r)
        S["by_coin"][c] = {"windows": len(R), "n_err": len(E),
            "quality_counts": dict(collections.Counter(r.get("quality") for r in R)),
            "emit_latency_s_median": round(statistics.median(lat), 3) if lat else None, "emit_latency_s_max": max(lat) if lat else None,
            "err_bps_signed_median": round(statistics.median([r["err_bps"] for r in E]), 5) if E else None,
            "abs_err_bps_median": round(statistics.median(a), 5) if a else None, "abs_err_bps_p90": a[int(0.9 * (len(a) - 1))] if a else None,
            "abs_err_bps_max": a[-1] if a else None, "within_0.01bps_pct": round(100 * sum(x <= 0.01 for x in a) / len(a), 1) if a else None,
            "within_0.1bps_pct": round(100 * sum(x <= 0.1 for x in a) / len(a), 1) if a else None,
            "dir_checked": len(D), "dir_disagree": D.count(False),
            "fallback_abs_err_bps_median": round(statistics.median(F), 4) if F else None,
            "fallback_dir_disagree": sum(r.get("fallback_dir_agree") is False for r in R),
            "pm_ptb_first_seen_s_median": statistics.median([r["pm_ptb_first_seen_s"] for r in R if r.get("pm_ptb_first_seen_s")]) if any(r.get("pm_ptb_first_seen_s") for r in R) else None}
    try: S["corrected"] = corrected_summary(rows)
    except Exception as e: elog("corrected_summary", e)
    atomic(f"{OUT}/summary.json", S)

def _m(errs_signed, dirs):
    a = sorted(abs(x) for x in errs_signed); n = len(a)
    if not n: return {"n": 0}
    return {"n": n, "abs_err_bps_median": round(statistics.median(a), 5), "abs_err_bps_p90": a[int(0.9 * (n - 1))],
            "abs_err_bps_max": a[-1], **{f"within_{t}bps_pct": round(100 * sum(x <= t for x in a) / n, 1) for t in (0.01, 0.1, 0.3, 1)},
            "dir_checked": len(dirs), "dir_disagree": dirs.count(False)}

def corrected_summary(rows):
    """Out-of-sample: windows with T >= corr deploy first_T, both methods on the same windows."""
    dep = json.load(open(f"{OUT}/corr_deploy.json", encoding="utf-8")) if os.path.exists(f"{OUT}/corr_deploy.json") else {}
    T0 = dep.get("first_T", 1 << 62)
    out = {"deploy_madrid": dep.get("madrid"), "first_T": dep.get("first_T"), "window": f"ts in [T{C2_A},T{C2_B}]", "by_coin": {}}
    for c in COINS + ["all"]:
        R = [r for r in rows if r.get("T", 0) >= T0 and (c == "all" or r.get("coin") == c) and "c2_err_bps" in r and "err_bps" in r]
        lat = sorted(r["c2_emit_latency_s"] for r in R if "c2_emit_latency_s" in r)
        out["by_coin"][c] = {"windows": len(R),
            "c2_quality_counts": dict(collections.Counter(r.get("c2_quality") for r in R)),
            "c2": _m([r["c2_err_bps"] for r in R], [r["c2_dir_agree"] for r in R if "c2_dir_agree" in r]),
            "c2_plain": _m([r["c2_plain_err_bps"] for r in R if "c2_plain_err_bps" in r], [r["c2_plain_dir_agree"] for r in R if "c2_plain_dir_agree" in r]),
            "current": _m([r["err_bps"] for r in R], [r["dir_agree"] for r in R if "dir_agree" in r]),
            "c2_emit_latency_s_median": statistics.median(lat) if lat else None, "c2_emit_latency_s_max": lat[-1] if lat else None}
    return out

async def heartbeat():
    while True:
        wlog("heartbeat.ndjson", {"madrid": time.strftime("%H:%M:%S"),
             "rtds_age": {c: round(now() - rt_last[c], 2) if rt_last[c] else None for c in COINS},
             "bn_age": {c: round(now() - bn_last[c], 2) if bn_last[c] else None for c in COINS},
             "usdt": usdt[-1][1] if usdt else None, "pending": len(pending), "pending_corr": len(pending_corr),
             "reconnects": ws_holder["reconnects"], "resubscribes": ws_holder["resubscribes"]})
        await asyncio.sleep(60)

START_T = time.time()

# ── seguir el ajuste COINS en caliente: si cambian las monedas, el PTB se reinicia solo (mismo comando) ──
COINS_WATCH_S = 5.0


def settings_coins():
    try:
        return list(config.load_settings()["COINS"])
    except Exception:
        return list(COINS)


def restart_self(_execv=None, _popen=None, _exit=None):
    """Vuelve a arrancar este PTB con las monedas nuevas.

    Linux/macOS: ``os.execv`` (mismo pid: data/ptb.pid y systemd siguen valiendo).
    Windows: lanza un PTB nuevo desacoplado, actualiza data/ptb.pid si apuntaba a este proceso y sale."""
    cmd = [sys.executable, "-m", "caro5m.ptb"]
    if not compat.is_windows():
        try:
            sys.stdout.flush(); sys.stderr.flush()
        except Exception:
            pass
        (_execv or os.execv)(sys.executable, cmd)
        return None
    log_f = open(os.path.join(str(config.data_dir()), "ptb.log"), "ab")
    try:
        proc = (_popen or subprocess.Popen)(cmd, cwd=str(config.home_dir()), env=dict(os.environ), stdin=subprocess.DEVNULL,
                                            stdout=log_f, stderr=log_f, **compat.detached_popen_kwargs())
    finally:
        log_f.close()
    pid_file = config.data_dir() / "ptb.pid"
    try:
        if pid_file.read_text(encoding="utf-8").strip() == str(os.getpid()):
            tmp = pid_file.with_name("ptb.pid.tmp")
            tmp.write_text(str(proc.pid), encoding="utf-8")
            compat.replace(tmp, pid_file)
    except OSError:
        pass
    (_exit or os._exit)(0)
    return proc


async def coins_watch():
    last = config.settings_mtime()
    while True:
        await asyncio.sleep(COINS_WATCH_S)
        m = config.settings_mtime()
        if m == last:
            continue
        last = m
        new = settings_coins()
        if new != COINS:
            wlog("run.log", {"event": "coins_changed_restart", "old": COINS, "new": new, "madrid": time.strftime("%Y-%m-%d %H:%M:%S")})
            print(f"PTB: monedas cambiadas {','.join(COINS)} -> {','.join(new)}: me reinicio para seguirlas", flush=True)
            restart_self()

async def main():
    wlog("run.log", {"event": "start", "madrid": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": os.getpid(),
                     "note": "watchdog+hard-deadline+lastmid-fallback+corrected_c2_m62m3"})
    first_T = (int(now() + C2_EARLY_S) // 300 + 1) * 300
    atomic(f"{OUT}/corr_deploy.json", {"deploy_ts": round(now(), 3), "madrid": time.strftime("%Y-%m-%d %H:%M:%S"),
                                       "first_T": first_T, "first_T_madrid": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(first_T)),
                                       "window": f"ts in [T{C2_A},T{C2_B}]", "note": "windows with T >= first_T are out-of-sample"}) \
        if not os.path.exists(f"{OUT}/corr_deploy.json") else None
    async with aiohttp.ClientSession() as sess:
        for f in (rtds_ws(), binance_ws(), usdt_poll(sess), heartbeat(), coins_watch()): asyncio.create_task(f)
        keep = set()
        while True:
            T = (int(now()) // 300 + 1) * 300
            await asyncio.sleep(max(0, T - C2_EARLY_S - now()))
            if now() <= T + DEADLINE_S:
                for c in COINS:
                    t = asyncio.create_task(emit_corr(c, T)); keep.add(t); t.add_done_callback(keep.discard)
            await asyncio.sleep(max(0, T - 0.5 - now()))
            # if we somehow woke up past the deadline for this T, skip rather than emit late
            if now() > T + DEADLINE_S:
                wlog("run.log", {"event": "skip_late_window", "T": T, "lag_s": round(now() - T, 3)})
                asyncio.get_running_loop().call_later(10, lambda T=T: [pending_corr.pop((c, T), None) for c in COINS])
                continue
            for c in COINS:
                for coro in (emit(c, T), gamma_follow(sess, c, T)):
                    t = asyncio.create_task(coro); keep.add(t); t.add_done_callback(keep.discard)
            await asyncio.sleep(max(0.5, T + 1 - now()))

def run() -> None:
    compat.ensure_utf8_stdio()
    asyncio.run(main())


if __name__ == "__main__":
    run()
