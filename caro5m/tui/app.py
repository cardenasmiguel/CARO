"""Panel de control (TUI) de caro5m, con Textual. No toca la red de trading ni lee claves:
lee ``status.json`` del bot y escribe órdenes de control en ``control/``. Solo la pantalla de cuenta [A] escribe el .env."""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual import events
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, RadioButton, RadioSet, RichLog, Select, Static, TextArea

from .. import __version__, account, checks, coins as coins_mod, compat, config, launcher, setup, setuplog, shutdown, status as status_mod, wizard
from . import render


# ── diálogos ───────────────────────────────────────────────────────
class ConfirmScreen(ModalScreen[bool]):
    """Confirmación S/N."""

    BINDINGS = [
        Binding("s,S,y,Y", "yes", "Sí"),
        Binding("n,N,escape", "no", "No"),
    ]

    def __init__(self, text: str, danger: bool = False) -> None:
        super().__init__()
        self._text = text
        self._danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box", classes="danger" if self._danger else ""):
            yield Static(self._text, id="confirm-text")
            yield Static("[b]\\[S][/b] Sí      [b]\\[N][/b] No", id="confirm-keys")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class InputScreen(ModalScreen[Optional[str]]):
    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(self, title: str, value: str, hint: str = "") -> None:
        super().__init__()
        self._title, self._value, self._hint = title, value, hint

    def compose(self) -> ComposeResult:
        with Vertical(id="input-box"):
            yield Label(self._title, id="input-title")
            yield Input(value=self._value, id="input-field")
            yield Static(self._hint + "\nEnter = guardar · Esc = cancelar", id="input-hint")

    def on_mount(self) -> None:
        self.query_one("#input-field", Input).focus()

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── ajustes ────────────────────────────────────────────────────────
STEPS = {"BET_SIZE": 0.5, "TP_OFFSET": 0.01, "SL_OFFSET": 0.01, "CB_LOSSES": 1, "CB_COOLDOWN": 300,
         "MIN_TOKEN": 0.01, "MAX_TOKEN": 0.01, "POSITION_TIMEOUT": 30}


class CoinsScreen(ModalScreen[Optional[List[str]]]):
    """Selección de monedas (casillas). Devuelve la lista elegida o None si se cancela."""

    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(self, current: List[str]) -> None:
        super().__init__()
        self._current = set(current)

    def compose(self) -> ComposeResult:
        with Vertical(id="coins-box"):
            yield Label("Monedas a operar (mercados Up/Down de 5 min de Polymarket)", id="coins-title")
            for c in coins_mod.SUPPORTED:
                spec = coins_mod.COIN_TABLE[c]
                note = "" if spec.chainlink_polygon else "  (sin Chainlink útil en Polygon: precio de Binance)"
                yield Checkbox(f"{c.upper():<5} {spec.name}{note}", value=c in self._current, id=f"coin-{c}")
            yield Static("", id="coins-msg")
            with Horizontal(id="coins-buttons"):
                yield Button("Guardar", id="coins-save", variant="primary")
                yield Button("Cancelar", id="coins-cancel")
            yield Static("Espacio marca/desmarca · Tab siguiente · Esc cancelar. Se aplica en caliente, sin reiniciar el bot.", id="coins-hint")

    def selected(self) -> List[str]:
        return [c for c in coins_mod.SUPPORTED if self.query_one(f"#coin-{c}", Checkbox).value]

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        if ev.button.id == "coins-save":
            sel = self.selected()
            if not sel:
                self.query_one("#coins-msg", Static).update(Text("✖ Elige al menos una moneda.", style="bold red"))
                return
            self.dismiss(sel)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SettingsScreen(Screen):
    BINDINGS = [
        Binding("escape,q,Q", "app.pop_screen", "Volver"),
        Binding("enter", "edit", "Editar", priority=True),
        Binding("left", "nudge(-1)", "−", show=False, priority=True),
        Binding("right", "nudge(1)", "+", show=False, priority=True),
        Binding("a,A", "account", "Cuenta"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(" Ajustes ", id="screen-title")
        yield DataTable(id="settings-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="settings-msg")
        yield Static("↑↓ elegir · Enter editar · ←→ ajustar valor · \\[A] Cuenta (claves CLOB, proxy…) · Esc volver   ● al instante   ⟳ reiniciar el bot\n"
                     "Se guarda en config/settings.env (sin secretos). La clave privada, el Safe y el proxy se cambian con \\[W].",
                     id="settings-help")

    def on_mount(self) -> None:
        t = self.query_one("#settings-table", DataTable)
        t.add_columns("Ajuste", "Valor", "")
        self.reload()
        t.focus()

    def reload(self) -> None:
        t = self.query_one("#settings-table", DataTable)
        keep = t.cursor_row
        t.clear()
        vals = config.load_settings()
        for k in config.EDITABLE_KEYS:
            spec = config.SPEC_BY_KEY[k]
            v = vals[k]
            shown = ", ".join(x.upper() if k == "COINS" else x for x in v) if isinstance(v, list) else ("sí" if v is True else "no" if v is False else f"{v:.2f}".replace(".", ",") if isinstance(v, float) else str(v))
            if len(shown) > 52:
                shown = shown[:51] + "…"
            t.add_row(spec.label, shown, "●" if spec.hot else "⟳", key=k)
        if t.row_count:
            t.move_cursor(row=min(keep, t.row_count - 1))

    def _current_key(self) -> Optional[str]:
        t = self.query_one("#settings-table", DataTable)
        if not t.row_count:
            return None
        return t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value

    def _msg(self, text: str, ok: bool = True) -> None:
        self.query_one("#settings-msg", Static).update(Text(text, style="green" if ok else "bold red"))

    def _save(self, key: str, raw: str) -> bool:
        try:
            config.save_settings({key: raw})
            spec = config.SPEC_BY_KEY[key]
            if spec.hot:
                self._msg("✔ Guardado y aplicado en caliente: el bot lo lee en unos segundos (sin reiniciar).")
            else:
                self._msg("✔ Guardado. ⟳ Reinicia el bot para aplicarlo (tecla R en el panel).")
                offer = getattr(self.app, "offer_restart", None)
                if offer:
                    offer(f"Has cambiado «{spec.label}», que solo se aplica al reiniciar el bot.")
            ok = True
        except ValueError as e:
            self._msg(f"✖ {e}", ok=False)
            ok = False
        self.reload()
        return ok

    def action_edit(self) -> None:
        key = self._current_key()
        if not key:
            return
        spec = config.SPEC_BY_KEY[key]
        cur = config.load_settings()[key]
        if spec.kind == "bool":
            self._save(key, "0" if cur else "1")
            return
        if spec.kind == "coins":
            def picked(sel: Optional[List[str]]) -> None:
                if sel is None or sel == list(cur):
                    return
                if self._save(key, ",".join(sel)):
                    gone = [c for c in cur if c not in sel]
                    held = [c for c in gone if c in ((getattr(self.app, "st", None) or {}).get("positions") or {})]
                    msg = f"✔ Monedas: {coins_mod.title(sel)}. Se aplica en caliente, sin reiniciar"
                    if held:
                        msg += f" (la posición abierta de {coins_mod.title(held)} se sigue gestionando hasta cerrarla)"
                    self._msg(msg + ".")
                    self.app.notify(msg + ". El PTB se reinicia solo para seguir las monedas nuevas.", timeout=8)

            self.app.push_screen(CoinsScreen(list(cur)), picked)
            return
        raw = ", ".join(cur) if isinstance(cur, list) else str(cur)

        def done(val: Optional[str]) -> None:
            if val is not None:
                self._save(key, val)

        self.app.push_screen(InputScreen(spec.label, raw, spec.help), done)

    def action_account(self) -> None:
        self.app.push_screen(AccountScreen())

    def action_nudge(self, direction: int) -> None:
        key = self._current_key()
        if not key or key not in STEPS:
            return
        spec = config.SPEC_BY_KEY[key]
        cur = float(config.load_settings()[key])
        new = round(cur + direction * STEPS[key], 4)
        self._save(key, f"{int(new)}" if spec.kind == "int" else f"{new:.2f}")


# ── log ────────────────────────────────────────────────────────────
OPS_WORDS = ("ORDER_", "FILL", "EXIT_", "SELL", "DRY_RUN", "RESOLVE", "CIRCUIT", "MODE", "CONTROL", "SETTINGS", "COOLDOWN")
ERR_WORDS = ("ERR", "FATAL", "WARN", "failed", "UNAVAILABLE", "Traceback")


def log_class(line: str) -> str:
    if any(w in line for w in ERR_WORDS):
        return "err"
    if any(w in line for w in OPS_WORDS):
        return "ops"
    return "other"


class LogScreen(Screen):
    """Log del bot en vivo (data/caro5m.log): se sigue solo cada ~1 s, se puede seleccionar y copiar (C), igual que el visor de errores.
    Funciona también con el bot arrancado desde el panel (escribe en ese mismo fichero)."""

    BINDINGS = [
        Binding("escape,q,Q", "app.pop_screen", "Volver", priority=True),
        Binding("1", "filter('all')", "Todo", priority=True),
        Binding("2", "filter('ops')", "Operaciones", priority=True),
        Binding("3", "filter('err')", "Errores", priority=True),
        Binding("c,C", "copy", "Copiar", priority=True),
        Binding("f,F", "follow", "Seguir", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.mode = "all"
        self.follow = True
        self._sig: Any = None

    def compose(self) -> ComposeResult:
        yield Static("", id="screen-title")
        yield TextArea("", id="log", read_only=True, soft_wrap=False, show_line_numbers=False)
        yield Static("", id="log-msg")
        yield Static("[1] Todo   [2] Operaciones   [3] Errores   [F] Seguir/parar   [C] Copiar   [Esc] Volver", id="log-help")

    def on_mount(self) -> None:
        self._title()
        self.set_interval(1.0, self.refresh_log)
        self.refresh_log(force=True)
        self.query_one("#log", TextArea).focus()

    def _title(self) -> None:
        names = {"all": "Todo", "ops": "Operaciones", "err": "Errores"}
        self.query_one("#screen-title", Static).update(
            f" Log del bot — filtro: {names[self.mode]} — {'siguiendo en vivo' if self.follow else 'seguimiento en pausa'} ")

    def _msg(self, text: str, style: str = "grey62") -> None:
        self.query_one("#log-msg", Static).update(Text(" " + text if text else "", style=style))

    def action_filter(self, mode: str) -> None:
        self.mode = mode
        self._title()
        self.refresh_log(force=True)

    def action_follow(self) -> None:
        self.follow = not self.follow
        self._title()
        if self.follow:
            self.refresh_log(force=True)

    def _read_tail(self, max_bytes: int = 300_000) -> List[str]:
        p = config.log_path()
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
        lines = data.splitlines()
        return lines[1:] if size > max_bytes else lines

    def _set_text(self, text: str, to_end: bool) -> None:
        ta = self.query_one("#log", TextArea)
        ta.load_text(text)
        if to_end:
            ta.move_cursor(ta.document.end)
            ta.scroll_end(animate=False)

    def refresh_log(self, force: bool = False) -> None:
        try:
            st = config.log_path().stat()
        except OSError:
            if force or self._sig != "missing":
                self._set_text(f"Todavía no hay log del bot: {config.log_path()}\n\n"
                               "Se crea en cuanto el bot arranca. Pulsa [Esc] y luego [B] para arrancarlo desde el panel "
                               f"(o ejecuta {compat.bot_command()} en otra terminal). Esta pantalla se actualiza sola.", False)
                self._msg("El fichero de log aún no existe.", "yellow")
                self._sig = "missing"
            return
        sig = (st.st_size, st.st_mtime, self.mode)
        if not force and (sig == self._sig or not self.follow):
            return
        ta = self.query_one("#log", TextArea)
        if not force and ta.selected_text:
            return  # no pisar una selección que el usuario está haciendo para copiar
        self._sig = sig
        try:
            raw = self._read_tail()
        except OSError as e:
            self._msg(f"No se pudo leer el log: {e.strerror or e}", "bold red")
            return
        lines = [ln for ln in raw if self.mode == "all" or log_class(ln) == self.mode][-400:]
        self._set_text(setuplog.mask_text("\n".join(lines)) if lines else "(sin líneas para este filtro todavía)", True)
        self._msg(f"{len(lines)} líneas · {config.log_path()} · selecciona con el ratón y pulsa C para copiar" if self.follow else "", "grey62")

    def action_copy(self) -> None:
        ta = self.query_one("#log", TextArea)
        sel = ta.selected_text
        text = sel or ta.text
        how = setuplog.copy_text(text, self.app)
        self._msg(f"{'Selección' if sel else 'Todo el texto'} ({len(text)} caracteres): {how}", "green")


# ── configuración de cuenta ────────────────────────────────────────
def checks_text(cs: List[account.Check]) -> Text:
    col = {"ok": "green", "warn": "yellow", "fail": "bold red", "info": "cyan"}
    t = Text()
    for c in cs:
        t.append(f" {account.ICON[c.level]} ", style=col[c.level])
        t.append(f"{c.title}: ", style="bold")
        t.append(c.detail + "\n")
        if c.fix:
            t.append(f"     → {c.fix}\n", style="grey62")
    ok, warn, fail = account.summarize(cs)
    t.append(f"\n {ok} ✔  {warn} ⚠  {fail} ✖  ", style="bold")
    t.append("listo para operar" if fail == 0 else "hay problemas que impiden operar en real", style="bold green" if fail == 0 else "bold red")
    return t


class ChoiceScreen(ModalScreen[Optional[str]]):
    """Pregunta con varias respuestas (una por botón). Esc / 0 = cancelar (devuelve None)."""

    BINDINGS = [Binding("escape,0", "cancel", "Cancelar"), Binding("1", "pick(0)", "1"), Binding("2", "pick(1)", "2"), Binding("3", "pick(2)", "3")]

    def __init__(self, text: str, options: List[Any]) -> None:
        super().__init__()
        self.text, self.options = text, options

    def compose(self) -> ComposeResult:
        with Vertical(id="choice-box"):
            yield Static(self.text, id="choice-text")
            for n, (code, label) in enumerate(self.options, 1):
                yield Button(f"{n} · {label}", id=f"choice-{code}", variant="primary" if n == 1 else "default")
            yield Static("Esc = cancelar (no se guarda nada)", id="confirm-keys")

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        self.dismiss(str(ev.button.id).removeprefix("choice-"))

    def action_pick(self, idx: int) -> None:
        if idx < len(self.options):
            self.dismiss(self.options[idx][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class ProxyInput(Input):
    """Campo del proxy: si se pega más de una línea (o varios proxies separados por espacios) se queda con el primero válido."""

    def _on_paste(self, event: events.Paste) -> None:
        if event.text:
            text = account.first_valid_proxy_text(event.text) or event.text.splitlines()[0]
            if self.selection.is_empty:
                self.insert_text_at_cursor(text)
            else:
                self.replace(text, *self.selection)
        event.stop()


class ProxyScreen(ModalScreen[Optional[str]]):
    """Pantalla compacta para añadir/cambiar/quitar el proxy (cabe en 80×24). Enter guarda. Muestra al instante cómo se ha
    entendido lo pegado (con las credenciales ocultas) y avisa de formatos no válidos en español claro."""

    BINDINGS = [Binding("escape", "cancel", "Cancelar")]

    def __init__(self, note: str = "") -> None:
        super().__init__()
        self.note = note

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="proxy-box"):
            yield Static("Proxy opcional HTTP/SOCKS (para redes u operadores que bloquean el DNS o restringen la salida)", id="proxy-title")
            yield Static("Pega el proxy tal como te lo dio tu proveedor y pulsa Enter. Vale http://usuario:clave@host:puerto, "
                         "host:puerto:usuario:clave, socks5h://… Con cualquiera el proxy resuelve el DNS de polymarket.com por ti.", id="proxy-help")
            yield ProxyInput(password=True, id="proxy-input", placeholder="http://usuario:clave@host:puerto  ·  host:puerto:usuario:clave")
            yield Static("", id="proxy-preview")
            yield Checkbox("Mostrar lo que escribo", id="proxy-show")
            yield Checkbox("Usar también para leer precios (más lento; reiniciar el bot)", id="proxy-scan")
            yield Static("", id="proxy-result")
            with Horizontal(id="proxy-buttons"):
                yield Button("Guardar", id="proxy-save", variant="success")
                yield Button("Probar", id="proxy-test", variant="primary")
                yield Button("Quitar", id="proxy-clear", variant="warning")
                yield Button("Cancelar", id="proxy-cancel", variant="default")

    def on_mount(self) -> None:
        cur = setup.first_proxy()
        self.query_one("#proxy-scan", Checkbox).value = bool(config.load_settings()["CARO5M_PROXY_SCAN"])
        self._preview("")
        self._result(Text(self.note + "\n", style="yellow") if self.note else Text(""))
        if cur:
            self.query_one("#proxy-result", Static).update(Text.assemble((self.note + "\n" if self.note else "", "yellow"), (f"Proxy guardado ahora: {account.mask_proxy(cur)}", "grey62")))
        self.query_one("#proxy-input", Input).focus()

    def _result(self, t: Text | str) -> None:
        self.query_one("#proxy-result", Static).update(t)

    def _preview(self, val: str) -> None:
        w = self.query_one("#proxy-preview", Static)
        if not val.strip():
            w.update(Text("Escribe o pega el proxy (Ctrl+V / clic derecho / Shift+Insert según tu terminal).", style="grey62"))
            return
        lvl, txt = account.describe_proxy_input(val)
        w.update(Text(txt, style="green" if lvl == "ok" else "bold red"))

    def on_input_changed(self, ev: Input.Changed) -> None:
        if ev.input.id == "proxy-input":
            self._preview(ev.value)

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        if ev.input.id == "proxy-input":
            self._save()

    def on_checkbox_changed(self, ev: Checkbox.Changed) -> None:
        if ev.checkbox.id == "proxy-show":
            self.query_one("#proxy-input", Input).password = not ev.value

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        ev.stop()
        bid = ev.button.id
        if bid == "proxy-save":
            self._save()
        elif bid == "proxy-cancel":
            self.dismiss(None)
        elif bid == "proxy-clear":
            setup.apply_proxy_setting("")
            self.dismiss("✔ Proxy eliminado. Reinicia el bot para aplicarlo.")
        elif bid == "proxy-test":
            typed = self.query_one("#proxy-input", Input).value.strip()
            try:
                proxy = account.parse_proxy(typed) if typed else setup.first_proxy()
            except ValueError as e:
                self._result(Text(f"✖ {e}", style="bold red"))
                return
            if not proxy:
                self._result(Text("No hay ningún proxy que probar: pega uno arriba.", style="yellow"))
                return
            self._result(Text("Probando la salida por el proxy (puede tardar unos segundos)…", style="grey62"))
            self.run_worker(lambda: self._test(proxy), thread=True)

    def _test(self, proxy: str) -> None:
        try:
            r = account.probe_proxy(proxy)
            lvl, txt = account.describe_probe(r)
            setuplog.event(f"prueba de proxy {account.mask_proxy(proxy)}: {lvl} · {txt}", "INFO" if lvl == "ok" else "WARN")
            t = Text(f"{account.ICON[lvl]} {txt}", style={"ok": "green", "warn": "yellow"}.get(lvl, "bold red"))
            if lvl != "ok":
                t.append("\nSi falla: revisa usuario/clave y puerto con tu proveedor, o prueba otro proxy.", style="grey62")
        except Exception as e:
            setuplog.error("probar proxy", e)
            t = Text(f"✖ {account._clean_err(e)} — detalle: tecla E", style="bold red")
        self.app.call_from_thread(self._result, t)

    def _save(self) -> None:
        val = self.query_one("#proxy-input", Input).value.strip()
        scan = self.query_one("#proxy-scan", Checkbox).value
        if not val:
            if setup.first_proxy():
                config.save_settings({"CARO5M_PROXY_SCAN": "1" if scan else "0"})
                self.dismiss("✔ Ajuste de lecturas guardado (el proxy guardado se conserva).")
            else:
                self._result(Text("Todavía no has escrito ningún proxy. Pégalo arriba y pulsa Enter (Esc = salir).", style="yellow"))
            return
        try:
            setup.apply_proxy_setting(val, scan)
        except ValueError as e:
            setuplog.event(f"proxy no guardado: {e}", "WARN")
            self._result(Text(f"✖ {e}", style="bold red"))
            return
        norm = account.parse_proxy(val)
        t = f"✔ Proxy guardado: {account.mask_proxy(norm)}. Reinicia el bot para aplicarlo."
        adv = account.proxy_advice(norm, scan)
        self.dismiss(t + ("\n" + adv if adv else ""))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ErrorsScreen(Screen):
    """Errores y registro de configuración, SELECCIONABLES y copiables (botón Copiar / tecla C; también Ctrl+C sobre una selección).
    Todo va ya enmascarado (sin claves, credenciales ni IPs completas)."""

    BINDINGS = [Binding("escape", "close", "Volver"), Binding("c", "copy", "Copiar", priority=True), Binding("t", "toggle", "Cambiar vista", priority=True),
                Binding("r", "reload", "Actualizar", priority=True)]

    def __init__(self) -> None:
        super().__init__()
        self.which = "errors"

    def compose(self) -> ComposeResult:
        yield Static(" Errores y registro (copiable) ", id="screen-title")
        yield Static("", id="err-path")
        yield TextArea("", id="err-text", read_only=True, soft_wrap=True, show_line_numbers=False)
        yield Static("", id="err-msg")
        with Horizontal(id="err-buttons"):
            yield Button("Copiar (C)", id="err-copy", variant="success")
            yield Button("Errores ⇄ registro (T)", id="err-toggle", variant="primary")
            yield Button("Actualizar (R)", id="err-reload", variant="default")
            yield Button("Volver (Esc)", id="err-close", variant="default")

    def on_mount(self) -> None:
        self._load()
        self.query_one("#err-text", TextArea).focus()

    def _load(self) -> None:
        p = setuplog.errors_log() if self.which == "errors" else setuplog.setup_log()
        ta = self.query_one("#err-text", TextArea)
        ta.load_text(setuplog.report(self.which, 400))
        ta.move_cursor(ta.document.end)
        self.query_one("#err-path", Static).update(
            Text.assemble((" Mostrando: ", "bold"), ("solo errores" if self.which == "errors" else "registro completo", "cyan"),
                          ("   Fichero: ", "bold"), (str(p), "cyan"), ("\n Selecciona con el ratón (o Shift+flechas) y pulsa C para copiar la selección; sin selección copia todo.", "grey62")))

    def action_close(self) -> None:
        self.app.pop_screen()

    def action_reload(self) -> None:
        self._load()
        self.query_one("#err-msg", Static).update(Text(" Actualizado.", style="grey62"))

    def action_toggle(self) -> None:
        self.which = "setup" if self.which == "errors" else "errors"
        self._load()

    def action_copy(self) -> None:
        ta = self.query_one("#err-text", TextArea)
        sel = ta.selected_text
        text = sel or ta.text
        how = setuplog.copy_text(text, self.app)
        self.query_one("#err-msg", Static).update(Text(f" {'Selección' if sel else 'Todo el texto'} ({len(text)} caracteres): {how}", style="green"))

    def on_button_pressed(self, ev: Button.Pressed) -> None:
        bid = ev.button.id
        if bid == "err-copy":
            self.action_copy()
        elif bid == "err-toggle":
            self.action_toggle()
        elif bid == "err-reload":
            self.action_reload()
        elif bid == "err-close":
            self.action_close()


def _steps_text(title: str, steps: List[str]) -> str:
    return title + "\n" + "\n".join(f"  {n}. {t}" for n, t in enumerate(steps, 1))


class AccountScreen(Screen):
    """Todo lo necesario para operar, paso a paso: cartera (con la pregunta «¿cómo entras en Polymarket?»), claves CLOB, proxy,
    comprobaciones y permisos. Los secretos van en campos ocultos y solo se muestran enmascarados."""

    BINDINGS = [Binding("escape", "close", "Volver"), Binding("p,P,f2", "proxy", "Proxy"), Binding("e,E", "errors", "Errores")]

    def __init__(self, guided: bool = False) -> None:
        super().__init__()
        self.guided = guided

    def action_proxy(self) -> None:
        self.open_proxy()

    def action_errors(self) -> None:
        self.app.push_screen(ErrorsScreen())

    def open_proxy(self, then: Optional[Any] = None, note: str = "") -> None:
        def done(msg: Optional[str]) -> None:
            if msg:
                setuplog.event(msg.splitlines()[0])
                self._out(Text(msg, style="green"))
                self._refresh()
                if then is not None:
                    then()

        self.app.push_screen(ProxyScreen(note), done)

    def _log_hint(self) -> str:
        return f"Detalle completo (copiable, sin secretos): pulsa E · {setuplog.short_path(setuplog.errors_log())}"

    def _bg(self, name: str, fn: Any) -> None:
        """Ejecuta ``fn`` en un hilo; si algo inesperado falla se registra completo y se muestra sin romper la pantalla."""
        def run() -> None:
            try:
                fn()
            except Exception as e:
                setuplog.error(name, e)
                self._out_from_thread(Text(f"✖ {name}: {account._clean_err(e)}\n  {self._log_hint()}", style="bold red"))

        self.run_worker(run, thread=True)

    def _need_proxy(self, then: Any) -> None:
        self.open_proxy(then, account.NEED_PROXY_MSG)

    def action_close(self) -> None:
        incomplete = account.setup_incomplete()
        if self.guided and incomplete:
            account.mark_setup_skipped()  # no volver a abrir solo en cada arranque; se llega con [A]
        self.app.pop_screen()
        if self.guided or self.query_one("#acc-done").display:
            running = bool(getattr(self.app, "running", False))
            self.app.notify(("Cuenta pendiente: termínala cuando quieras con [A]. " if incomplete else "Estás en el panel del bot. ")
                            + ("[M] modo REAL · [L] log." if running else "[B] arranca el bot (modo PRUEBA) · [M] arranca y pasa a REAL · [L] log."),
                            timeout=10)

    def compose(self) -> ComposeResult:
        yield Static(" Configuración de cuenta ", id="screen-title")
        yield Static("", id="acc-summary")
        with VerticalScroll(id="acc-scroll"):
            yield Static("1 · Tu cartera", classes="acc-h")
            yield Static("¿Cómo entras en polymarket.com?", id="acc-q")
            with RadioSet(id="acc-wallet"):
                for k, v in account.WALLET_CHOICES.items():
                    yield RadioButton(v["label"], id=f"acc-w-{k}")
            yield Static("", id="acc-wallet-help", classes="acc-help")
            yield Input(password=True, id="acc-key", placeholder="Pega aquí tu clave privada 0x… (vacío = conservar la guardada)")
            yield Static("", id="acc-key-err", classes="acc-err")
            yield Static("", id="acc-signer", classes="acc-help")
            yield Static("", id="acc-funder-help", classes="acc-help")
            yield Input(id="acc-funder", placeholder="Dirección de tu cuenta de Polymarket 0x… (vacío = buscarla por mí)")
            yield Static("", id="acc-funder-err", classes="acc-err")
            yield Static("", id="acc-hint", classes="acc-help")
            with Horizontal(classes="acc-row"):
                yield Button("Guardar y continuar ▶", id="acc-save-go", variant="success")
                yield Button("Solo guardar", id="acc-save-wallet", variant="primary")
                yield Button("Buscar mi dirección", id="acc-lookup", variant="default")
            yield Static("2 · Proxy opcional HTTP/SOCKS (para redes u operadores que bloquean el DNS o restringen la salida)", classes="acc-h")
            yield Static("", id="acc-proxy-status")
            yield Static("Proxy HTTP/SOCKS opcional, por si tu red u operador bloquea el DNS o restringe el tráfico de salida: las órdenes y la autenticación saldrán por él y él resolverá el DNS. "
                         "Eres responsable de cumplir la ley de tu país y las condiciones de Polymarket (no uses un proxy para eludir sus restricciones geográficas).", classes="acc-help")
            with Horizontal(classes="acc-row"):
                yield Button("Añadir / cambiar proxy… (P)", id="acc-proxy-open", variant="success")
                yield Button("Probar proxy", id="acc-proxy-test", variant="primary")
                yield Button("Quitar proxy", id="acc-proxy-clear", variant="warning")
            yield Static("3 · Claves CLOB", classes="acc-h")
            with Horizontal(classes="acc-row"):
                yield Button("Generar / derivar", id="acc-keys-gen", variant="success")
                yield Button("Regenerar (claves nuevas)", id="acc-keys-regen", variant="warning")
                yield Button("Validar", id="acc-keys-validate", variant="primary")
            yield Static("4 · Comprobaciones y permisos", classes="acc-h")
            with Horizontal(classes="acc-row"):
                yield Button("Ejecutar comprobaciones", id="acc-checks", variant="primary")
                yield Button("Permisos (allowances)…", id="acc-approve", variant="warning")
                yield Button("Errores / copiar (E)", id="acc-errors", variant="default")
        with Vertical(id="acc-bottom"):
            with Horizontal(id="acc-done"):
                yield Static("", id="acc-done-text")
                yield Button("Ir al panel ▶", id="acc-go-panel", variant="success")
            with VerticalScroll(id="acc-out"):
                yield Static("", id="acc-out-text")
            yield Static(Text.assemble((" Esc ", "bold black on grey70"), (" volver al panel   ", ""), (" P ", "bold black on grey70"), (" proxy   ", ""),
                                       (" E ", "bold black on grey70"), (" errores (copiar)   ", ""), (" ^L ", "bold black on grey70"), (" redibujar", "")),
                         id="acc-keys")

    # ---- estado ----
    def _choice(self) -> str:
        pb = self.query_one("#acc-wallet", RadioSet).pressed_button
        return str(pb.id).removeprefix("acc-w-") if pb is not None and pb.id else "browser"

    def _set_choice(self, choice: str) -> None:
        self.query_one(f"#acc-w-{choice}", RadioButton).value = True
        self._wallet_help(choice)

    def _wallet_help(self, choice: str) -> None:
        m = account.WALLET_CHOICES[choice]
        self.query_one("#acc-wallet-help", Static).update(_steps_text("Cómo obtener tu CLAVE PRIVADA:", m["key_steps"]))
        self.query_one("#acc-funder-help", Static).update(_steps_text("Cómo obtener la DIRECCIÓN DE TU CUENTA de Polymarket (donde están tus fondos):", m["funder_steps"]))
        for wid in ("#acc-funder-help", "#acc-funder", "#acc-lookup"):
            self.query_one(wid).display = choice != "own"

    def on_mount(self) -> None:
        s, env = config.load_settings(), account.read_env()
        self._set_choice(account.choice_for_sig(int(s["POLY_SIGNATURE_TYPE"])))
        self.query_one("#acc-funder", Input).value = env.get("POLY_SAFE_ADDRESS") or env.get("POLY_FUNDER_ADDRESS") or ""
        setuplog.install_library_capture()
        for wid in ("#acc-key-err", "#acc-funder-err"):
            self.query_one(wid).display = False
        self.query_one("#acc-done").display = False
        self._refresh()
        self._signer_line()
        self._out(Text(("¡Bienvenido! Vamos a dejar tu cuenta lista en 4 pasos: cartera → claves CLOB → (proxy si hace falta) → comprobaciones.\n" if self.guided else "")
                       + "Elige con ↑/↓, rellena y pulsa «Guardar y continuar». Cada paso se puede saltar (Esc) y repetir cuando quieras con la tecla A.\n"
                       "El modo PRUEBA sigue activado: nada de esto activa órdenes reales ni mueve dinero.", style="grey62"))
        self.query_one("#acc-wallet", RadioSet).focus()

    def _refresh(self) -> None:
        st = setup.current_state()
        w = account.wallet_key(int(st["sig"]))
        t = Text()
        t.append(f" Cartera: {account.WALLET_CHOICES[account.WALLET_TO_CHOICE[w]]['short']}\n", style="bold")
        t.append(f" Clave privada: {'guardada ✔' if st['pk'] else 'sin configurar ✖'}   Fondos: {wizard.mask_address(str(st['funder'])) if st['funder'] else '—'}\n")
        env = account.read_env()
        c = account.stored_credentials(env)
        t.append(f" Claves CLOB: {account.mask_secret(c.api_key) + ' ✔' if c else 'sin generar ✖'}   Proxy: {account.mask_proxy(str(st['proxy'])) if st['proxy'] else 'ninguno'}\n")
        t.append(f" Siguiente paso: {account.next_step(env, int(st['sig']))}", style="bold green")
        t.append("\n Teclas: P proxy · E errores (copiar) · Esc volver al panel · registro: " + setuplog.short_path(setuplog.errors_log()), style="grey62")
        self.query_one("#acc-summary", Static).update(t)
        self.query_one("#acc-proxy-status", Static).update(
            Text.assemble(("Proxy actual: ", "bold"), (account.mask_proxy(str(st["proxy"])) if st["proxy"] else "ninguno — si tu proveedor bloquea Polymarket, pulsa P para añadirlo",
                                                         "green" if st["proxy"] else "yellow")))

    def _signer_line(self) -> None:
        key = self.query_one("#acc-key", Input).value.strip() or account.read_env().get("POLY_PRIVATE_KEY", "")
        signer = None
        if key:
            try:
                signer = wizard.signer_address(wizard.validate_private_key(key))
            except ValueError:
                signer = None
        self.query_one("#acc-signer", Static).update(
            f"✔ Tu clave corresponde a la dirección {wizard.mask_address(signer)} (la dirección de tu cartera; no es donde están tus fondos en Polymarket)." if signer else "")

    def _out(self, t: Text | str) -> None:
        self.query_one("#acc-out-text", Static).update(t if isinstance(t, Text) else Text(t))
        self.query_one("#acc-out", VerticalScroll).scroll_home(animate=False)

    def _out_from_thread(self, t: Text | str) -> None:
        self.app.call_from_thread(self._out, t)

    READY_TEXT = ("✔ Todo listo. Pulsa Esc (o «Ir al panel») para ir al panel del bot.\n"
                  "  Allí: [B] arranca el bot (en modo PRUEBA) · [M] pasa a modo REAL (te pide confirmación).")

    def _show_done(self, ready: bool) -> None:
        """Caja verde final cuando todas las comprobaciones pasan: qué hacer a continuación."""
        box = self.query_one("#acc-done")
        box.display = bool(ready)
        if ready:
            self.query_one("#acc-done-text", Static).update(Text(self.READY_TEXT, style="bold green"))
            setuplog.event("configuración de cuenta lista (todas las comprobaciones sin fallos)")

    def _field_errors(self, errors: Dict[str, str]) -> None:
        for fld, wid in (("key", "#acc-key-err"), ("funder", "#acc-funder-err")):
            w = self.query_one(wid, Static)
            w.update(f"✖ {errors[fld]}" if fld in errors else "")
            w.display = fld in errors

    def on_radio_set_changed(self, ev: RadioSet.Changed) -> None:
        if ev.radio_set.id == "acc-wallet" and ev.pressed is not None and ev.pressed.id:
            self._wallet_help(str(ev.pressed.id).removeprefix("acc-w-"))

    def on_input_changed(self, ev: Input.Changed) -> None:
        if ev.input.id in ("acc-key", "acc-funder"):
            self._signer_line()
            key = self.query_one("#acc-key", Input).value.strip() or account.read_env().get("POLY_PRIVATE_KEY", "")
            funder = self.query_one("#acc-funder", Input).value.strip()
            hint = ""
            try:
                signer = wizard.signer_address(wizard.validate_private_key(key)) if key else None
            except ValueError:
                signer = None
            if signer and funder and funder.lower() == signer.lower() and self._choice() != "own":
                hint = "⚠ Esa dirección es la de tu cartera, no la de tu cuenta de Polymarket. Mira tu perfil en polymarket.com (o pulsa «Buscar mi dirección»)."
            self.query_one("#acc-hint", Static).update(hint)

    # ---- acciones ----
    def on_button_pressed(self, ev: Button.Pressed) -> None:
        bid = ev.button.id
        if bid in ("acc-save-wallet", "acc-save-go"):
            self._save_wallet(go_on=bid == "acc-save-go")
        elif bid == "acc-lookup":
            self._lookup()
        elif bid == "acc-proxy-open":
            self.open_proxy()
        elif bid == "acc-errors":
            self.action_errors()
        elif bid == "acc-proxy-clear":
            setup.apply_proxy_setting("")
            self._out(Text("✔ Proxy eliminado. Reinicia el bot para aplicarlo.", style="green"))
            self._refresh()
        elif bid == "acc-proxy-test":
            self._out(Text("Probando conexión…", style="grey62"))
            self._bg("probar proxy", self._proxy_test)
        elif bid == "acc-keys-gen":
            self._out(Text("Generando claves CLOB (firma L1; no gasta nada)…", style="grey62"))
            self._bg("claves CLOB", lambda: self._keys(False))
        elif bid == "acc-keys-regen":
            def done(ok: Optional[bool]) -> None:
                if ok:
                    self._out(Text("Regenerando claves CLOB con un nonce nuevo…", style="grey62"))
                    self._bg("claves CLOB", lambda: self._keys(True))

            self.app.push_screen(ConfirmScreen("Crear claves CLOB NUEVAS (otro nonce).\n\nLas actuales dejarán de ser las que usa el bot.\n\n¿Seguro?"), done)
        elif bid == "acc-keys-validate":
            self._out(Text("Validando con una lectura autenticada…", style="grey62"))
            self._bg("validar claves", self._validate)
        elif bid == "acc-checks":
            self._out(Text("Ejecutando comprobaciones (solo lectura)…", style="grey62"))
            self._bg("comprobaciones", self._checks)
        elif bid == "acc-approve":
            self._approve()
        elif bid == "acc-go-panel":
            self.action_close()

    def _lookup(self) -> None:
        key = self.query_one("#acc-key", Input).value.strip() or account.read_env().get("POLY_PRIVATE_KEY", "")
        try:
            signer = wizard.signer_address(wizard.validate_private_key(key)) if key else None
        except ValueError:
            signer = None
        if not signer:
            self._out(Text("✖ Primero pega tu clave privada (o guárdala) para poder buscar tu cuenta.", style="bold red"))
            return
        self._out(Text("Consultando tu perfil público de Polymarket (solo se envía tu dirección pública)…", style="grey62"))
        proxy = setup.first_proxy()

        def work() -> None:
            r = account._lookup_fn(signer, proxy=proxy)

            def show() -> None:
                if r.get("proxy_wallet"):
                    self.query_one("#acc-funder", Input).value = r["proxy_wallet"]
                    self._out(Text(f"✔ Polymarket indica que tu cuenta es {wizard.mask_address(r['proxy_wallet'])}"
                                   + (f" ({r['name']})" if r.get("name") else "") + ". La he puesto en el campo; pulsa «Guardar y continuar» para confirmarla.", style="green"))
                elif r.get("error"):
                    self._out(Text(f"⚠ No pude consultarlo: {r['error']}\n"
                                   + ("Si tu proveedor bloquea Polymarket, añade un proxy (tecla P) y vuelve a pulsar «Buscar mi dirección». " if not proxy else "")
                                   + "O cópiala a mano desde tu perfil en polymarket.com.\n" + self._log_hint(), style="yellow"))
                else:
                    self._out(Text("No encontré ninguna cuenta de Polymarket para esa clave. ¿Has entrado alguna vez en polymarket.com con esta cartera? "
                                   "Si no, hazlo una vez (y deposita), o revisa que la clave es la correcta.", style="yellow"))

            self.app.call_from_thread(show)

        self._bg("buscar mi dirección", work)

    def _save_wallet(self, go_on: bool = False) -> None:
        key = self.query_one("#acc-key", Input).value.strip()
        funder = self.query_one("#acc-funder", Input).value.strip()
        choice = self._choice()
        proxy = setup.first_proxy()
        self._out(Text("Revisando…" if not self.query_one("#acc-key", Input).value.strip() and not funder else "Revisando (consulto tu perfil público de Polymarket)…", style="grey62"))

        def work() -> None:
            plan = account.evaluate_wallet(choice, key, funder, proxy=proxy)
            self.app.call_from_thread(self._after_plan, plan, go_on)

        self._bg("revisar la cartera", work)

    def _after_plan(self, plan: account.WalletPlan, go_on: bool) -> None:
        self._field_errors(plan.errors)
        if plan.conflict:
            c = plan.conflict

            def decided(code: Optional[str]) -> None:
                if code is None:
                    self._out(Text("No se ha guardado nada. Revisa los datos (tus campos siguen como estaban).", style="yellow"))
                    return
                self._finish_plan(account.resolve_conflict(plan, code), go_on)

            self.app.push_screen(ChoiceScreen("⚠ " + c["message"] + "\n\n¿Qué quieres hacer?", c["options"]), decided)
            return
        self._finish_plan(plan, go_on)

    def _finish_plan(self, plan: account.WalletPlan, go_on: bool) -> None:
        try:
            r = account.save_plan(plan)
        except ValueError as e:
            self._out(Text(f"✖ {e}", style="bold red"))
            return
        if r["saved_key"]:
            self.query_one("#acc-key", Input).value = ""  # la clave no se queda en pantalla ni en el widget
        if "funder" not in plan.errors:
            self.query_one("#acc-funder", Input).value = r["funder"] or ""
        self._set_choice(plan.choice)
        self._field_errors(plan.errors)
        t = Text()
        if plan.errors:
            t.append("⚠ Guardé lo que estaba bien; corrige los campos marcados en rojo (el resto no se ha perdido).\n", style="yellow")
        t.append(f"✔ Cartera guardada: {account.WALLET_CHOICES[plan.choice]['short']} · "
                 f"clave {'guardada' if r['saved_key'] else 'conservada' if plan.has_key else 'PENDIENTE'} · "
                 f"fondos {wizard.mask_address(r['funder']) if r['funder'] else 'PENDIENTE'}. Reinicia el bot para aplicar el tipo de firma.", style="green")
        for w in plan.warnings:
            t.append(f"\n⚠ {w}", style="yellow")
        self._out(t)
        self._refresh()
        self._signer_line()
        if go_on and not plan.errors and plan.has_key and r["funder"]:
            self._out(Text.assemble(t, ("\nSiguiente: generando tus claves CLOB y comprobando todo (solo lectura)…", "grey62")))
            self._bg("guardar y continuar", lambda: self._continue_flow(t))

    def _continue_flow(self, head: Text) -> None:
        out = Text()
        out.append_text(head)
        out.append("\n")
        setuplog.event("guardar y continuar: claves CLOB + comprobaciones")
        try:
            env = account.read_env()
            if account.stored_credentials(env) is None:
                r = setup.do_generate_keys(False)
                v = r["validation"]
                out.append(f"✔ Claves CLOB generadas y guardadas (key {r['api_key']}). Lectura autenticada: " + ("OK" if v["ok"] else f"FALLÓ ({v['error']})") + "\n",
                           style="green" if v["ok"] else "bold red")
            else:
                out.append("✔ Ya tenías claves CLOB guardadas (no las toco; usa «Regenerar» para crear nuevas).\n", style="green")
        except account.NeedProxy as e:
            # DNS bloqueado y sin proxy: no se sigue (ni claves ni comprobaciones) hasta configurarlo
            setuplog.event(f"guardar y continuar detenido en el paso del proxy: {e}", "WARN")
            out.append(f"✖ {e}\n", style="bold red")
            out.append("Me detengo aquí: sin proxy no puedo seguir. Te abro la pantalla del proxy; al guardarlo continúo yo solo.", style="yellow")
            self._out_from_thread(out)
            self.app.call_from_thread(self._need_proxy, lambda: self._resume_flow(head))
            return
        except Exception as e:
            out.append(f"✖ Claves CLOB: {account.explain_clob_error(e, setup.first_proxy())}\n  {self._log_hint()}\n", style="bold red")
            if not isinstance(e, account.ClobError):
                setuplog.error("guardar y continuar (claves CLOB)", e)
        out.append("\n")
        cs = account.run_preflight()
        out.append_text(checks_text(cs))
        ready = account.ready_to_trade(cs) and bool(account.read_env().get("POLY_PRIVATE_KEY"))
        if ready:
            out.append("\n\n" + self.READY_TEXT, style="bold green")
        self._out_from_thread(out)
        self.app.call_from_thread(self._refresh)
        self.app.call_from_thread(self._show_done, ready)

    def _resume_flow(self, head: Text) -> None:
        self._out(Text.assemble(head, ("\nProxy guardado: continúo con las claves CLOB y las comprobaciones…", "grey62")))
        self._bg("guardar y continuar", lambda: self._continue_flow(head))

    def _proxy_test(self) -> None:
        t = Text()
        proxy = setup.first_proxy()
        col = {"ok": "green", "warn": "yellow", "fail": "bold red"}
        for label, p in (("Sin proxy", None), ("Con proxy", proxy)):
            if label == "Con proxy" and not proxy:
                t.append("Con proxy: no hay ninguno configurado (pulsa P para añadirlo).\n", style="grey62")
                continue
            lvl, txt = account.describe_probe(account.probe_proxy(p))
            setuplog.event(f"prueba {label.lower()}: {lvl} · {txt}", "INFO" if lvl == "ok" else "WARN")
            t.append(f" {account.ICON[lvl]} {label}: {txt}\n", style=col[lvl])
        self._out_from_thread(t)

    def _keys(self, regen: bool) -> None:
        try:
            r = setup.do_generate_keys(regen)
            v = r["validation"]
            t = Text(f"✔ Claves CLOB guardadas en .env (key {r['api_key']}). ", style="green")
            t.append("Comprobación autenticada: OK" if v["ok"] else f"Comprobación FALLÓ: {v['error']}", style="green" if v["ok"] else "bold red")
        except account.NeedProxy as e:
            self._out_from_thread(Text(f"✖ {e}\nTe abro la pantalla del proxy; al guardarlo genero las claves yo solo.", style="bold red"))
            self.app.call_from_thread(self._need_proxy, lambda: self._bg("claves CLOB", lambda: self._keys(regen)))
            return
        except Exception as e:
            msg = account.explain_clob_error(e, setup.first_proxy())
            if not isinstance(e, account.ClobError):
                setuplog.error("claves CLOB", e)
            t = Text(f"✖ Claves CLOB: {msg}\n  {self._log_hint()}", style="bold red")
        self._out_from_thread(t)
        self.app.call_from_thread(self._refresh)

    def _validate(self) -> None:
        env, s = account.read_env(), config.load_settings()
        creds = account.stored_credentials(env)
        if not creds or not env.get("POLY_PRIVATE_KEY"):
            self._out_from_thread(Text("✖ No hay claves CLOB guardadas: pulsa «Generar / derivar».", style="bold red"))
            return
        v = account.validate_credentials(env["POLY_PRIVATE_KEY"], int(s["POLY_SIGNATURE_TYPE"]), env.get("POLY_SAFE_ADDRESS", ""), creds, proxy=setup.first_proxy(env))
        if v["ok"]:
            bal = "—" if v["balance_usdc"] is None else f"{v['balance_usdc']:,.2f} USDC"
            t = Text(f"✔ Claves válidas ({account.mask_secret(creds.api_key)}). Saldo según el CLOB: {bal}. Permisos: "
                     f"{'OK' if v['allowance_ok'] else 'sin aprobar' if v['allowance_ok'] is False else 'desconocido'}.", style="green")
        else:
            t = Text(f"✖ Rechazadas: {v['error']}\n  {self._log_hint()}", style="bold red")
            if v.get("need_proxy"):
                self._out_from_thread(t)
                self.app.call_from_thread(self._need_proxy, lambda: self._bg("validar claves", self._validate))
                return
        self._out_from_thread(t)

    def _checks(self) -> None:
        cs = account.run_preflight()
        t = checks_text(cs)
        ready = account.ready_to_trade(cs) and bool(account.read_env().get("POLY_PRIVATE_KEY"))
        if ready:
            t.append("\n\n" + self.READY_TEXT, style="bold green")
        self._out_from_thread(t)
        self.app.call_from_thread(self._show_done, ready)

    def _approve(self) -> None:
        s, env = config.load_settings(), account.read_env()
        w = account.wallet_key(int(s["POLY_SIGNATURE_TYPE"]))
        self._out(Text(account.approvals_help(w) + "\n", style="yellow"))
        if w != "eoa":
            creds = account.stored_credentials(env)
            if not creds:
                return

            def done(ok: Optional[bool]) -> None:
                if ok:
                    def work() -> None:
                        try:
                            account.refresh_allowance_cache(env["POLY_PRIVATE_KEY"], int(s["POLY_SIGNATURE_TYPE"]), env.get("POLY_SAFE_ADDRESS", ""), creds, proxy=setup.first_proxy(env))
                            self._out_from_thread(Text("✔ El CLOB volverá a leer saldo/permisos. Si siguen sin aprobar, hazlo desde polymarket.com.", style="green"))
                        except Exception as e:
                            self._out_from_thread(Text(f"✖ {account._clean_err(e)}", style="bold red"))
                    self.run_worker(work, thread=True)

            self.app.push_screen(ConfirmScreen("Pedir al CLOB que vuelva a leer el estado de permisos.\n(Gratis; no envía transacciones.)\n\n¿Hacerlo?"), done)
            return
        pk = env.get("POLY_PRIVATE_KEY", "")
        signer = wizard.signer_address(pk) if pk else None
        if not signer:
            self._out(Text("✖ Falta la clave privada.", style="bold red"))
            return

        def inspect() -> None:
            try:
                items = account.approval_status(signer, s["POLYGON_RPCS"], s["CHAIN_ID"])
            except Exception as e:
                self._out_from_thread(Text(f"✖ {account._clean_err(e)}", style="bold red"))
                return
            missing = [i for i in items if not i["done"]]
            if not missing:
                self._out_from_thread(Text("✔ Todos los permisos ya están aprobados.", style="green"))
                return
            names = "\n".join(f"  • {i['name']}" for i in missing)

            def ask() -> None:
                def done(ok: Optional[bool]) -> None:
                    if not ok:
                        self._out(Text("No se ha enviado nada.", style="grey62"))
                        return
                    self._out(Text("Enviando aprobaciones…", style="grey62"))

                    def send() -> None:
                        try:
                            res = account.approve_onchain(pk, s["POLYGON_RPCS"], s["CHAIN_ID"], confirmed=True)
                            self._out_from_thread(Text("\n".join(f"{'✔' if r['status'] == 'confirmada' else '⚠'} {r['status']}: {r['name']} ({r['tx'][:10]}…)" for r in res) or "Nada que aprobar.", style="green"))
                        except Exception as e:
                            self._out_from_thread(Text(f"✖ {account._clean_err(e)}", style="bold red"))
                    self.run_worker(send, thread=True)

                self.app.push_screen(ConfirmScreen(f"ENVIAR {len(missing)} transacción(es) on-chain con tu clave (gasta POL):\n\n{names}\n\n¿Confirmas?", danger=True), done)

            self.app.call_from_thread(ask)

        self.run_worker(inspect, thread=True)


# ── aplicación ─────────────────────────────────────────────────────
CSS = """
Screen { background: $surface; width: 100%; height: 100%; }
#header { height: 1; width: 100%; background: $boost; padding: 0 1; }
#statusline { height: 1; width: 100%; padding: 0 1; }
.panel { border: round $primary; padding: 0 1; margin: 0 0 0 0; width: 100%; }
#row { width: 100%; }
#footer { width: 100%; }
#coins-box { width: 72; max-width: 100%; height: auto; border: heavy $primary; background: $panel; padding: 1 2; }
#coins-box Checkbox { height: 1; border: none; padding: 0; background: transparent; }
#coins-buttons { height: 3; margin-top: 1; }
#coins-buttons Button { margin-right: 1; min-width: 12; }
#coins-msg { height: auto; min-height: 1; }
#coins-hint { height: auto; color: $text-muted; }
#panel_window { height: auto; min-height: 7; }
#panel_position { height: auto; min-height: 3; }
#row { height: auto; min-height: 10; }
#panel_trades { width: 1fr; height: 100%; }
#panel_sys { width: 1fr; height: 100%; }
#panel_warn { height: auto; min-height: 3; }
#footer { height: auto; max-height: 2; dock: bottom; padding: 0 0; }
#acc-summary { height: auto; padding: 0 1; background: $boost; }
#acc-scroll { height: 1fr; padding: 0 2; }
.acc-h { text-style: bold; color: $accent; margin-top: 1; }
.acc-help { color: $text-muted; }
.acc-err { color: red; text-style: bold; height: auto; }
#acc-q { text-style: bold; }
#acc-wallet { height: auto; margin: 0 0 1 0; }
#choice-box { width: 90; height: auto; border: heavy $warning; background: $panel; padding: 1 2; }
#choice-box Button { width: 100%; margin-top: 1; }
ChoiceScreen { align: center middle; }
.acc-row { height: 3; }
.acc-row Button { margin-right: 1; }
#acc-bottom { dock: bottom; height: auto; max-height: 24; }
#acc-out { height: auto; min-height: 6; max-height: 16; border-top: heavy $primary; padding: 0 1; }
#acc-done { height: auto; min-height: 3; border: heavy $success; padding: 0 1; }
#acc-done-text { width: 1fr; height: auto; }
#acc-done Button { min-width: 16; }
#acc-keys { height: 1; background: $boost; }
#acc-out-text { height: auto; }
#proxy-box { width: 78; max-width: 100%; height: auto; max-height: 100%; border: heavy $warning; background: $panel; padding: 0 2; }
#proxy-title { text-style: bold; color: $accent; }
#proxy-help { color: $text-muted; }
#proxy-preview { height: auto; min-height: 1; }
#proxy-result { height: auto; min-height: 2; }
#proxy-box Checkbox { height: 1; border: none; padding: 0; background: transparent; }
#proxy-buttons { height: 3; }
#proxy-buttons Button { margin-right: 1; min-width: 12; }
ProxyScreen { align: center middle; }
#err-path { height: auto; padding: 0 1; background: $boost; }
#err-text { height: 1fr; }
#err-msg { height: auto; min-height: 1; }
#err-buttons { height: 3; }
#err-buttons Button { margin-right: 1; }
#screen-title { background: $primary; color: $text; height: 1; text-style: bold; }
#settings-table { height: 1fr; }
#settings-msg { height: 1; padding: 0 1; }
#settings-help { height: 3; padding: 0 1; color: $text-muted; }
#log { height: 1fr; }
#log-msg { height: auto; min-height: 1; }
#log-help { height: 1; padding: 0 1; color: $text-muted; }
#wizard-box { padding: 0 2; }
#wizard-intro { margin: 1 0; color: $text-muted; }
#wizard-buttons { height: 3; margin-top: 1; }
#w-result { margin-top: 1; }
ConfirmScreen, InputScreen, CoinsScreen { align: center middle; }
#confirm-box { width: 64; height: auto; border: heavy $warning; background: $panel; padding: 1 2; }
#confirm-box.danger { border: heavy red; }
#confirm-keys { margin-top: 1; text-align: center; }
#input-box { width: 70; height: auto; border: round $primary; background: $panel; padding: 1 2; }
"""


class Caro5mApp(App):
    CSS = CSS
    TITLE = "caro5m"
    BINDINGS = [
        Binding("p,P", "pause", "Pausar"),
        Binding("s,S", "sell", "Vender ya"),
        Binding("m,M", "mode", "Modo"),
        Binding("b,B", "bot", "Arrancar/parar bot"),
        Binding("r,R", "restart", "Reiniciar bot"),
        Binding("c,C", "settings", "Ajustes"),
        Binding("l,L", "log", "Log"),
        Binding("w,W", "account", "Cuenta"),
        Binding("a,A", "account", "Cuenta"),
        Binding("e,E", "errors", "Errores"),
        Binding("q,Q", "quit", "Salir"),
        Binding("ctrl+c", "quit", "Salir", show=False),
        Binding("ctrl+l", "redraw", "Redibujar", priority=True),
    ]

    def __init__(self, first_run_wizard: bool = True) -> None:
        super().__init__()
        self.st: Optional[Dict[str, Any]] = None
        self.running = False
        self._first_run_wizard = first_run_wizard
        self.tz = config.load_settings()["CARO5M_TZ"]
        self.disk_path = "/"
        self._starting = False
        self._mode_pending: Optional[Dict[str, Any]] = None  # {target, since}: esperando respuesta del bot a [M]
        self._ptb_proc: Optional[Dict[str, Any]] = None
        self._ptb_checked = 0.0
        self._procs: List[Any] = []  # procesos bot/PTB de ESTA carpeta (búsqueda en segundo plano cada 5 s)
        self._procs_scanned = 0.0
        self._scanning = False
        self._version_seen: Optional[str] = None
        self._quitting = False

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield Static(id="statusline")
        p = Static(id="panel_window", classes="panel")
        p.border_title = "VENTANA ACTUAL"
        yield p
        p = Static(id="panel_position", classes="panel")
        p.border_title = "POSICIÓN ABIERTA"
        yield p
        with Horizontal(id="row"):
            p = Static(id="panel_trades", classes="panel")
            p.border_title = "ÚLTIMAS OPERACIONES"
            yield p
            p = Static(id="panel_sys", classes="panel")
            p.border_title = "ESTADO DEL SISTEMA"
            yield p
        p = Static(id="panel_warn", classes="panel")
        p.border_title = "AVISOS"
        yield p
        yield Static(id="footer")

    def on_mount(self) -> None:
        d = config.data_dir()
        self.disk_path = str(d if d.exists() else d.parent if d.parent.exists() else "/")
        setuplog.install_library_capture()
        self.refresh_view()
        self.set_interval(1.0, self.refresh_view)
        self.scan_processes()
        self.call_after_refresh(self.action_redraw)  # pantalla limpia al arrancar (sin restos de la terminal)
        if self._first_run_wizard and account.setup_incomplete() and not account.setup_skipped():
            self.push_screen(AccountScreen(guided=True))  # primer arranque: configuración guiada de cartera y claves

    # ── redibujar (cambio de tamaño de la terminal / Ctrl+L) ──
    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self.action_redraw)

    def action_redraw(self) -> None:
        """Repinta TODO (layout + contenido): evita restos de un tamaño de terminal anterior."""
        try:
            self.refresh_view()
            self.screen.refresh(repaint=True, layout=True)
            self.refresh(repaint=True, layout=True)
        except Exception:
            pass

    def _ptb_info(self, now: float) -> Optional[Dict[str, Any]]:
        if now - self._ptb_checked >= 5:
            self._ptb_checked = now
            try:
                self._ptb_proc = launcher.ptb_state(use_systemd=False)  # sin systemctl aquí: no bloquear el panel
            except Exception:
                self._ptb_proc = None
        return self._ptb_proc

    # ── vista ──
    def refresh_view(self) -> None:
        try:
            self._refresh_view()
        except NoMatches:  # pantalla principal montándose o cerrándose (al salir): se pinta en el siguiente segundo
            pass

    def _refresh_view(self) -> None:
        launcher.reap()
        st = status_mod.read_status()
        now = time.time()
        running = status_mod.bot_running(st, now)
        if st and running:
            st = dict(st)
            st["paused"] = status_mod.is_paused()  # reflejo inmediato de [P]
        self.st, self.running = st, running
        width = max(60, self.size.width or 100)
        self.query_one("#header", Static).update(render.header(st, running, now, (st or {}).get("tz") or self.tz, width, starting=self._starting))
        self.query_one("#statusline", Static).update(render.status_line(st, now))
        tz = (st or {}).get("tz") or self.tz
        self.query_one("#panel_window", Static).update(render.window_panel(st, now, tz, running))
        self.query_one("#panel_position", Static).update(render.position_panel(st, now, tz))
        self.query_one("#panel_trades", Static).update(render.trades_panel(st, tz))
        self.query_one("#panel_sys", Static).update(render.system_panel(st, now, running, render.disk_percent(self.disk_path), self._ptb_info(now)))
        if now - self._procs_scanned >= 5:
            self.scan_processes()
        banner = render.process_banner(st, running, __version__, self._procs)
        self.query_one("#panel_warn", Static).update(render.warnings_panel(st, tz, running=running, extra=banner))
        self.query_one("#footer", Static).update(render.footer_text(st, bool(st and st.get("paused")), running,
                                                                    restart=bool(banner) and running))
        self._check_restart_pending(st, running)
        self._check_version(st, running)
        self._check_mode_answer(st, now)

    # ── procesos de esta carpeta (bot viejo, duplicados, lanzados a mano) ──
    def scan_processes(self) -> None:
        if self._scanning:
            return
        self._scanning = True
        self._procs_scanned = time.time()

        def work() -> None:
            try:
                self._procs = launcher.project_processes()
            finally:
                self._scanning = False

        try:
            self.run_worker(work, thread=True, group="scan")
        except Exception:
            self._scanning = False

    def _bot_procs(self, fresh: bool = False) -> List[Any]:
        lst = launcher.project_processes() if fresh else self._procs
        if fresh:
            self._procs = lst
        return [p for p in lst if p.kind == "bot"]

    def version_mismatch(self, st: Optional[Dict[str, Any]] = None, running: Optional[bool] = None) -> Optional[str]:
        """Versión del bot en marcha si no es la instalada (None si coincide, no se sabe o el bot está parado)."""
        st = self.st if st is None else st
        running = self.running if running is None else running
        v = str((st or {}).get("version") or "")
        return v if (running and v and v != __version__) else None

    def _check_version(self, st: Optional[Dict[str, Any]], running: bool) -> None:
        v = self.version_mismatch(st, running)
        if v and v != self._version_seen:
            self.notify(f"⟳ El bot en marcha es la versión {v}; tienes instalada la {__version__}. Pulsa R para reiniciarlo.",
                        severity="warning", timeout=15)
        self._version_seen = v

    # ── ajustes que exigen reiniciar (status.restart_pending) ──
    def _check_restart_pending(self, st: Optional[Dict[str, Any]], running: bool) -> None:
        pend = tuple(sorted(str(x.get("label")) for x in ((st or {}).get("restart_pending") or []))) if running else ()
        if pend and pend != getattr(self, "_restart_seen", ()):
            self.notify("⟳ Reinicia el bot para aplicarlo: " + ", ".join(pend) + ". Pulsa [R] (mantiene el modo actual).",
                        severity="warning", timeout=12)
        self._restart_seen = pend

    def action_restart(self) -> None:
        if self._starting:
            self.notify("Ya estoy arrancando el bot, espera unos segundos…", severity="warning")
            return
        pend = [str(x.get("label")) for x in ((self.st or {}).get("restart_pending") or [])]
        why = ("Cambios pendientes de reinicio: " + ", ".join(pend) + ".") if pend else "Reiniciar el bot."
        v = self.version_mismatch()
        if v:
            why = f"El bot en marcha es la versión {v}; tienes instalada la {__version__}. " + ("" if not pend else why)
        bots = self._bot_procs(fresh=True)
        if not self.running and [p for p in bots if not p.systemd]:
            self._stop_flow(bots)  # bot que no actualiza su estado (viejo/colgado): primero pararlo
            return
        self.offer_restart(why.strip())

    # ── respuesta del bot a [M] ──
    MODE_WAIT = 15.0

    def _check_mode_answer(self, st: Optional[Dict[str, Any]], now: float) -> None:
        pend = self._mode_pending
        if not pend:
            return
        mr = (st or {}).get("mode_request") or {}
        if mr and mr.get("target") == pend["target"] and float(mr.get("ts") or 0) >= pend["since"] - 1:
            self._mode_pending = None
            name = "REAL" if pend["target"] == "live" else "PRUEBA"
            if mr.get("result") == "ok":
                self.notify(f"✔ Modo {name}: {mr.get('reason') or 'activado'}.", severity="information", timeout=10)
            else:
                self.notify(f"✖ No se activó el modo {name}: {mr.get('reason') or 'motivo desconocido'}.\n\n"
                            "Lo verás también en AVISOS. El bot sigue en su modo actual.", severity="error", timeout=20)
            return
        if now - pend["since"] > self.MODE_WAIT:
            self._mode_pending = None
            self.notify("El bot no ha respondido a la petición de modo en 15 s. Mira el log con [L] "
                        "(¿bot antiguo, colgado o parado?).", severity="warning", timeout=15)

    # ── acciones ──
    def _need_bot(self) -> bool:
        if self._starting and not self.running:
            self.notify("El bot se está arrancando… espera unos segundos.", severity="warning")
            return False
        if not self.running:
            self.notify("El bot está detenido. Pulsa [B] para arrancarlo desde el panel (o [M] para arrancarlo y pasar a modo real).", severity="warning")
            return False
        return True

    def action_pause(self) -> None:
        if not self._need_bot():
            return
        paused = status_mod.is_paused()
        status_mod.set_paused(not paused)
        self.notify("Bot REANUDADO." if paused else "Bot en PAUSA: no abrirá posiciones nuevas (las abiertas se siguen gestionando).")
        self.refresh_view()

    def action_sell(self) -> None:
        if not self._need_bot():
            return
        pos = (self.st or {}).get("positions") or {}
        if not pos:
            self.notify("No hay ninguna posición abierta que vender.", severity="warning")
            return
        names = ", ".join(f"{c.upper()} {p.get('side')}" for c, p in pos.items())

        def done(ok: Optional[bool]) -> None:
            if ok:
                status_mod.request_sell(list(pos))
                self.notify("Orden de venta enviada al bot.")

        self.push_screen(ConfirmScreen(f"¿Vender YA a precio de mercado?\n\n  {names}", danger=True), done)

    # ── arrancar / parar el bot desde el panel ──
    START_TEXT = ("El bot está parado.\n\n¿Lo arranco ahora desde el panel?\n\n"
                  "Arranca en modo PRUEBA (simulación, sin dinero real).{exit}{more}")

    def _start_flow(self, then_mode: bool) -> None:
        """Pregunta y arranca el bot (scripts/run-bot.sh). Con ``then_mode`` sigue después con el cambio de modo de [M]."""
        if self._starting:
            self.notify("Ya estoy arrancando el bot, espera unos segundos…", severity="warning")
            return
        try:
            launcher.check_can_start(status_mod.read_status())
        except launcher.LaunchError as e:
            self.notify(e.text, severity="warning", timeout=12)
            return
        more = "\n\nCuando esté en marcha te preguntaré si pasas a modo REAL." if then_mode else ""

        def done(ok: Optional[bool]) -> None:
            if ok:
                self._starting = True
                self.notify("Arrancando el bot… (hasta 30 s)")
                self.run_worker(lambda: self._start_worker(then_mode), thread=True)

        ext = (" Sigue funcionando aunque cierres el panel (KEEP_RUNNING_ON_EXIT=1)." if shutdown.keep_running()
               else " Al salir del panel (Q) se paran el bot y el PTB.")
        self.push_screen(ConfirmScreen(self.START_TEXT.format(more=more, exit=ext)), done)

    def _start_worker(self, then_mode: bool) -> None:
        since = time.time()
        try:
            try:
                pid = launcher.start_bot()
            except launcher.LaunchError as e:
                setuplog.event(f"arrancar bot: {e.text}", "WARN")
                self.call_from_thread(self._start_failed, e.text)
                return
            setuplog.event(f"bot arrancado desde el panel (pid {pid})")
            self._maybe_start_ptb()
            ok = launcher.wait_until_fresh(pid, since)
            if ok:
                self.call_from_thread(self._start_ok, pid, then_mode)
            else:
                tail = launcher.log_tail()
                setuplog.event("el bot no llegó a arrancar en 30 s" + (f"\n{tail}" if tail else ""), "ERROR")
                self.call_from_thread(self._start_failed, "El bot no llegó a ponerse en marcha en 30 s.", tail)
        except Exception as e:  # nunca romper el panel
            setuplog.error("arrancar bot desde el panel", e)
            self.call_from_thread(self._start_failed, f"Error al arrancar el bot: {account._clean_err(e)}")
        finally:
            self._starting = False

    def _maybe_start_ptb(self) -> None:
        """Con AUTO_START_PTB=1 (y CARO5M_USE_PTB=1) lanza también el PTB temprano. Nunca rompe el arranque del bot."""
        try:
            cfg = config.load_settings()
            if not (cfg.get("AUTO_START_PTB") and cfg.get("CARO5M_USE_PTB")):
                return
            ppid = launcher.start_ptb()
            self._ptb_checked = 0.0
            if ppid:
                setuplog.event(f"PTB arrancado desde el panel (pid {ppid})")
        except launcher.LaunchError as e:
            setuplog.event(f"arrancar PTB: {e.text}", "WARN")
            self.call_from_thread(self.notify, f"PTB no arrancado: {e.text} El bot funciona igual (modo legado).", severity="warning")
        except Exception as e:
            setuplog.error("arrancar PTB desde el panel", e)

    def _start_failed(self, text: str, tail: str = "") -> None:
        msg = text + (f"\n\nÚltimas líneas:\n{tail}" if tail else "") + "\n\nMás detalle: [L] log del bot · [E] errores."
        self.notify(msg, severity="error", timeout=20)
        self.refresh_view()

    def _start_ok(self, pid: int, then_mode: bool) -> None:
        self.refresh_view()
        live = (self.st or {}).get("mode") == "live"
        if live:
            self.notify(f"Bot en marcha (pid {pid}) y ya en modo REAL (DRY_RUN=0 en tu configuración).", severity="warning")
            return
        self.notify(f"Bot en marcha (pid {pid}) en modo PRUEBA. Log: [L].")
        if then_mode:
            self.action_mode()

    def action_bot(self) -> None:
        if self._starting:
            self.notify("Ya estoy arrancando el bot, espera unos segundos…", severity="warning")
            return
        bots = self._bot_procs(fresh=True)
        if not self.running and not [p for p in bots if not p.systemd]:
            self._start_flow(then_mode=False)
            return
        self._stop_flow(bots)

    def _stop_flow(self, bots: List[Any]) -> None:
        """Parar TODOS los bots de esta carpeta: el lanzado por el panel, uno viejo, uno lanzado a mano o duplicados."""
        own = [p for p in bots if not p.systemd]
        if not own:
            if bots:
                self.notify(f"El bot corre como servicio systemd ({launcher.SERVICE}): páralo con «sudo systemctl stop "
                            f"{launcher.SERVICE}».", severity="warning", timeout=12)
            else:
                self.notify("No encuentro el proceso del bot de esta carpeta (¿otro usuario u otra carpeta?). Si es un servicio: "
                            f"«sudo systemctl stop {launcher.SERVICE}».", severity="warning", timeout=12)
            return
        pids = ", ".join(str(p.pid) for p in own)
        dup = (f"\n\nHay {len(own)} bots de esta carpeta en marcha (duplicados): se paran todos." if len(own) > 1 else "")
        extra = "\n\nHay una posición abierta: seguirá abierta (el bot no la cierra al parar)." if (self.st or {}).get("positions") else ""

        def done(ok: Optional[bool]) -> None:
            if ok:
                self.run_worker(self._stop_worker, thread=True)

        self.push_screen(ConfirmScreen(f"¿Parar el bot (pid {pids})?\n\nDejará de operar y de gestionar posiciones hasta que lo "
                                       f"arranques otra vez.{dup}{extra}", danger=True), done)

    def _stop_worker(self) -> None:
        try:
            pid = launcher.stop_bot()
            rep = launcher.last_report
            many = rep.bots if rep and len(rep.bots) > 1 else []
            setuplog.event(f"bot parado desde el panel (pid {', '.join(map(str, many)) if many else pid})")
            extra = (f" Había {len(many)} bots en marcha (duplicados): parados todos (pid {', '.join(map(str, many))})." if many else "")
            try:
                ppid = launcher.stop_ptb()
                if ppid:
                    setuplog.event(f"PTB parado desde el panel (pid {ppid})")
                    extra = f" PTB parado (pid {ppid})."
            except launcher.LaunchError as e:
                extra = f" PTB: {e.text}"
            self._ptb_checked = 0.0
            self._procs = []
            self._procs_scanned = 0.0
            self.call_from_thread(self._after_stop, f"Bot parado (pid {pid}).{extra}", "information")
        except launcher.LaunchError as e:
            self.call_from_thread(self._after_stop, e.text, "warning")
        except Exception as e:
            setuplog.error("parar bot desde el panel", e)
            self.call_from_thread(self._after_stop, f"No se pudo parar el bot: {account._clean_err(e)}", "error")

    def _after_stop(self, text: str, severity: str) -> None:
        self.notify(text, severity=severity)  # type: ignore[arg-type]
        self.refresh_view()

    def action_mode(self) -> None:
        if not self.running:
            self._start_flow(then_mode=True)  # primero arrancar el bot; el modo real se confirma después
            return
        if (self.st or {}).get("positions"):
            self.notify("Cierra la posición abierta antes de cambiar de modo.", severity="warning")
            return
        live = (self.st or {}).get("mode") == "live"
        target = "dry" if live else "live"
        if target == "live":
            st = self.st or {}
            w = st.get("wallet") or {}
            keys = st.get("keys_present")
            if keys is None:  # bot anterior a 1.2.3
                keys = w.get("configured")
            if not keys:
                self.notify("No se puede pasar a REAL: falta la clave privada o la dirección del Safe. "
                            "Configúralas con [A] (Cuenta).", severity="warning", timeout=12)
                return
            pre = ""
            bal, bet = w.get("balance_usdc"), float(st.get("bet_size") or 1.0)
            need = bet * 1.03
            if bal is None:
                pre = "\n\n[yellow]El saldo aún no se ha leído: el bot lo comprobará antes de cambiar.[/]"
            elif float(bal) < need:
                pre = (f"\n\n[bold red]⚠ Saldo insuficiente: {float(bal):.2f} USDC; hacen falta ≥ {need:.2f} "
                       f"(apuesta {bet:.2f} + comisión). El bot lo rechazará: añade fondos o baja la apuesta en \\[C].[/]")
            text = ("Pasar a modo REAL.\n\nEl bot enviará ÓRDENES REALES con dinero real.\n"
                    "Antes comprobará saldo y allowance (solo lectura)." + pre + "\n\n¿Seguro?")
        else:
            text = "Volver a modo PRUEBA (simulación, no se envían órdenes).\n\n¿Seguro?"

        def done(ok: Optional[bool]) -> None:
            if ok:
                status_mod.set_mode_request(target)
                self._mode_pending = {"target": target, "since": time.time()}
                self.notify("Petición enviada al bot. Te digo el resultado en unos segundos…")

        self.push_screen(ConfirmScreen(text, danger=(target == "live")), done)

    def offer_restart(self, why: str) -> None:
        """Tras un ajuste que exige reiniciar: si el bot lo arrancó este panel, ofrece reiniciarlo (parar + arrancar)."""
        if not self.running:
            self.notify(f"{why} Se aplicará cuando arranques el bot ([B]).")
            return
        bots = self._bot_procs(fresh=True)
        own = [p for p in bots if not p.systemd]
        if not own:
            if bots:
                msg = f"«sudo systemctl restart {launcher.SERVICE}»"
            else:
                msg = f"Ctrl+C y {compat.bot_command()} donde lo lanzaste"
            self.notify(f"{why} No puedo reiniciarlo desde aquí: reinícialo con {msg} para aplicarlo.", severity="warning", timeout=15)
            return
        was_live = (self.st or {}).get("mode") == "live"
        keep = ("Estabas en modo REAL: al volver te pediré otra vez la confirmación de modo REAL (por seguridad)."
                if was_live else "Vuelve en modo PRUEBA, como ahora.")
        if (self.st or {}).get("positions"):
            keep = ("[bold red]⚠ Hay una posición abierta.[/] Queda guardada y el bot la sigue gestionando al volver, pero "
                    "durante unos segundos nadie la vigila.\n\n" + keep)
            has_pos = True
        else:
            has_pos = False

        def done(ok: Optional[bool]) -> None:
            if ok:
                self._starting = True
                self.notify("Reiniciando el bot… " + ("(después confirma el modo REAL)" if was_live else "(modo PRUEBA)"))
                self.run_worker(lambda: self._restart_worker(was_live), thread=True)

        self.push_screen(ConfirmScreen(f"{why}\n\n¿Reinicio el bot ahora para aplicarlo?\n\n{keep}", danger=was_live or has_pos), done)

    def _restart_worker(self, was_live: bool = False) -> None:
        try:
            try:
                launcher.stop_bot()  # todos los bots de esta carpeta (también uno viejo o duplicado)
            except launcher.LaunchError:
                pass
            try:
                launcher.stop_ptb()  # el PTB también lee COINS al arrancar
            except launcher.LaunchError:
                pass
            self._procs = []
            for _ in range(40):  # status.json deja de estar fresco
                if not status_mod.bot_running(status_mod.read_status()):
                    break
                time.sleep(0.5)
        except Exception as e:
            setuplog.error("reiniciar bot (parar)", e)
        self._starting = False
        self.call_from_thread(self._restart_start, was_live)

    def _restart_start(self, was_live: bool = False) -> None:
        self._starting = True
        self.notify("Arrancando el bot… (hasta 30 s)")
        self.run_worker(lambda: self._start_worker(was_live), thread=True)  # si estaba en REAL, vuelve a pedir la confirmación

    # ── salir: para bot + PTB de esta carpeta (salvo KEEP_RUNNING_ON_EXIT=1 o servicio systemd) ──
    async def action_quit(self) -> None:
        if self._quitting:
            return
        if shutdown.keep_running():
            self._quitting = True
            self.exit()
            return
        st = self.st or {}
        if self.running and st.get("mode") == "live" and st.get("positions"):
            def done(ok: Optional[bool]) -> None:
                if ok:
                    self._quit_now()

            self.push_screen(ConfirmScreen("Hay una posición abierta en REAL; al salir el bot se detiene y la posición queda "
                                           "sin gestionar. ¿Salir igualmente?", danger=True), done)
            return
        self._quit_now()

    def _quit_now(self) -> None:
        self._quitting = True
        sysd = [p for p in self._procs if p.systemd]
        if sysd:
            self.notify(f"El bot es el servicio systemd ({launcher.SERVICE}): sigue en marcha al salir.", severity="warning")
        if self.running or [p for p in self._procs if not p.systemd]:
            self.notify("Cerrando bot y PTB…", timeout=10)

        def work() -> None:
            shutdown.shutdown_project(reason="Q")
            self.call_from_thread(self.exit)

        self.run_worker(work, thread=True, group="quit")

    def action_settings(self) -> None:
        self.push_screen(SettingsScreen())

    def action_log(self) -> None:
        self.push_screen(LogScreen())

    def action_account(self) -> None:
        self.push_screen(AccountScreen())

    def action_errors(self) -> None:
        self.push_screen(ErrorsScreen())


def main() -> None:
    compat.ensure_utf8_stdio()
    app = Caro5mApp()
    shutdown.install(exit_cb=lambda: app.exit())  # SIGTERM/SIGHUP/cierre de consola + atexit: nunca dejar el bot huérfano
    try:
        app.run()
    finally:
        shutdown.shutdown_project(reason="fin del panel")  # Ctrl+Q, error… (idempotente: si Q ya lo hizo, no repite)
        shutdown.print_result()


if __name__ == "__main__":
    main()
