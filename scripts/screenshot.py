#!/usr/bin/env python3
"""Genera capturas SVG del TUI con datos de DEMOSTRACIÓN (sin bot, sin red, sin claves reales). Uso:

    python scripts/screenshot.py docs/tui.svg [VISTA]

Vistas: main · version · modo · settings · coins · log · cartera · cuenta · conflicto · proxy · errores · real
Todas usan datos inventados (direcciones 0x1111…/0x2222…, IP de documentación 203.0.113.x) en una carpeta temporal,
salvo «real», que captura el panel con lo que haya en CARO5M_SCREENSHOT_HOME (una carpeta donde el bot corre en PRUEBA).
Para pasar las SVG a PNG: python scripts/svg2png.py docs/*.svg
"""
import asyncio
import os
import sys
import tempfile

# Textual respeta NO_COLOR/TERM=dumb: para una captura a color los quitamos
for k in ("NO_COLOR", "FORCE_COLOR"):
    os.environ.pop(k, None)
os.environ["TERM"] = "xterm-256color"
os.environ["COLORTERM"] = "truecolor"
home = os.environ.get("CARO5M_SCREENSHOT_HOME")
if not home:  # carpeta de demostración limpia en cada captura (ruta corta y genérica en pantalla)
    import shutil

    home = os.path.join(tempfile.gettempdir(), "caro5m")
    shutil.rmtree(home, ignore_errors=True)
    os.makedirs(home)
os.environ["CARO5M_HOME"] = home
os.environ.setdefault("CARO5M_SHM_DIR", os.path.join(home, "data", "shm"))

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from caro5m import account as account_mod, config, launcher, status  # noqa: E402
from caro5m.tui.app import Caro5mApp  # noqa: E402
sys.path.insert(0, os.path.dirname(__file__))
import demo_status  # noqa: E402

SIZES = {"cuenta": (118, 74), "cartera": (118, 58), "conflicto": (118, 40), "proxy": (80, 24), "errores": (110, 32),
         "coins": (118, 40), "settings": (118, 40), "log": (130, 34), "modo": (118, 38)}
DEMO_LOG = """\
[2026-11-20 10:35:00] === caro5m 1.2.6 starting ===
[2026-11-20 10:35:00] coins=btc,eth,xrp,sol,doge,hype degradadas(sin Chainlink en Polygon, precio Binance)=hype
[2026-11-20 10:35:00] mode=PRUEBA (DRY_RUN: no se envían órdenes)
[2026-11-20 10:35:00] scan=direct order_proxy=203.0.113.10
[2026-11-20 10:35:01] FEED writer starting (chainlink RPC x3 + binance REST https://api.binance.com)
[2026-11-20 10:35:04] PTB_SRC=pm_c2 btc iv=1795170900 secs=4 ptb=62268.5700 legacy_open_twap=62270.1100 legacy_minus_c2_bps=+0.25 q=ok
[2026-11-20 10:38:01] NEURON_TRACE BTC UP lane=B d_twap=+0.1520 bin1m=+0.0410 rsi=57.2 ask=0.550 hist_n=24
[2026-11-20 10:38:01] SCAN BTC slug=btc-updown-5m-1795171200 secs=61/239 d_twap=+0.1520 d_spot=+0.1490 bin1m=+0.0410 rsi=57.2 up_ask=0.550 dn_ask=0.460 spread=-0.010 | SIGNAL UP
[2026-11-20 10:38:01] ORDER_ATTEMPT BUY BTC UP ask=0.5500 size=1.8182 usdc=1.0 DRY_RUN
[2026-11-20 10:38:01] ORDER_RESULT BTC UP filled=True status=matched DRY_RUN
[2026-11-20 10:38:01] FILL BTC UP @0.5500 size=1.8182 slug=btc-updown-5m-1795171200 DRY_RUN
[2026-11-20 10:38:04] SCAN ETH slug=eth-updown-5m-1795171200 secs=64/236 d_twap=-0.1360 d_spot=-0.1300 bin1m=-0.0350 rsi=41.0 up_ask=0.300 dn_ask=0.600 spread=+0.100 | SIGNAL DOWN
[2026-11-20 10:38:04] SKIP ETH wide_spread 0.100
[2026-11-20 10:38:05] SCAN SOL slug=sol-updown-5m-1795171200 secs=65/235 d_twap=-0.0210 d_spot=-0.0190 bin1m=+0.0040 rsi=49.8 up_ask=0.490 dn_ask=0.520 spread=-0.010 | |d_twap|=0.0210 not in [0.1,0.3]
[2026-11-20 10:38:05] SCAN DOGE slug=doge-updown-5m-1795171200 secs=65/235 d_twap=+0.1040 d_spot=+0.1100 bin1m=+0.0120 rsi=55.1 up_ask=0.570 dn_ask=0.440 spread=-0.010 | bin1m_no_confirm UP bin1m=+0.0120
[2026-11-20 10:38:31] SETTINGS reloaded changed=TP_OFFSET hot=TP_OFFSET restart=- bet=1.0 tp=0.09 sl=0.2 cb=2 range=[0.45,0.7] coins=btc,eth,xrp,sol,doge,hype
[2026-11-20 10:38:40] HOLD BTC UP entry=0.550 bid=0.590 tp=0.640 sl=0.350 slug=btc-updown-5m-1795171200
[2026-11-20 10:38:52] ORDER_ERR XRP UP: order couldn't be fully filled (FOK)
[2026-11-20 10:39:02] EXIT_TP BTC UP bid=0.640 tp=0.640 filled=True status=matched DRY_RUN
[2026-11-20 10:40:10] COINS hot-reload coins=btc,eth,xrp,sol,doge,hype añadidas=hype
"""


def demo_ptb_state(**_k):
    return {"running": True, "ours": True, "pid": 4243, "how": "panel", "file_age": 1.0}


async def main(out: str, view: str) -> None:
    if view == "real":
        app = Caro5mApp(first_run_wizard=False)
        async with app.run_test(size=(118, 36)) as pilot:
            await pilot.pause(2.5)
            open(out, "w", encoding="utf-8").write(app.export_screenshot(title="caro5m — panel de control (bot real en modo PRUEBA)"))
        print("escrito", out)
        return
    launcher.ptb_state = demo_ptb_state  # demo: el PTB aparece «en marcha (panel)» sin lanzar nada
    config.save_settings({"COINS": "btc,eth,xrp,sol,doge,hype"})
    if view == "version":
        st = demo_status.build_showcase(version="1.2.2")
        st["restart_pending"] = [{"key": "proxy", "label": "Proxy"}]
    else:
        st = demo_status.build_showcase(with_position=(view != "modo"))
    if view == "modo":
        st["wallet"]["balance_usdc"] = 25.0
    status.write_status(st)
    config.log_path().parent.mkdir(parents=True, exist_ok=True)
    config.log_path().write_text(DEMO_LOG, encoding="utf-8")
    app = Caro5mApp(first_run_wizard=(view == "cartera"))  # «cartera» = primer arranque: se abre sola la configuración guiada
    async with app.run_test(size=SIZES.get(view, (118, 38))) as pilot:
        await pilot.pause(1.0)
        if view == "settings":
            await pilot.press("c")
            await pilot.pause(0.5)
            await pilot.press("down")          # fila «Take-profit»
            await pilot.pause(0.2)
            await pilot.press("right")         # 0,08 -> 0,09: «Guardado y aplicado en caliente»
            await pilot.pause(0.5)
        elif view == "coins":
            await pilot.press("c")
            await pilot.pause(0.5)
            for _ in range(config.EDITABLE_KEYS.index("COINS")):
                await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause(0.6)
        elif view == "log":
            await pilot.press("l")
            await pilot.pause(1.5)
        elif view == "modo":
            await pilot.press("m")
            await pilot.pause(0.6)
        elif view == "cuenta":
            # datos 100 % de demostración (claves y direcciones inventadas; todo sale enmascarado)
            from caro5m import wizard
            from caro5m.tui.app import checks_text
            wizard.write_env({"POLY_PRIVATE_KEY": "0x" + "11" * 32, "POLY_SAFE_ADDRESS": "0x" + "22" * 20,
                              "POLYMARKET_PROXY_LIST": "http://demo:demo@203.0.113.10:8080",
                              "POLY_CLOB_API_KEY": "d3m0a1b2-c3d4-e5f6-a7b8-c9d0e1f2a3b4", "POLY_CLOB_API_SECRET": "x" * 20, "POLY_CLOB_API_PASSPHRASE": "y" * 20})
            config.save_settings({"POLY_SIGNATURE_TYPE": "2"})
            await pilot.press("a")
            await pilot.pause(0.6)
            C = account_mod.Check
            app.screen._out(checks_text([
                C("shm", "ok", "Carpeta compartida (SHM)", "data/shm"),
                C("wallet", "ok", "Cartera", "cartera del navegador (MetaMask, Rabby…) · clave configurada · funder 0x2222…2222"),
                C("dns", "ok", "DNS de polymarket.com", "resuelve"),
                C("clock", "ok", "Reloj del sistema", "desfase +0.3 s"),
                C("geo_proxy", "ok", "Proxy configurado", "Por el proxy: 96 ms al CLOB."),
                C("rpc", "ok", "RPC de Polygon", "3/3 responden"),
                C("binance", "ok", "Binance", "https://api.binance.com accesible"),
                C("creds", "ok", "Claves CLOB", "d3m0…b4 · válidas"),
                C("funds", "ok", "Saldo USDC", "25,00 (mínimo 1,03 = apuesta + 3 % de margen para comisiones)"),
                C("allow", "ok", "Allowance del exchange", "aprobado"),
                C("mode", "info", "Modo", "PRUEBA por defecto (DRY_RUN=1): nada de esto activa órdenes reales"),
            ]))
            app.screen._show_done(True)
            await pilot.pause(0.4)
        elif view == "proxy":
            # proxy inventado (IP de documentación 203.0.113.x) en formato «host:puerto:usuario:clave» tal como lo dan los proveedores
            await pilot.press("a")
            await pilot.pause(0.5)
            await pilot.press("p")
            await pilot.pause(0.4)
            app.screen.query_one("#proxy-input").value = "203.0.113.10:8080:demo:demo"
            await pilot.pause(0.4)
        elif view == "errores":
            from caro5m import setuplog
            # traceback de DEMOSTRACIÓN (rutas genéricas): así se ve un fallo de «Guardar y continuar» con el DNS bloqueado
            demo = ("[2026-11-20 10:41:07] ERROR claves CLOB (create_or_derive_api_key) — firma=2 proxy=no\n"
                    "Traceback (most recent call last):\n"
                    "  File \".../py_clob_client_v2/http_helpers/helpers.py\", line 54, in request\n"
                    "    resp = _http_client.request(method=method, url=endpoint, ...)\n"
                    "httpx.ConnectError: [Errno -5] No address associated with hostname\n\n"
                    "During handling of the above exception, another exception occurred:\n\n"
                    "Traceback (most recent call last):\n"
                    "  File \".../caro5m/account.py\", line 505, in generate_credentials\n"
                    "    creds = call()\n"
                    "py_clob_client_v2.exceptions.PolyApiException: PolyApiException[status_code=None, error_message=Request exception!]\n"
                    + "-" * 60 + "\n")
            setuplog._append(setuplog.errors_log(), demo)
            setuplog.event("guardar y continuar detenido en el paso del proxy: " + account_mod.NEED_PROXY_MSG, "WARN")
            await pilot.press("a")
            await pilot.pause(0.4)
            await pilot.press("e")
            await pilot.pause(0.6)
        elif view == "cartera":
            await pilot.pause(0.8)  # ya se abrió sola la pantalla guiada
        elif view == "conflicto":
            from caro5m.tui.app import ChoiceScreen
            account_mod._lookup_fn = lambda signer, **k: {"proxy_wallet": "0x" + "33" * 20, "name": None, "error": None}  # perfil inventado
            signer = account_mod.evaluate_wallet("browser", "0x" + "11" * 32, "", stored={}, lookup=False).signer
            plan2 = account_mod.evaluate_wallet("browser", "0x" + "11" * 32, signer, stored={})
            await pilot.press("a")
            await pilot.pause(0.5)
            app.screen.query_one("#acc-funder").value = plan2.signer
            await pilot.pause(0.3)
            app.push_screen(ChoiceScreen("⚠ " + plan2.conflict["message"] + "\n\n¿Qué quieres hacer?", plan2.conflict["options"]))
            await pilot.pause(0.5)
        open(out, "w", encoding="utf-8").write(app.export_screenshot(title="caro5m — panel de control (datos de demostración)"))
    print("escrito", out)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "docs/tui.svg", sys.argv[2] if len(sys.argv) > 2 else "main"))
