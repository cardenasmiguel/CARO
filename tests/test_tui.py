"""TUI sin terminal: Textual en modo headless (pilot). Comprueba pintado y teclas contra ficheros de control."""
import asyncio
import json
import os
import stat
import time

import pytest

from caro5m import config, status
from caro5m.tui.app import Caro5mApp, ConfirmScreen, InputScreen, LogScreen, SettingsScreen

import demo_status


def run(coro):
    return asyncio.run(coro)


def write_demo(**kw):
    st = demo_status.build(**kw)
    st.update({k: v for k, v in kw.items() if k not in ("with_position",)})
    status.write_status(st)
    return st


async def with_app(body, size=(118, 40), wizard=False):
    app = Caro5mApp(first_run_wizard=wizard)
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.3)
        await body(app, pilot)


def svg_text(app):
    import html
    import re

    raw = app.export_screenshot()
    return html.unescape(re.sub(r"<[^>]+>", "", raw)).replace("\xa0", " ")


def test_renders_all_panels_in_spanish():
    write_demo()

    async def body(app, pilot):
        s = svg_text(app)
        for needle in ("PRUEBA", "VENTANA ACTUAL", "POSICI", "LTIMAS OPERACIONES", "ESTADO DEL SISTEMA", "AVISOS",
                       "Vender", "Europe/Madrid", "12,34", "62.268,57", "margen"):
            assert needle in s, needle

    run(with_app(body))


def test_bot_stopped_is_read_only():
    st = demo_status.build()
    st["ts"] = time.time() - 120
    status.write_status(st)

    async def body(app, pilot):
        assert not app.running
        await pilot.press("p")
        await pilot.press("s")
        await pilot.press("m")                    # v1.2.2: ofrece arrancar el bot (aquí se rechaza)
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await pilot.pause(0.2)
        assert not status.is_paused() and status.get_mode_request() is None
        assert not (config.control_dir() / "sell_now").exists()
        assert "DETENIDO" in svg_text(app).upper()

    run(with_app(body))


def test_no_status_file_at_all():
    async def body(app, pilot):
        assert not app.running
        await pilot.press("p")
        assert not status.is_paused()

    run(with_app(body))


def test_pause_toggles_control_file():
    write_demo()

    async def body(app, pilot):
        await pilot.press("p")
        assert status.is_paused()
        await pilot.press("P")
        assert not status.is_paused()

    run(with_app(body))


def test_sell_confirm_yes_no():
    write_demo()

    async def body(app, pilot):
        await pilot.press("s")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await pilot.pause(0.1)
        assert not (config.control_dir() / "sell_now").exists()
        await pilot.press("s")
        await pilot.pause(0.1)
        await pilot.press("s")  # S = sí
        await pilot.pause(0.1)
        req = json.loads((config.control_dir() / "sell_now").read_text())
        assert req["coins"] == ["btc"]

    run(with_app(body))


def test_sell_without_position_does_nothing():
    write_demo(with_position=False)

    async def body(app, pilot):
        await pilot.press("s")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ConfirmScreen)
        assert not (config.control_dir() / "sell_now").exists()

    run(with_app(body))


def test_mode_switch_needs_wallet_and_confirmation():
    st = write_demo(with_position=False)
    st["wallet"]["configured"] = False
    status.write_status(st)

    async def body(app, pilot):
        await pilot.press("m")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ConfirmScreen) and status.get_mode_request() is None

    run(with_app(body))

    write_demo(with_position=False)

    async def body2(app, pilot):
        await pilot.press("m")
        await pilot.pause(0.1)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.press("n")
        await pilot.pause(0.1)
        assert status.get_mode_request() is None
        await pilot.press("m")
        await pilot.pause(0.1)
        await pilot.press("s")
        await pilot.pause(0.1)
        assert status.get_mode_request() == "live"

    run(with_app(body2))


def test_mode_switch_blocked_with_open_position():
    write_demo(with_position=True)

    async def body(app, pilot):
        await pilot.press("m")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, ConfirmScreen) and status.get_mode_request() is None

    run(with_app(body))


def test_back_to_dry_from_live():
    st = write_demo(with_position=False)
    st["mode"] = "live"
    status.write_status(st)

    async def body(app, pilot):
        s = svg_text(app)
        assert "EN VIVO" in s
        await pilot.press("m")
        await pilot.pause(0.1)
        await pilot.press("s")
        await pilot.pause(0.1)
        assert status.get_mode_request() == "dry"

    run(with_app(body))


def test_settings_screen_edit_and_nudge_saves_to_settings_file_only():
    write_demo()

    async def body(app, pilot):
        await pilot.press("c")
        await pilot.pause(0.2)
        assert isinstance(app.screen, SettingsScreen)
        # primera fila: tamaño de apuesta -> flecha derecha = +paso
        before = config.load_settings()["BET_SIZE"]
        await pilot.press("right")
        await pilot.pause(0.1)
        assert config.load_settings()["BET_SIZE"] > before
        # editar con Enter: valor inválido => mensaje de error y NO se guarda
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, InputScreen)
        inp = app.screen.query_one("#input-field")
        inp.value = "0.01"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert config.load_settings()["BET_SIZE"] >= 0.5
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, SettingsScreen)

    run(with_app(body))
    txt = config.settings_file().read_text()
    assert "BET_SIZE" in txt and "PRIVATE" not in txt
    assert not config.env_file().exists()  # el TUI no crea ni toca el .env


def test_log_screen_filters():
    write_demo()
    config.log_path().parent.mkdir(parents=True, exist_ok=True)
    config.log_path().write_text(
        "[t] SCAN BTC slug=x | SIGNAL UP\n[t] FILL BTC UP @0.6 DRY_RUN\n[t] ORDER_ERR BTC UP: boom\n")

    async def body(app, pilot):
        await pilot.press("l")
        await pilot.pause(0.5)
        assert isinstance(app.screen, LogScreen)
        s = svg_text(app)
        assert "SCAN" in s and "FILL" in s and "ORDER_ERR" in s
        await pilot.press("3")
        await pilot.pause(0.3)
        s = svg_text(app)
        assert "ORDER_ERR" in s and "SCAN BTC" not in s
        await pilot.press("2")
        await pilot.pause(0.3)
        s = svg_text(app)
        assert "FILL" in s and "SCAN BTC" not in s
        await pilot.press("escape")

    run(with_app(body))


def test_log_screen_without_log_file():
    async def body(app, pilot):
        await pilot.press("l")
        await pilot.pause(0.3)
        assert isinstance(app.screen, LogScreen)

    run(with_app(body))


def test_quit_key():
    write_demo()

    async def body(app, pilot):
        await pilot.press("q")
        await pilot.pause(0.2)

    run(with_app(body))


def test_cooldown_with_open_position_is_shown():
    st = demo_status.build(with_position=True)
    st["cooldown_until"] = time.time() + 1800
    status.write_status(st)

    async def body(app, pilot):
        s = svg_text(app)
        assert "FRENO" in s and "Freno activo" in s and "se gestiona esta posición" in s

    run(with_app(body, size=(130, 40)))
