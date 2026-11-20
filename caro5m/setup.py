"""Asistente de configuración para terminal (lo usa install.sh). ``python -m caro5m.setup``.

  python -m caro5m.setup                  guiado la primera vez; menú si ya hay clave (más cómodo: el panel, tecla A)
  python -m caro5m.setup --guided|--menu  fuerza uno u otro
  python -m caro5m.setup --check          solo las comprobaciones de solo lectura
  python -m caro5m.setup --non-interactive  lee CARO5M_SETUP_* del entorno (sin preguntas, sin aprobaciones)
  python -m caro5m.setup --log            muestra dónde está el registro de errores y sus últimas líneas (para copiarlas)

Todo queda anotado (con secretos enmascarados) en data/logs/setup.log y los errores, con su traceback, en data/logs/errors.log.

Variables para --non-interactive (todas opcionales): CARO5M_SETUP_WALLET (eoa|magic|safe),
CARO5M_SETUP_PRIVATE_KEY o CARO5M_SETUP_PRIVATE_KEY_FILE, CARO5M_SETUP_FUNDER, CARO5M_SETUP_PROXY,
CARO5M_SETUP_PROXY_SCAN (0/1), CARO5M_SETUP_GENERATE_KEYS (1), CARO5M_SETUP_CHECKS (0 para omitir).

Nunca toca DRY_RUN (el bot sigue en modo PRUEBA), nunca muestra un secreto completo y NUNCA aprueba nada
on-chain sin que escribas «s» a una pregunta explícita (el modo no interactivo no puede aprobar).
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from typing import Callable, Dict, List, Optional

from . import account, config, setuplog, wizard

ICON_COLOR = {"ok": "\033[32m", "warn": "\033[33m", "fail": "\033[31m", "info": "\033[36m"}


class IO:
    """Entrada/salida de terminal (sustituible en pruebas)."""

    def __init__(self, color: Optional[bool] = None) -> None:
        self.color = sys.stdout.isatty() if color is None else color

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def ask(self, prompt: str, default: str = "") -> str:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
        return v or default

    def secret(self, prompt: str) -> str:
        return getpass.getpass(f"{prompt}: ").strip()

    def yes(self, prompt: str, default: bool = False) -> bool:
        v = input(f"{prompt} ({'S/n' if default else 's/N'}): ").strip().lower()
        return default if not v else v in ("s", "si", "sí", "y", "yes")

    def check_line(self, c: account.Check) -> None:
        ic = account.ICON[c.level]
        if self.color:
            ic = f"{ICON_COLOR[c.level]}{ic}\033[0m"
        self.say(f"  {ic} {c.title}: {c.detail}" + (f"\n      → {c.fix}" if c.fix else ""))


# ── pasos ──────────────────────────────────────────────────────────
def current_state() -> Dict[str, object]:
    s, env = config.load_settings(), account.read_env()
    return {"sig": int(s["POLY_SIGNATURE_TYPE"]), "pk": bool(env.get("POLY_PRIVATE_KEY")), "funder": env.get("POLY_SAFE_ADDRESS") or env.get("POLY_FUNDER_ADDRESS") or "",
            "proxy": env.get("POLYMARKET_PROXY_LIST", ""), "creds": account.stored_credentials(env) is not None,
            "proxy_scan": bool(s["CARO5M_PROXY_SCAN"])}


def apply_wallet(wallet: str, private_key: str = "", funder: str = "") -> Dict[str, str]:
    """Guarda tipo de cartera (settings.env), clave y dirección de fondos (.env 0600) SIN preguntar (modo no interactivo y pruebas).
    Todo o nada: si algún campo es inválido lanza ValueError y no guarda nada. Para EOA, funder = dirección de la clave."""
    if wallet not in account.WALLET_TYPES:
        raise ValueError("Tipo de cartera: eoa, magic o safe")
    plan = account.evaluate_wallet(account.WALLET_TO_CHOICE[wallet], private_key, funder, lookup=False)
    if plan.errors:
        raise ValueError(next(iter(plan.errors.values())))
    warn = plan.conflict["message"] if plan.conflict else ""
    plan.conflict = None
    r = account.save_plan(plan)
    return {"wallet": wallet, "funder": r["funder"] or "", "warning": warn}


def apply_proxy_setting(proxy: str, scan: Optional[bool] = None) -> None:
    """'' = quitar el proxy. Valida (http/https/socks5/socks5h) y guarda en .env; el interruptor de lecturas en settings.env."""
    proxy = wizard.validate_proxy(proxy)
    if proxy:
        wizard.write_env({"POLYMARKET_PROXY_LIST": proxy})
    else:
        wizard.remove_env(["POLYMARKET_PROXY_LIST"])
    setuplog.event("proxy " + (f"guardado: {account.mask_proxy(proxy)}" if proxy else "eliminado"))
    if scan is not None:
        config.save_settings({"CARO5M_PROXY_SCAN": "1" if scan else "0"})


def first_proxy(env: Optional[Dict[str, str]] = None) -> Optional[str]:
    env = env if env is not None else account.read_env()
    lst = account.parse_proxy_list(env.get("POLYMARKET_PROXY_LIST", "")) if env.get("POLYMARKET_PROXY_LIST") else []
    return lst[0] if lst else None


def do_generate_keys(regenerate: bool = False) -> Dict[str, object]:
    """Deriva/crea las credenciales CLOB (L1) con la clave guardada, por el proxy si hay; las guarda y las valida."""
    env, s = account.read_env(), config.load_settings()
    pk, funder, sig = env.get("POLY_PRIVATE_KEY", ""), env.get("POLY_SAFE_ADDRESS") or env.get("POLY_FUNDER_ADDRESS") or "", int(s["POLY_SIGNATURE_TYPE"])
    if not pk:
        raise ValueError("Falta la clave privada: configura primero la cartera")
    proxy = first_proxy(env)
    nonce = account.next_nonce(env) if regenerate else None
    setuplog.event(f"generar claves CLOB (regenerar={regenerate}, proxy={'sí' if proxy else 'no'})")
    creds = account.generate_credentials(pk, sig, funder, proxy=proxy, nonce=nonce)
    account.save_credentials(creds, nonce)
    v = account.validate_credentials(pk, sig, funder, creds, proxy=proxy)
    return {"api_key": account.mask_secret(creds.api_key), "validation": v, "nonce": nonce}


def _ask_valid(io: IO, prompt: str, validate: Callable[[str], object], *, secret: bool = False, keep: bool = False, tries: int = 4) -> str:
    """Pregunta un campo y lo vuelve a pedir si es inválido. Vacío = conservar/saltar (nunca es un error). Devuelve '' o el valor válido."""
    for _ in range(tries):
        v = io.secret(prompt) if secret else io.ask(prompt)
        v = (v or "").strip()
        if not v:
            return ""
        try:
            validate(v)
            return v
        except ValueError as e:
            io.say(f"  ✖ {e}. Inténtalo otra vez (Enter vacío = dejarlo como está).")
    return ""


def _show_steps(io: IO, title: str, steps: List[str]) -> None:
    io.say(f"  {title}")
    for n, t in enumerate(steps, 1):
        io.say(f"    {n}. {t}")


def step_wallet(io: IO) -> None:
    st = current_state()
    stored = account.read_env()
    io.say("\n── 1/4 Tu cartera ──────────────────────────────────────")
    io.say("  ¿Cómo entras en polymarket.com?")
    keys = list(account.WALLET_CHOICES)
    for n, k in enumerate(keys, 1):
        io.say(f"    {n}) {account.WALLET_CHOICES[k]['label']}")
    default = str(keys.index(account.choice_for_sig(int(st["sig"]))) + 1)
    pick = io.ask("Elige 1, 2 o 3", default)
    choice = keys[int(pick) - 1] if pick in ("1", "2", "3") else keys[int(default) - 1]
    meta = account.WALLET_CHOICES[choice]
    io.say(f"  → {meta['short']}.")
    _show_steps(io, "Cómo obtener tu CLAVE PRIVADA:", meta["key_steps"])
    io.say("  (Se pide oculta. Se guarda solo en .env con permisos 0600 y nunca se muestra entera.)")
    key_prompt = "Clave privada (0x… o 64 hex)" + ("; Enter = conservar la guardada" if st["pk"] else "; Enter = saltar este paso")
    key = _ask_valid(io, key_prompt, wizard.validate_private_key, secret=True)
    signer = wizard.signer_address(key or stored.get("POLY_PRIVATE_KEY", "") or "") if (key or stored.get("POLY_PRIVATE_KEY")) else None
    if signer:
        io.say(f"  ✔ Tu clave corresponde a la dirección {wizard.mask_address(signer)} (la dirección de tu cartera).")
    funder = ""
    if meta["wallet"] != "eoa":
        _show_steps(io, "Cómo obtener la DIRECCIÓN DE TU CUENTA de Polymarket (dónde están tus fondos):", meta["funder_steps"])
        cur = str(st["funder"])
        funder = _ask_valid(io, "Dirección de tu cuenta de Polymarket (0x…)" + (f"; Enter = conservar {wizard.mask_address(cur)}" if cur else "; Enter = buscarla por mí / saltar"),
                            wizard.validate_address)
    if signer:
        io.say("  Consultando tu perfil público de Polymarket…")
    plan = account.evaluate_wallet(choice, key, funder, stored=stored, proxy=first_proxy(stored))
    for w in plan.warnings:
        io.say(f"  ⚠ {w}")
    if plan.funder_from_profile and plan.funder:
        io.say(f"  ✔ Polymarket indica que tu cuenta es {wizard.mask_address(plan.funder)}; la usaré como dirección de fondos.")
    while plan.conflict:
        io.say(f"\n  ⚠ {plan.conflict['message']}")
        opts = plan.conflict["options"]
        for n, (_c, label) in enumerate(opts, 1):
            io.say(f"    {n}) {label}")
        a = io.ask("Elige una opción", "1")
        idx = int(a) - 1 if a.isdigit() and 1 <= int(a) <= len(opts) else 0
        plan = account.resolve_conflict(plan, opts[idx][0])
        if plan.choice != choice:
            io.say(f"  → Cambiado a: {account.WALLET_CHOICES[plan.choice]['short']}.")
            choice = plan.choice
    r = account.save_plan(plan)
    parts = []
    if r["saved_key"]:
        parts.append("clave guardada en .env (0600)")
    elif plan.has_key:
        parts.append("clave: se conserva la guardada")
    else:
        parts.append("clave: PENDIENTE")
    parts.append(f"fondos {wizard.mask_address(r['funder'])}" if r["funder"] else "dirección de fondos: PENDIENTE")
    io.say(f"  ✔ Guardado: {account.WALLET_CHOICES[plan.choice]['short']} · " + " · ".join(parts))
    for fld, msg in plan.errors.items():
        io.say(f"  ✖ {fld}: {msg} (no guardado)")


def step_proxy(io: IO) -> None:
    st = current_state()
    io.say("\n── 2/4 Proxy opcional HTTP/SOCKS (para redes u operadores que bloquean el DNS o restringen la salida) ──")
    io.say("  Las ÓRDENES y la autenticación salen por el proxy; el resto (precios) va directo salvo que lo actives.")
    io.say("  ⚠ Eres responsable de cumplir la ley de tu país y las condiciones de Polymarket.")
    direct = account.probe_proxy(None)
    lvl, txt = account.describe_probe(direct)
    io.say(f"  {account.ICON[lvl]} {txt}")
    cur = str(st["proxy"])
    if cur:
        io.say(f"  Proxy actual: {account.mask_proxy(cur)}")
    action = io.ask("[a]ñadir/editar · [q]uitar · [p]robar el actual · [Enter] seguir" if cur else "[a]ñadir proxy · [Enter] seguir sin proxy", "").lower()
    if action == "q" and cur:
        apply_proxy_setting("")
        io.say("  ✔ Proxy eliminado.")
    elif action == "a":
        new = io.secret("Proxy (http://usuario:clave@host:puerto, https://…, socks5h://… ; al escribir/pegar no se ve nada: pega y pulsa Enter)")
        try:
            apply_proxy_setting(new)
            io.say(f"  ✔ Guardado: {account.mask_proxy(new)}")
            cur = new
            action = "p"
        except ValueError as e:
            io.say(f"  ✖ {e}")
    if action == "p" or (action == "a" and cur):
        p = first_proxy()
        if p:
            lvl, txt = account.describe_probe(account.probe_proxy(p))
            io.say(f"  {account.ICON[lvl]} {txt}")
    if first_proxy():
        scan = io.yes("¿Usar el proxy también para leer precios (Gamma/libro)? Más lento; normalmente no", bool(st["proxy_scan"]))
        apply_proxy_setting(account.read_env().get("POLYMARKET_PROXY_LIST", ""), scan)


def step_keys(io: IO) -> None:
    st = current_state()
    io.say("\n── 3/4 Claves CLOB (API key del CLOB de Polymarket) ──")
    if not st["pk"]:
        io.say("  Sin clave privada: salta este paso.")
        return
    regen = False
    if st["creds"]:
        io.say("  Ya hay claves guardadas.")
        v = account.validate_credentials(account.read_env().get("POLY_PRIVATE_KEY", ""), int(st["sig"]), str(st["funder"]),
                                         account.stored_credentials(account.read_env()), proxy=first_proxy())
        io.say(f"  {'✔ válidas' if v['ok'] else '✖ rechazadas: ' + str(v['error'])}")
        if not v["ok"]:
            io.say("    Detalle técnico: " + setuplog.short_path(setuplog.errors_log()))
        if v["ok"] and not io.yes("¿Regenerar (claves nuevas con otro nonce)?", False):
            return
        regen = True
    elif not io.yes("¿Generar las claves ahora (firma con tu clave, no gasta nada)?", True):
        return
    for attempt in (1, 2):
        try:
            r = do_generate_keys(regen)
            v = r["validation"]
            io.say(f"  ✔ Claves guardadas en .env (key {r['api_key']}). Comprobación autenticada: " + ("OK" if v["ok"] else f"FALLÓ ({v['error']})"))
            return
        except account.NeedProxy as e:
            setuplog.event(f"claves CLOB detenidas: {e}", "WARN")
            io.say(f"  ✖ {e}")
            if attempt == 1 and io.yes("  ¿Configurar el proxy ahora?", True):
                step_proxy(io)
                continue
            io.say("    Configura el proxy (menú → 2) y repite este paso.")
            return
        except Exception as e:
            setuplog.error("generar claves CLOB", e)
            io.say(f"  ✖ {account.explain_clob_error(e, first_proxy())}")
            io.say("    Detalle técnico completo: " + setuplog.short_path(setuplog.errors_log()) + "  (python -m caro5m.setup --log)")
            return


def step_checks(io: IO) -> List[account.Check]:
    io.say("\n── 4/4 Comprobaciones previas (solo lectura) ──")
    cs = account.run_preflight()
    for c in cs:
        io.check_line(c)
    ok, warn, fail = account.summarize(cs)
    io.say(f"\n  Resumen: {ok} ✔  {warn} ⚠  {fail} ✖ — " + ("listo para operar." if account.ready_to_trade(cs) else "hay problemas que impiden operar en real."))
    io.say("  El bot sigue en modo PRUEBA (DRY_RUN=1). Para operar en real: panel → [M].")
    io.say("  Registro (copiable, sin secretos): " + setuplog.short_path(setuplog.setup_log()) + " · errores: " + setuplog.short_path(setuplog.errors_log()))
    return cs


def step_approvals(io: IO) -> None:
    st = current_state()
    wallet = account.wallet_key(int(st["sig"]))
    io.say("\n── Aprobaciones (permisos de USDC/tokens para el exchange) ──")
    io.say("  " + account.approvals_help(wallet))
    env, s = account.read_env(), config.load_settings()
    if wallet != "eoa":
        if st["creds"] and io.yes("¿Pedir al CLOB que vuelva a leer el estado de permisos (gratis)?", False):
            try:
                account.refresh_allowance_cache(env["POLY_PRIVATE_KEY"], int(st["sig"]), str(st["funder"]), account.stored_credentials(env), proxy=first_proxy(env))
                io.say("  ✔ Hecho.")
            except Exception as e:
                io.say(f"  ✖ {account.explain_clob_error(e, first_proxy(env))}")
        return
    pk = env.get("POLY_PRIVATE_KEY", "")
    signer = wizard.signer_address(pk) if pk else None
    if not signer:
        io.say("  Falta la clave privada.")
        return
    items = account.approval_status(signer, s["POLYGON_RPCS"], s["CHAIN_ID"])
    missing = [i for i in items if not i["done"]]
    for i in items:
        io.say(f"  {'✔' if i['done'] else '✖'} {i['name']}")
    if not missing:
        io.say("  Todo aprobado.")
        return
    if not io.yes(f"¿Enviar {len(missing)} transacción(es) de aprobación con tu clave (gasta POL)?", False):
        io.say("  No se ha enviado nada.")
        return
    try:
        for r in account.approve_onchain(pk, s["POLYGON_RPCS"], s["CHAIN_ID"], confirmed=True):
            io.say(f"  {r['status']}: {r['name']} (tx {r['tx'][:10]}…)")
    except Exception as e:
        io.say(f"  ✖ {account._clean_err(e)}")


def next_steps(io: IO, ready: Optional[bool] = None) -> None:
    """Qué hacer al terminar (no se repite si lo llama el instalador, que imprime su propio resumen)."""
    if os.environ.get("CARO5M_FROM_INSTALLER") == "1":
        return
    from . import compat

    panel = compat.panel_command() + ("" if compat.is_windows() else "   (o .venv/bin/caro5m)")
    io.say("\n══ Siguientes pasos ══")
    if ready:
        io.say("  ✔ Todo listo.")
    elif ready is False:
        io.say("  ⚠ Quedan avisos arriba: puedes corregirlos luego desde el panel, tecla [A].")
    io.say(f"  1) Abre el panel del bot:  {panel}")
    io.say("  2) En el panel: [B] arranca el bot (modo PRUEBA) · [M] pasa a modo REAL (pide confirmación) · [L] log · [A] cuenta")
    io.say(f"  3) Sin panel:  {compat.bot_command()}")


def guided(io: IO) -> None:
    io.say("caro5m — configuración de cuenta (puedes saltar cualquier paso con Enter y repetirlo luego con: python -m caro5m.setup)")
    step_wallet(io)
    step_proxy(io)
    step_keys(io)
    cs = step_checks(io)
    if account.wallet_key(int(current_state()["sig"])) == "eoa" and current_state()["pk"] and io.yes("\n¿Revisar/aprobar ahora los permisos del exchange (EOA)?", False):
        step_approvals(io)
    next_steps(io, account.ready_to_trade(cs) and bool(current_state()["pk"]))


def menu(io: IO) -> None:
    while True:
        st = current_state()
        io.say("\n══ Configuración de cuenta ══")
        io.say(f"  Cartera: {account.WALLET_CHOICES[account.choice_for_sig(int(st['sig']))]['short']} · clave {'✔' if st['pk'] else '✖'} · claves CLOB {'✔' if st['creds'] else '✖'} · proxy {account.mask_proxy(str(st['proxy'])) if st['proxy'] else 'ninguno'}")
        io.say("  1) Cartera y clave   2) Proxy   3) Claves CLOB   4) Comprobaciones   5) Aprobaciones   0) Salir")
        c = io.ask("Opción", "0")
        if c == "1":
            step_wallet(io)
        elif c == "2":
            step_proxy(io)
        elif c == "3":
            step_keys(io)
        elif c == "4":
            step_checks(io)
        elif c == "5":
            step_approvals(io)
        else:
            next_steps(io)
            return


def non_interactive(io: IO, env: Optional[Dict[str, str]] = None) -> int:
    e = os.environ if env is None else env
    pk = e.get("CARO5M_SETUP_PRIVATE_KEY", "")
    if e.get("CARO5M_SETUP_PRIVATE_KEY_FILE"):
        with open(e["CARO5M_SETUP_PRIVATE_KEY_FILE"], encoding="utf-8") as f:
            pk = f.read().strip()
    wallet, funder, proxy = e.get("CARO5M_SETUP_WALLET", ""), e.get("CARO5M_SETUP_FUNDER", ""), e.get("CARO5M_SETUP_PROXY")
    did = False
    try:
        if wallet or pk or funder:
            apply_wallet(wallet or account.wallet_key(int(current_state()["sig"])), pk, funder)
            io.say("✔ Cartera guardada")
            did = True
        if proxy is not None:
            scan = e.get("CARO5M_SETUP_PROXY_SCAN")
            apply_proxy_setting(proxy, None if scan is None else scan == "1")
            io.say("✔ Proxy " + ("guardado: " + account.mask_proxy(proxy) if proxy else "eliminado"))
            did = True
        if e.get("CARO5M_SETUP_GENERATE_KEYS") == "1":
            r = do_generate_keys(False)
            io.say(f"✔ Claves CLOB guardadas (key {r['api_key']}); validación: {'OK' if r['validation']['ok'] else r['validation']['error']}")
            did = True
    except Exception as ex:
        if not isinstance(ex, (account.NeedProxy, ValueError)):
            setuplog.error("configuración no interactiva", ex)
        io.say(f"✖ {account.explain_clob_error(ex, proxy) if isinstance(ex, (account.NeedProxy, account.ClobError)) else account._clean_err(ex)}")
        io.say("  Registro de errores: " + setuplog.short_path(setuplog.errors_log()))
        return 1
    if not did:
        io.say("(sin variables CARO5M_SETUP_*: no se cambió nada)")
    if e.get("CARO5M_SETUP_CHECKS", "1") != "0":
        cs = step_checks(io)
        next_steps(io, account.ready_to_trade(cs))
    else:
        next_steps(io)
    return 0


def main(argv: Optional[List[str]] = None, io: Optional[IO] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m caro5m.setup", description="Configuración de cuenta de caro5m")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--guided", action="store_true")
    g.add_argument("--menu", action="store_true")
    g.add_argument("--check", action="store_true", help="solo comprobaciones de solo lectura")
    g.add_argument("--non-interactive", action="store_true", help="usa CARO5M_SETUP_* del entorno")
    g.add_argument("--log", action="store_true", help="muestra la ruta del registro de errores y sus últimas líneas")
    a = ap.parse_args(argv)
    from . import compat

    compat.ensure_utf8_stdio()
    io = io or IO()
    setuplog.install_library_capture()
    try:
        if a.log:
            io.say(f"Registro completo: {setuplog.setup_log()}\nSolo errores:      {setuplog.errors_log()}\n")
            io.say(setuplog.report("errors", 80))
            return 0
        setuplog.event("── inicio de setup " + " ".join(argv if argv is not None else sys.argv[1:]))
        if a.check:
            cs = step_checks(io)
            return 0 if account.ready_to_trade(cs) else 2
        if a.non_interactive:
            return non_interactive(io)
        if a.menu or (not a.guided and current_state()["pk"]):
            menu(io)
        else:
            guided(io)
        return 0
    except (KeyboardInterrupt, EOFError):
        io.say("\nCancelado. No se ha cambiado nada más.")
        return 130
    except Exception as ex:   # error inesperado: se registra completo y se explica sin traceback
        setuplog.error("setup (error inesperado)", ex)
        io.say(f"✖ Error inesperado: {account._clean_err(ex)}\n  Detalle completo (sin secretos): {setuplog.errors_log()}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
