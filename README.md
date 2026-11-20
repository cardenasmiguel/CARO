<div align="center">

# caro5m

**CARO** = *Circuit-Armored Risk Optimizer* · **5m** = mercados de 5 minutos.

**Bot open source para los mercados «Up/Down» de 5 minutos de Polymarket, con un panel de control en la terminal.**

BTC · ETH · XRP · SOL · DOGE · BNB · HYPE · ZEC — modo PRUEBA por defecto — Linux · macOS · Windows

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![Versión 1.2.6](https://img.shields.io/badge/versi%C3%B3n-1.2.6-blue)
![Plataformas](https://img.shields.io/badge/plataformas-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)
![Interfaz: Textual](https://img.shields.io/badge/TUI-Textual-5A4FCF)
![Pruebas: pytest](https://img.shields.io/badge/pruebas-pytest-0A9EDC?logo=pytest&logoColor=white)
![Licencia: MIT](https://img.shields.io/badge/licencia-MIT-green)

[English version](README.en.md)

![Panel principal de caro5m con seis monedas en la ventana actual, una posición abierta en BTC, las últimas operaciones, el estado del sistema y los avisos (datos de demostración)](docs/tui.png)

<sub>Todas las capturas usan datos de demostración inventados (`scripts/screenshot.py`): no son operaciones ni saldos reales.</sub>

</div>

> [!WARNING]
> **Software experimental que puede operar con dinero real.** Puedes perder todo lo que deposites. No es asesoramiento financiero, no promete beneficios y no tiene ninguna garantía. Arranca siempre en **modo PRUEBA** (simula las operaciones y no envía ninguna orden). Eres el único responsable de cumplir la ley de tu país y las [condiciones de uso](https://polymarket.com/tos) de Polymarket, incluidas sus [restricciones geográficas](https://docs.polymarket.com/developers/CLOB/geoblock). Si Polymarket no está permitido donde vives, no lo uses.

## Índice

- [Qué hace](#qué-hace)
- [Cómo decide](#cómo-decide)
- [Características](#características)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Primeros pasos](#primeros-pasos)
- [Teclas del panel](#teclas-del-panel)
- [Ajustes](#ajustes)
- [Configuración de cuenta](#configuración-de-cuenta)
- [Arquitectura](#arquitectura)
- [Seguridad](#seguridad)
- [Solución de problemas](#solución-de-problemas)
- [Preguntas frecuentes](#preguntas-frecuentes)
- [Limitaciones conocidas](#limitaciones-conocidas)
- [Desarrollo y pruebas](#desarrollo-y-pruebas)
- [Contribuir](#contribuir)
- [Licencia](#licencia)
- [Cambios](#cambios)

## Qué hace

Polymarket abre cada 5 minutos un mercado por moneda del tipo «¿Sube o baja BTC entre las 10:00 y las 10:05?». Cada mercado tiene dos fichas, **UP** y **DOWN**, que cotizan entre 0,01 y 0,99 USDC. caro5m vigila esos mercados y, cuando el precio de la moneda se ha movido de forma clara y confirmada respecto a la referencia de apertura, **compra el lado favorable** y lo vende con un objetivo de ganancia (*take-profit*), un límite de pérdida (*stop-loss*) o por tiempo.

- **Monedas** (ajuste `COINS`, por defecto `btc,eth`): BTC, ETH, XRP, SOL, DOGE, BNB, HYPE y ZEC. Se eligen con casillas en el panel y se aplican sin reiniciar.
- **Una posición por moneda como máximo**, con una apuesta fija por operación (`BET_SIZE`, por defecto 1 USDC).
- **Modo PRUEBA por defecto**: simula las compras y ventas con los precios reales del libro de órdenes, sin firmar ni enviar nada. El modo REAL se activa a mano, con confirmación, en cada arranque.
- **Panel en la terminal** (en español) para ver todo en vivo y controlarlo con una tecla: arrancar o parar el bot, pausar, vender ya, cambiar de modo, editar ajustes, ver el log y configurar la cuenta.

### Fuentes de precio por moneda

Los mercados se resuelven con el precio de Chainlink de cada moneda. El bot usa estas fuentes (tabla en [`caro5m/coins.py`](caro5m/coins.py)):

| Moneda | Precio en vivo | Impulso y RSI | Referencia temprana (PTB) |
|---|---|---|---|
| BTC, ETH, XRP, SOL, DOGE, BNB | Chainlink `<MONEDA>/USD` en Polygon, leído por RPC público | Binance `<MONEDA>USDT` | RTDS de Polymarket |
| HYPE `*` | Binance spot (no hay Chainlink HYPE/USD en Polygon) | Binance `HYPEUSDT` | RTDS de Polymarket |
| ZEC `*` | Binance spot (el Chainlink ZEC/USD de Polygon solo se actualiza con un 1 % de movimiento o cada 24 h) | Binance `ZECUSDT` | RTDS de Polymarket |

`*` = **modo degradado**: el panel marca la moneda con un asterisco. La estrategia se ajustó con BTC y ETH; las demás monedas no tienen historial con ella, así que pruébalas primero en modo PRUEBA.

## Cómo decide

Todo lo que sigue sale de [`caro5m/bot.py`](caro5m/bot.py). Los umbrales son **relativos** (variaciones en %, precios de ficha entre 0 y 1), así que valen igual para monedas caras y baratas. Con `COIN_SCALE` (avanzado) puedes exigir movimientos X veces mayores a una moneda concreta.

### 1. Datos que lee

| Dato | De dónde | Cada cuánto |
|---|---|---|
| **Precio Chainlink** de la moneda y su **media de 30 s** (TWAP) | Contrato Chainlink en Polygon (`latestRoundData`), rotando entre los RPC de `POLYGON_RPCS` | ~2 s |
| **Referencia de apertura** («price to beat», PTB) | Por defecto, la TWAP de 30 s al empezar la ventana. Si el proceso PTB está en marcha, usa su valor temprano (ver abajo) | una vez por ventana |
| **Impulso de 1 minuto** (`bin1m`) y **RSI(14)** | Binance: precio actual y velas de 1 minuto | ~2 s |
| **Mercado de la ventana** | Gamma API de Polymarket, por el *slug* `<moneda>-updown-5m-<inicio>` | cada ciclo |
| **Mejor compra/venta de UP y DOWN** | Libro de órdenes del CLOB de Polymarket | cada ciclo |

El ciclo del bot se repite cada ~2,5 s más lo que tarden las peticiones.

**PTB temprano (opcional).** El proceso `caro5m.ptb` escucha el WebSocket público de Polymarket (RTDS, precios Chainlink de 1 s) y calcula la referencia como la media de los puntos entre 62 y 3 segundos antes del inicio de la ventana. El bot solo la usa si es de calidad «ok» o «degradada», se emitió entre 10 s antes y 5 s después del inicio, sus datos tienen como mucho 8 s y no se desvía más de un 0,5 % del precio on-chain. Si no, usa el método de siempre (en el panel sale «sin datos (modo legado)»). Es de solo lectura: no usa claves.

### 2. Filtros de entrada

Para cada moneda, en cada ciclo, la señal tiene que pasar **todos** estos filtros:

| # | Filtro | Regla (valores del código) |
|---|---|---|
| 1 | Datos frescos | Binance con menos de 10 s y Chainlink con menos de 45 s |
| 2 | Momento de la ventana | Entre el segundo 45 y el 125 de la ventana, con al menos 120 s por delante |
| 3 | Dirección | El signo de **Δ TWAP**: variación (%) de la TWAP de 30 s respecto al PTB. Positivo = UP, negativo = DOWN |
| 4 | Tamaño del movimiento | \|Δ TWAP\| ≤ 0,30 %. **Carril B** (normal): \|Δ\| ≥ 0,10 %. **Carril A** (temprano): \|Δ\| entre 0,06 % y 0,10 % y como mucho en el segundo 90 |
| 5 | Coherencia | El precio puntual y la TWAP se mueven en el mismo sentido |
| 6 | Veto de impulso contrario | Descarta si Binance 1 min va en contra más de 0,03 % (y UP con `bin1m` < −0,02 %) |
| 7 | Confirmación de Binance | Carril A: `bin1m` a favor ≥ 0,04 %. Carril B: a favor ≥ 0,03 %, o plano (\|bin1m\| ≤ 0,01 %) si el movimiento es fuerte (\|Δ\| ≥ 0,15 %) o temprano (segundo ≤ 60 y \|Δ\| ≥ 0,11 %) |
| 8 | Vetos de RSI | UP con RSI > 70; DOWN con RSI < 10 o > 75; DOWN con `bin1m` > +0,01 % y RSI < 28 |
| 9 | Anti-*fakeout* | Con el historial de los últimos ~60 s: latigazo previo del impulso (≥ 0,08 % en contra en 30 s), impulso no persistente (media de 15 s < 0,02 %), impulso que se apaga mientras el Δ crece, ficha que se abarata ≥ 0,05 desde su máximo de 20 s, carril A UP con RSI < 40 y movimiento agotado en carril B (\|Δ\| ≥ 0,18 % sin impulso ≥ 0,08 %) |
| 10 | Pausa y freno | No entra si pausaste el bot (`P`) o el freno está activo |
| 11 | Margen del libro | Descarta si `1 − (venta UP + venta DOWN)` > 0,08 (el «hueco» del libro: cuánto falta para que las dos ventas sumen 1). En el panel sale como `margen` |
| 12 | Precio de la ficha | El precio de compra del lado elegido entre `MIN_TOKEN` y `MAX_TOKEN` (por defecto 0,45–0,70). Carril A: además ≤ 0,60 |
| 13 | Ficha cara | Carril B con precio > 0,62: exige \|Δ\| ≥ 0,10 % e impulso a favor ≥ 0,03 % |
| 14 | Segunda comprobación | Vuelve a evaluar la señal justo antes de enviar la orden; si cambió, no entra |

Si todo pasa, envía una orden **FOK** (*fill-or-kill*: se ejecuta entera al momento o se cancela) de compra por `BET_SIZE` USDC al precio de venta visto. En modo PRUEBA la ejecución se simula a ese mismo precio. En la columna «Señal» del panel ves la palabra del filtro que paró cada moneda: `delta`, `momento`, `rsi`, `precio`, `margen`, `ventana`, `datos`, etc.

### 3. Salidas

Con una posición abierta, en cada ciclo mira el mejor precio de compra (*bid*) de su ficha:

- **Take-profit**: si el bid llega a `entrada + TP_OFFSET` (por defecto +0,08), vende con una orden FOK. El objetivo nunca pasa de 0,99, el precio máximo de Polymarket. Reinicia la racha de pérdidas.
- **Stop-loss**: si el bid baja a `entrada − SL_OFFSET` (por defecto −0,20) y la posición tiene al menos 20 s, vende con una orden FOK. Suma una pérdida a la racha.
- **Tiempo máximo**: pasados `POSITION_TIMEOUT` segundos (por defecto 360, más que la ventana de 5 min) **intenta una venta FOK al mejor bid**. Si se ejecuta, queda como `TIMEOUT` con PnL. Si no (el mercado suele estar ya resuelto): si la posición pierde, se cierra a 0; si gana, en una cartera EOA se cobra on-chain (`AUTO_REDEEM=1`, en modo PRUEBA solo se simula) y queda como `REDEEM`; en Safe o email el panel mantiene la posición con el aviso «posición resuelta pendiente de cobrar en polymarket.com».
- **Vender ya**: la tecla `S` vende al momento con una orden FOK.

**Freno (*circuit breaker*)**: tras `CB_LOSSES` *stop-loss* seguidos (por defecto 2), no abre posiciones nuevas durante `CB_COOLDOWN` segundos (por defecto 3600). Es global, para todas las monedas, y **solo bloquea entradas**: las posiciones abiertas se siguen gestionando.

### Diagrama

```mermaid
flowchart TD
    A["Cada ~2,5 s, para cada moneda"] --> B{"¿Hay posición abierta?"}
    B -- Sí --> X{"Bid de su ficha"}
    X -- "≥ entrada + TP (máx. 0,99)" --> TP["Vende FOK · TP · racha a 0"]
    X -- "≤ entrada − SL y edad ≥ 20 s" --> SL["Vende FOK · SL · racha +1"]
    SL --> CB{"¿Racha ≥ CB_LOSSES?"}
    CB -- Sí --> CD["Freno: sin entradas nuevas durante CB_COOLDOWN"]
    X -- "edad > POSITION_TIMEOUT" --> TO["Venta FOK al bid; si no, cobra EOA o avisa en el panel"]
    X -- "otro caso" --> H["Mantiene"]
    B -- No --> F{"¿Freno o pausa?"}
    F -- Sí --> Z["No entra"]
    F -- No --> D["Lee Chainlink, Binance, PTB, mercado y libro"]
    D --> S{"Filtros 1-9: datos, momento, Δ TWAP, impulso, RSI, anti-fakeout"}
    S -- "no pasa" --> Z
    S -- pasa --> P{"Filtros 11-13: libro y precio de la ficha"}
    P -- "no pasa" --> Z
    P -- pasa --> R{"Segunda comprobación"}
    R -- cambia --> Z
    R -- igual --> O["Compra FOK de BET_SIZE USDC (PRUEBA: simulada)"]
```

## Características

**Panel de control (TUI).** Hecho con [Textual](https://textual.textualize.io/). Muestra la ventana de 5 minutos con una barra de progreso; cada moneda con su PTB, precio, Δ frente al PTB, compra/venta de UP y DOWN y la señal; la posición abierta con una barra SL ↔ TP; las últimas operaciones; el estado del sistema (bot, Chainlink, Binance, PTB, proxy, CLOB, Gamma y disco) y los avisos traducidos a lenguaje claro (saldo insuficiente, 425 por reinicio del motor, 429, orden sin liquidez, bloqueo geográfico…).

**Configuración de cuenta guiada.** Se abre sola en el primer arranque, o con la tecla `A`. Te pregunta **«¿Cómo entras en polymarket.com?»** y deduce el tipo de cartera:

| Respuesta | Tipo (`POLY_SIGNATURE_TYPE`) |
|---|---|
| MetaMask, Rabby, Coinbase Wallet u otra cartera del navegador | Safe (2) |
| Email o Google (Magic) | Magic (1) |
| Mi propia cartera, sin la web de Polymarket (avanzado) | EOA (0) |

Después genera o recupera las **claves CLOB** a partir de tu clave privada, permite añadir un **proxy**, ejecuta **comprobaciones previas** de solo lectura y ofrece los **permisos** (*allowances*) de forma explícita. Hay también un asistente de terminal: `python -m caro5m.setup`.

![Pantalla de configuración de cuenta con la cartera guardada, las claves CLOB enmascaradas, todas las comprobaciones en verde y el recuadro verde «Todo listo» con el botón Ir al panel (datos de demostración)](docs/tui_cuenta.png)

**Ajustes en caliente.** Casi todo se aplica sin reiniciar en unos segundos, incluidas las monedas. Lo poco que necesita reinicio (proxy, claves con el bot en REAL, host CLOB…) sale en una banda amarilla y se aplica con `R`, que conserva el modo.

**Gestión de procesos.** El panel arranca y para el bot (y el proceso PTB) en segundo plano. `B`, `R` y `Q` encuentran **cualquier** bot o PTB de esta carpeta (aunque sea de una versión anterior, lanzado a mano o duplicado) y nunca tocan procesos de otras carpetas. Al salir del panel se cierra todo, salvo que pongas `KEEP_RUNNING_ON_EXIT=1`.

**Logs copiables.** `L` muestra el log del bot en vivo, con filtros (todo, operaciones, errores) y copia al portapapeles. `E` muestra el registro de la configuración y los errores con su *traceback*, enmascarado.

**Multiplataforma.** Linux, macOS y Windows 10/11 (instalador de PowerShell). En Linux, opcionalmente, servicios systemd y logrotate.

## Requisitos

- **Python 3.10 o superior** con `venv`. Las dependencias fijadas se han verificado con Python 3.12 y 3.13.
- Linux, macOS o Windows 10/11. En Windows, mejor con **Windows Terminal**.
- Conexión a internet.
- **Para el modo PRUEBA no hace falta cuenta ni claves.**
- Para el modo REAL: una cuenta de Polymarket con USDC, su clave privada y, si usas Safe o Magic, la dirección de tu cuenta de Polymarket. Con EOA, también un poco de POL para el gas de las aprobaciones.

Dependencias principales: [`py_clob_client_v2`](https://pypi.org/project/py-clob-client-v2/) (cliente oficial del CLOB), `textual`, `httpx`, `requests`, `aiohttp`, `websockets` y, solo en Windows, `psutil`.

## Instalación

### Linux y macOS

```bash
git clone https://github.com/cardenasmiguel/CARO.git
cd CARO
./install.sh      # crea .venv, instala dependencias, prepara .env (0600) y config/settings.env
./start.sh        # abre el panel
```

El instalador se puede repetir sin perder tu `.env` ni tus ajustes. Si encuentra un bot o PTB de esta carpeta en marcha, lo para antes de actualizar (pregunta; por defecto sí). Al terminar te pregunta cómo configurar la cuenta (en el panel, que es lo recomendado; en la terminal; o más tarde) y si quieres abrir el panel ya. **Nunca activa el dinero real.**

| Opción | Qué hace |
|---|---|
| `--panel` | Al terminar abre el panel con la configuración guiada (por defecto) |
| `--cli` | Configura la cuenta en la terminal, con preguntas |
| `--no-wizard` | No configura la cuenta ahora (luego, tecla `A`) |
| `--non-interactive` | Sin preguntas: lee variables `CARO5M_SETUP_*` (ver `python -m caro5m.setup -h`) |
| `--systemd` | Instala los servicios `caro5m-bot` y `caro5m-ptb` (solo Linux, usa `sudo`) |
| `--start` | Con `--systemd`: los habilita y arranca (en modo PRUEBA salvo `DRY_RUN=0`) |
| `--logrotate` | Instala la rotación de logs (solo Linux, usa `sudo`) |
| `--no-lock` | Usa `requirements.txt` en vez de `requirements.lock.txt` |
| `--python RUTA` | Usa ese intérprete de Python |

En macOS no hay `/dev/shm`, así que la carpeta compartida entre el bot y el PTB es `data/shm`. No hay systemd ni logrotate.

### Windows

```powershell
git clone https://github.com/cardenasmiguel/CARO.git
cd CARO
powershell -ExecutionPolicy Bypass -File install.ps1
.\caro5m.cmd      # o doble clic en start.bat
```

- `-ExecutionPolicy Bypass` solo afecta a esa ejecución; no cambia la política del sistema.
- Opciones: `-Cli`, `-NoWizard`, `-NoLock`, `-NonInteractive`, `-NoOpen` y `-Python C:\ruta\python.exe`.
- Necesitas Python 3.10+ de [python.org](https://www.python.org/downloads/windows/) (marca «Add python.exe to PATH») o `winget install Python.Python.3.12`. El instalador usa el lanzador `py -3` si existe.
- Deja estos accesos: `caro5m.cmd` y `start.bat` (panel), `run-bot.cmd` (bot sin panel) y `run-ptb.cmd` (PTB).
- El `.env` se protege con `icacls` (solo tu usuario) en lugar de `chmod 600`. La carpeta compartida es `data\shm`.

> [!NOTE]
> La compatibilidad con Windows está cubierta por pruebas que simulan Windows y el instalador se ha validado con el analizador de PowerShell, pero **todavía no se ha probado en un equipo Windows real**. Si algo falla, abre una *issue* con la salida de `install.ps1` y `data\caro5m.log`.

## Primeros pasos

1. **Abre el panel** con `./start.sh` (Windows: `.\caro5m.cmd`). En el primer arranque se abre la configuración de cuenta. Puedes saltarla con `Esc` para probar en modo PRUEBA sin cuenta.
2. **Configura la cuenta** (tecla `A`): elige cómo entras en Polymarket, pega tu clave privada y pulsa «Guardar y continuar». Se generan las claves CLOB y se ejecutan las comprobaciones. Cuando todo está bien sale el recuadro verde **«✔ Todo listo»**.
3. **Arranca el bot** con `B`. Siempre arranca en modo **PRUEBA**. Si el proceso PTB no está en marcha, lo arranca también (`AUTO_START_PTB=1`).
4. **Obsérvalo** un rato: la columna «Señal», la posición simulada, las operaciones marcadas `·sim` y el log (`L`).
5. **Pasa a REAL** con `M` y confírmalo con `S` en el aviso rojo. El bot comprueba la clave, el saldo (al menos `BET_SIZE × 1,03`) y el permiso de USDC. Si falta algo, **se queda en PRUEBA** y el panel te dice el motivo.

![Aviso rojo de confirmación «Pasar a modo REAL», que advierte de que el bot enviará órdenes reales tras comprobar saldo y allowance (datos de demostración)](docs/tui_modo_real.png)

El modo REAL hay que confirmarlo **en cada arranque**. No se puede cambiar de modo con una posición abierta. Si quieres que arranque siempre en REAL (por ejemplo, como servicio), pon `DRY_RUN=0` en `config/settings.env` sabiendo lo que haces.

Sin panel: `./scripts/run-bot.sh` arranca el bot en esa terminal y `./scripts/run-ptb.sh` el PTB (Windows: `run-bot.cmd` y `run-ptb.cmd`).

## Teclas del panel

| Tecla | Acción |
|---|---|
| `B` | **Arrancar o parar el bot** (con confirmación). Al parar, encuentra cualquier bot de esta carpeta: SIGTERM, hasta 8 s de espera y, si no responde, SIGKILL. Un bot del servicio systemd no se toca: te indica `sudo systemctl stop caro5m-bot` |
| `M` | **Cambiar entre PRUEBA y REAL** (con confirmación). Si el bot está parado, primero ofrece arrancarlo. Te dice en pantalla si el bot aceptó o rechazó el cambio y por qué |
| `P` | **Pausar o reanudar.** En pausa no abre posiciones nuevas; las abiertas se siguen gestionando |
| `S` | **Vender ya** la posición abierta (con confirmación) |
| `R` | **Reiniciar el bot** para aplicar cambios que lo necesitan o si el bot en marcha es de otra versión. Conserva el modo; si estabas en REAL, vuelve a pedir la confirmación |
| `C` | **Ajustes.** `↑`/`↓` elegir, `Enter` editar, `←`/`→` subir o bajar el valor |
| `L` | **Log del bot** en vivo. `1` todo · `2` operaciones · `3` errores · `F` seguir/parar · `C` copiar |
| `A` / `W` | **Configuración de cuenta**: cartera, claves CLOB, proxy, comprobaciones y permisos. Dentro: `P` (o `F2`) proxy, `E` errores |
| `E` | **Errores y registro** de la configuración (copiable). `C` copiar · `T` cambiar de vista · `R` actualizar |
| `Q` / `Ctrl+C` | **Salir y cerrar todo**: para el bot y el PTB de esta carpeta. Si hay una posición abierta en REAL, pide confirmación. Con `KEEP_RUNNING_ON_EXIT=1` siguen en marcha |
| `Ctrl+L` | Redibujar la pantalla (también se hace solo al cambiar el tamaño de la terminal) |

Si el bot no actualiza su estado en 15 s, el panel lo muestra como **DETENIDO** (mientras lo arranca, **ARRANCANDO…**).

![Panel con la banda amarilla que avisa de que el bot en marcha es de otra versión y de un cambio de proxy pendiente de reinicio, con la tecla R en la barra inferior (datos de demostración)](docs/tui_version.png)

![Visor del log del bot en vivo, con líneas de escaneo, una compra simulada, un take-profit y una recarga de ajustes en caliente (datos de demostración)](docs/tui_log.png)

## Ajustes

Se editan en el panel con `C` y se guardan en `config/settings.env` (sin secretos). Prioridad: **variable de entorno > `config/settings.env` > valor por defecto**. Un valor no válido se ignora y se usa el de la capa anterior. El bot relee el fichero en cada ciclo y deja en el log `SETTINGS reloaded changed=… hot=… restart=…`.

![Pantalla de Ajustes con el take-profit seleccionado en 0,09, la marca ● de aplicación en caliente y el mensaje «Guardado y aplicado en caliente» (datos de demostración)](docs/tui_settings.png)

● = se aplica en caliente · ⟳ = hay que reiniciar el bot (`R`)

| Ajuste | Qué es | Defecto | Rango | Cuándo |
|---|---|---|---|---|
| `BET_SIZE` | Apuesta por operación (USDC) | `1.00` | 1–1000 | ● |
| `TP_OFFSET` | Take-profit sobre la entrada | `0.08` | 0,01–1,00 (el objetivo se limita a 0,99) | ● |
| `SL_OFFSET` | Stop-loss bajo la entrada | `0.20` | 0,01–0,99 (si queda por debajo de 0,01, no salta) | ● |
| `CB_LOSSES` | Pérdidas seguidas para el freno | `2` | 1–100 | ● |
| `CB_COOLDOWN` | Pausa del freno (s) | `3600` | 60–86400 | ● |
| `MIN_TOKEN` / `MAX_TOKEN` | Rango de precio de compra de la ficha | `0.45` / `0.70` | 0,01–0,98 / 0,02–0,99; mínimo < máximo | ● |
| `POSITION_TIMEOUT` | Tiempo máximo de una posición (s); al vencer intenta vender y cobrar | `360` | 30–3600 | ● |
| `AUTO_REDEEM` | Cobrar on-chain al resolver a favor (solo cartera EOA; en PRUEBA se simula) | `1` | 0/1 | ● |
| `COINS` | Monedas a operar (casillas en el panel) | `btc,eth` | btc, eth, xrp, sol, doge, bnb, hype, zec | ● |
| `COIN_SCALE` | Avanzado: umbrales X veces mayores por moneda, p. ej. `doge:2` | vacío | factor 0,2–10 | ● |
| `POLYGON_RPCS` | RPC de Polygon (lista, en rotación) | 3 RPC públicos | URLs http(s) | ● |
| `BINANCE_HOST` | Host REST de Binance | `https://api.binance.com` | URL (en EE. UU., `https://api.binance.us`) | ● |
| `GAMMA_HOST` | Host de la Gamma API (solo en el fichero) | `https://gamma-api.polymarket.com` | URL | ● |
| `CARO5M_TZ` | Zona horaria del reloj del panel | `Europe/Madrid` | zona IANA | ● |
| `CARO5M_USE_PTB` | Usar el PTB temprano si el proceso corre | `1` | 0/1 | ● |
| `AUTO_START_PTB` | El panel arranca y para el PTB junto con el bot | `1` | 0/1 | ● (panel) |
| `KEEP_RUNNING_ON_EXIT` | Dejar el bot en marcha al salir del panel | `0` | 0/1 | ● (panel) |
| `DRY_RUN` | Modo al arrancar: `1` PRUEBA, `0` REAL (solo en el fichero) | `1` | 0/1 | al arrancar |
| `POLY_SIGNATURE_TYPE` | Tipo de firma: 0 EOA · 1 Magic · 2 Safe (lo fija el asistente) | `2` | 0–2 | ⟳ en REAL (en PRUEBA se aplica solo) |
| `CARO5M_PROXY_SCAN` | Usar el proxy también para Gamma y el libro de órdenes | `0` | 0/1 | ⟳ |
| `CLOB_HOST` / `CHAIN_ID` | Host del CLOB y red (no los cambies) | Polymarket / `137` | — | ⟳ |

Cambios en el `.env` (desde la pantalla de cuenta): el **proxy** siempre necesita reinicio; la **cartera o las claves CLOB** se aplican solas en PRUEBA (al pasar a REAL) y necesitan reinicio con el bot en REAL. El modo **nunca** cambia al recargar los ajustes: `DRY_RUN` solo decide el modo del próximo arranque.

**Monedas.** Al quitar una moneda que tiene una posición abierta, el bot la sigue gestionando (TP, SL, tiempo máximo, venta manual) hasta cerrarla y luego la retira; mientras tanto, el panel la marca «solo gestión». El proceso PTB se reinicia solo para seguir las monedas nuevas.

![Selector de monedas con casillas para BTC, ETH, XRP, SOL, DOGE, BNB, HYPE y ZEC; HYPE y ZEC indican que usan el precio de Binance (datos de demostración)](docs/tui_monedas.png)

Rutas (todas relativas al proyecto y configurables por variable de entorno): `CARO5M_HOME`, `CARO5M_ENV_FILE`, `CARO5M_SETTINGS_FILE`, `CARO5M_DATA_DIR`, `CARO5M_SHM_DIR` (por defecto `/dev/shm` en Linux y `data/shm` en macOS y Windows), `CARO5M_LOG_FILE` y `PM_PTB_SHM`.

## Configuración de cuenta

Todo está en una sola pantalla del panel (`A`) o en el asistente de terminal (`python -m caro5m.setup`; `--check` solo comprobaciones, `--log` muestra el registro de errores).

![Primer arranque: la configuración guiada pregunta «¿Cómo entras en polymarket.com?» y explica paso a paso cómo obtener la clave privada y la dirección de la cuenta (datos de demostración)](docs/tui_cartera.png)

1. **Cartera.** Cada campo se valida por separado y un campo vacío significa «conservar lo guardado». Desde la clave muestra la dirección de tu cartera (enmascarada) para que la compruebes. Si dejas vacía la dirección de fondos o pulsas «Buscar mi dirección», la busca en el perfil público de Polymarket (solo envía tu dirección pública, nunca la clave). Si detecta una incoherencia, por ejemplo que la dirección de fondos sea la de la propia clave con MetaMask o email, **pregunta antes de guardar**.

   ![Aviso de incoherencia: la dirección de fondos es la misma que la de la clave, con tres opciones para elegir (datos de demostración)](docs/tui_conflicto.png)

2. **Claves CLOB.** Las genera o recupera con `create_or_derive_api_key` del cliente oficial y las guarda en `.env`. «Regenerar» usa un *nonce* nuevo y crea un juego nuevo. «Validar» hace una lectura autenticada. Si no las generas, el bot las deriva al pasar a REAL.
3. **Proxy opcional HTTP/SOCKS.** Para redes u operadores que bloquean el DNS o restringen el tráfico de salida. Acepta `http://usuario:clave@host:puerto`, `host:puerto:usuario:clave`, `usuario:clave@host:puerto`, `host:puerto` y los esquemas `https://`, `socks5://` y `socks5h://`. Si pegas varias líneas, usa la primera válida (sin preferencia por país). «Probar» muestra la latencia y lo que responde el endpoint público de geobloqueo de Polymarket. El proxy se usa para las órdenes y la autenticación del CLOB (y, con `CARO5M_PROXY_SCAN=1`, para Gamma y el libro); Chainlink, Binance y los RPC van siempre directos.

   ![Pantalla del proxy con un proxy de ejemplo en formato host:puerto:usuario:clave y la interpretación con las credenciales ocultas (datos de demostración)](docs/tui_proxy.png)

   > [!IMPORTANT]
   > Usar un proxy o una VPN para saltarte las restricciones geográficas de Polymarket puede incumplir sus condiciones de uso y la ley de tu país. caro5m no comprueba si puedes operar legalmente desde donde estás. Esa responsabilidad es tuya.

4. **Comprobaciones previas** (solo lectura): carpetas de datos y compartida, permisos del `.env`, cartera, DNS de `polymarket.com`, desfase del reloj, geobloqueo y CLOB, RPC de Polygon, Binance, validez de las claves CLOB, saldo de USDC frente a la apuesta, permiso (*allowance*) del exchange y, con EOA, POL para el gas.
5. **Permisos (*allowances*).** Nada se aprueba solo. Con **EOA**, el asistente puede enviar las aprobaciones on-chain (USDC `approve` y `setApprovalForAll` de los tokens hacia los contratos del exchange; como mucho 4 transacciones) tras una confirmación `S/N`. Con **Safe o Magic** no envía transacciones: Polymarket aprueba los permisos la primera vez que depositas u operas en su web. `--non-interactive` nunca aprueba.

Todo lo que hace la configuración queda en `data/logs/setup.log`, y los errores, con su *traceback*, en `data/logs/errors.log` (permisos 0600, rotan a 1 MB, enmascarados).

![Visor de errores y registro de la configuración con un traceback de ejemplo listo para copiar (datos de demostración)](docs/tui_errores.png)

## Arquitectura

Tres procesos independientes que se comunican por ficheros. El panel no envía órdenes.

```mermaid
flowchart LR
    subgraph Panel["Panel · caro5m (TUI)"]
        T["Pantalla principal"]
        AC["Pantalla de cuenta (A)"]
    end
    subgraph Bot["Bot · python -m caro5m.bot"]
        L["Bucle de decisión"]
        FW["Hilo de feeds"]
    end
    PTB["PTB · python -m caro5m.ptb (opcional, solo lectura)"]
    T -- "data/control/: pause, mode, sell_now" --> L
    L -- "data/status.json (cada ciclo)" --> T
    AC -- "lee y escribe" --> ENV[(".env · 0600")]
    ENV -- lee --> L
    T -- "escribe" --> SET[("config/settings.env")]
    SET -- "relee cada ciclo" --> L
    FW -- "chainlink_*.json, binance_*.json" --> SHM[("Carpeta compartida")]
    PTB -- "pm_ptb_latest.json" --> SHM
    SHM --> L
    FW --> RPC["RPC de Polygon (Chainlink)"]
    FW --> BN["Binance REST"]
    PTB --> RTDS["RTDS de Polymarket (WebSocket)"]
    L --> GM["Gamma API"]
    L --> CLOB["CLOB: libro y órdenes FOK"]
```

| Fichero o carpeta | Para qué |
|---|---|
| `caro5m/bot.py` | Bot: feeds, señal, órdenes, salidas, freno, estado para el panel |
| `caro5m/ptb.py` | Proceso PTB temprano (WebSocket RTDS de Polymarket) |
| `caro5m/tui/app.py`, `caro5m/tui/render.py` | Panel (pantallas, teclas) y dibujo de cada sección |
| `caro5m/config.py` | Ajustes: especificación, rangos, validación, rutas |
| `caro5m/coins.py` | Tabla de monedas: contrato Chainlink, símbolo de Binance y de RTDS |
| `caro5m/account.py`, `wizard.py`, `setup.py` | Cartera, claves CLOB, proxy, comprobaciones, permisos; asistente de terminal |
| `caro5m/checks.py` | Saldo y *allowance* de USDC por RPC (solo lectura) |
| `caro5m/launcher.py`, `procs.py`, `shutdown.py` | Arrancar, encontrar y parar los procesos de esta carpeta; cierre al salir |
| `caro5m/compat.py` | Diferencias entre Linux, macOS y Windows (procesos, señales, permisos, rutas) |
| `caro5m/status.py`, `messages.py`, `setuplog.py` | Canal de estado y control, avisos en español, registro enmascarado |
| `data/` | `caro5m.log`, `status.json`, `state_dry.json`, `state_live.json`, `control/`, `bot.pid`, `ptb.pid`, `ptb.log`, `ptb/` (auditoría), `logs/` |
| `systemd/`, `logrotate/` | Servicios y rotación de logs opcionales (Linux) |

Cada modo tiene su propio estado (`state_dry.json` y `state_live.json`): la simulación nunca toca la racha ni las posiciones reales. Las posiciones se guardan en disco en cada cambio, así que al reiniciar el bot se siguen gestionando.

## Seguridad

- **PRUEBA por defecto.** `DRY_RUN=1`. Cada arranque empieza en PRUEBA y el paso a REAL se confirma con `M` y `S`. El bot borra al arrancar cualquier petición de modo pendiente.
- **Comprobación antes de operar en real.** Al pasar a REAL, el bot comprueba clave, saldo (≥ `BET_SIZE × 1,03`) y permiso de USDC; si algo falla, sigue en PRUEBA.
- **Secretos solo en `.env`** con permisos 0600 (Windows: `icacls`), ignorado por Git. `config/settings.env` nunca guarda secretos.
- **El panel principal no lee claves** ni habla con la red de trading: solo lee `status.json` y escribe ficheros de control. Solo la pantalla de cuenta y el asistente de terminal leen y escriben el `.env`, y muestran los secretos **enmascarados**. El campo de la clave privada se vacía tras guardar.
- **Registros enmascarados**: claves, credenciales CLOB, usuario, contraseña e IP de los proxies, direcciones e IPs.
- **Sin aprobaciones automáticas**: con EOA, solo tras confirmación; con Safe o Magic, ninguna transacción.
- **Los servicios systemd no reciben secretos**: el bot lee el `.env` él mismo.
- **Sin órdenes por una conexión no prevista**: si hay un proxy configurado y no se puede aplicar, el bot se detiene en lugar de enviar las órdenes sin él.

Si encuentras un problema de seguridad, avisa por un canal privado antes de publicarlo (ver [Contribuir](#contribuir)).

## Solución de problemas

| Síntoma | Qué pasa y qué hacer |
|---|---|
| El panel dice **DETENIDO** | El bot no está corriendo o no actualiza `status.json` desde hace más de 15 s. Pulsa `B` para arrancarlo y mira `L`. Si el arranque falla, el panel muestra las últimas líneas del log |
| Banda amarilla **«El bot en marcha es la versión X»** | Actualizaste con un bot viejo en marcha. Pulsa `R` |
| **«No se activó el modo REAL: saldo insuficiente»** | Hace falta al menos `BET_SIZE × 1,03` USDC en la dirección de fondos. Añade fondos o baja la apuesta en `C` |
| **«Falta la aprobación (allowance)»** | Safe o Magic: haz un primer depósito u operación en polymarket.com. EOA: usa «Permisos» en la pantalla de cuenta |
| **«No address associated with hostname»** o **«Connection reset»** | Tu proveedor de internet no resuelve o corta `polymarket.com`. El asistente lo detecta y lo confirma con un DNS alternativo (DoH). Antes de buscar alternativas, comprueba que operar en Polymarket está permitido donde estás |
| **«Polymarket restringe el trading en algunos países (403)»** | Polymarket no acepta órdenes desde tu ubicación. Consulta su [página de restricciones geográficas](https://docs.polymarket.com/developers/CLOB/geoblock) y cumple sus condiciones de uso. caro5m no evita esa restricción |
| Binance da **451** | Binance no está disponible en tu región. En EE. UU., `BINANCE_HOST=https://api.binance.us` |
| **PTB: sin datos (modo legado)** | El proceso PTB no está en marcha o no tiene datos válidos. El bot funciona igual con el método de siempre. Arráncalo con `B` (si `AUTO_START_PTB=1`) o `./scripts/run-ptb.sh` |
| Se ve mal o con restos en pantalla | `Ctrl+L`. En Windows, usa Windows Terminal en lugar del `cmd.exe` clásico |
| `Ctrl+C` no copia en el log | Selecciona con el ratón y pulsa `C`. Usa el portapapeles del sistema si hay herramienta (`wl-copy`, `xclip`, `xsel`, `pbcopy`, `clip.exe`…) y además OSC52 |

## Preguntas frecuentes

**¿Necesito cuenta para probarlo?** No. En modo PRUEBA lee datos públicos y simula las operaciones.

**¿Gana dinero?** No hay ninguna garantía. Este proyecto no publica resultados ni promete rentabilidad. La estrategia puede perder, y las comisiones no se cuentan en el PnL del panel.

**¿Puedo dejarlo corriendo en un servidor?** Sí: `./install.sh --systemd --start` (Linux) o `KEEP_RUNNING_ON_EXIT=1` para que no se pare al cerrar el panel. Recuerda que el modo REAL hay que confirmarlo en cada arranque, salvo `DRY_RUN=0`.

**¿Qué pasa si cierro el panel con una posición abierta?** Por defecto se para el bot. La posición queda guardada y se sigue gestionando al volver a arrancarlo. Si estás en REAL, el panel te pide confirmación antes de salir.

**¿Reclama los premios de los mercados resueltos?** En una cartera EOA (clave propia), sí: con `AUTO_REDEEM=1` (por defecto) llama a `redeemPositions` en el CTF de Polymarket (hace falta un poco de POL para el gas). En modo PRUEBA solo se simula. En carteras Safe o email no se envía esa transacción: el panel mantiene la posición con el aviso «posición resuelta pendiente de cobrar en polymarket.com».

**¿Puedo usar otra estrategia?** El código está abierto: la lógica está en `evaluate_signal` y en el bucle de `caro5m/bot.py`. Si la cambias, pruébala primero en PRUEBA.

## Limitaciones conocidas

- El PnL del panel es `(salida − entrada) × tamaño`, **sin comisiones**. Una salida por tiempo (`TIMEOUT`) sí tiene PnL si se vendió o se cobró (a 1,00 si gana; a 0 si pierde).
- El cobro on-chain automático (`AUTO_REDEEM`) solo está implementado para carteras EOA. En Safe o email hay que cobrar en polymarket.com (el panel lo recuerda y no descarta la posición).
- El envío de órdenes reales y la lectura de saldo con fondos no se pueden cubrir en las pruebas automáticas sin arriesgar capital (usan un cliente simulado). Empieza con `BET_SIZE=1` y vigila las primeras operaciones.
- **Windows no se ha probado en un equipo real** (solo con pruebas que lo simulan).
- Las monedas distintas de BTC y ETH no tienen historial con esta estrategia. HYPE y ZEC usan Binance en vez de Chainlink.

## Desarrollo y pruebas

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

351 pruebas cubren los ajustes y la recarga en caliente (incluido el alta y la baja de monedas con posición abierta), los avisos, los ficheros de control, el asistente, el bucle del bot en modo PRUEBA con red simulada (entrada, TP, SL y freno, venta manual, pausa), el panel sin pantalla, la gestión de procesos y la compatibilidad simulada con Windows y macOS.

Capturas del README (datos inventados, sin red):

```bash
.venv/bin/python scripts/screenshot.py docs/tui.svg main   # vistas: main version modo settings coins log cartera cuenta conflicto proxy errores
.venv/bin/python scripts/svg2png.py docs/*.svg             # PNG a 2x con Chrome/Chromium headless
```

## Contribuir

Las *issues* y los *pull requests* son bienvenidos.

- Para un fallo, incluye la versión (`caro5m 1.2.6` sale en el log), tu sistema operativo y las líneas relevantes de `L` o `E`. Ya salen enmascaradas, pero revisa que no haya nada privado antes de pegarlas.
- **Nunca pegues tu `.env`, tu clave privada ni tus claves CLOB.**
- Antes de un PR, pasa `python -m pytest`. Si tocas la lógica de entrada o salida, explica el cambio y pruébalo en modo PRUEBA.
- Para un problema de seguridad, escribe en privado a **cardenasmiguel@gmail.com** o usa las [Security Advisories](https://github.com/cardenasmiguel/CARO/security/advisories/new) del repositorio. No abras una issue pública. Ver [SECURITY.md](SECURITY.md).
- Contacto general: **cardenasma@gmail.com**.

## Licencia

[MIT](LICENSE). Copyright © 2026 Miguel Cardenas.

caro5m no está afiliado con Polymarket, Chainlink ni Binance.

## Cambios

- **1.2.6**: al vencer el tiempo máximo se intenta vender al mejor bid (ya no se descarta en silencio). Cobro automático on-chain de posiciones ganadoras en carteras EOA (`AUTO_REDEEM=1`; en PRUEBA se simula); en Safe o email el panel avisa «posición resuelta pendiente de cobrar en polymarket.com». Proxy sin preferencia de país, textos neutrales (no se sugiere eludir el geobloqueo). Etiqueta de saldo según el tipo de cartera. El filtro de «spread» se llama `margen` (es `1 − (venta UP + venta DOWN)`). El nombre **CARO** = Circuit-Armored Risk Optimizer.
- **1.2.5**: `B`, `R`, `Q` y los instaladores paran cualquier bot o PTB de esta carpeta (versión vieja, lanzado a mano o duplicado; nunca de otra carpeta). Banda amarilla si el bot en marcha es de otra versión, con reinicio por `R`. Salir del panel (`Q`, `Ctrl+C`, cerrar la terminal o la consola de Windows) cierra el bot y el PTB, salvo `KEEP_RUNNING_ON_EXIT=1`. TP de 0,01 a 1,00 con salida limitada a 0,99; rangos más amplios en SL, apuesta, freno, rango de compra y tiempo máximo.
- **1.2.4**: compatibilidad con Windows (`install.ps1`, accesos `.cmd`, `caro5m/compat.py`). Ajustes en caliente, incluidas las monedas; lo que exige reinicio sale en una banda amarilla y se aplica con `R`, conservando el modo. Recuadro verde «Todo listo» al terminar la configuración, y `install.sh` ofrece abrir el panel (`./start.sh`).
- **1.2.3**: las 8 monedas de 5 minutos (`COINS`), con HYPE y ZEC en modo degradado y `COIN_SCALE`. El panel dice si el bot aceptó o rechazó el modo REAL y por qué. El PTB se arranca y para con el bot. Redibujado con `Ctrl+L` y al cambiar el tamaño.
- **1.2.2**: arrancar y parar el bot desde el panel (`B`, y `M` con el bot parado). Log del bot en vivo y copiable (`L`). El proxy acepta varias líneas y usa la primera válida.
- **1.2.1**: pantalla propia del proxy (formato `host:puerto:usuario:clave`). Las claves CLOB van por el proxy, con errores en español. Registro de errores copiable (`E`).
- **1.2.0**: configuración de cuenta guiada en el panel («¿Cómo entras en polymarket.com?»), búsqueda de la dirección en el perfil público, detección del bloqueo de DNS y reloj con fuentes de reserva.
- **1.1.0**: claves CLOB (generar, derivar, regenerar, validar), proxy HTTP/HTTPS/SOCKS5 con prueba, tipos de cartera EOA, Magic y Safe, comprobaciones previas, aprobaciones explícitas solo para EOA.
- **1.0.1**: el freno ya no deja sin gestionar las posiciones abiertas de otras monedas.
- **1.0.0**: primera versión publicable: panel, modo PRUEBA por defecto e instalador.
