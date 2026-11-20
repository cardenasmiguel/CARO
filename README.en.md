<div align="center">

# caro5m

**CARO** = *Circuit-Armored Risk Optimizer* · **5m** = 5-minute markets.

**Open-source bot for Polymarket's 5-minute crypto "Up/Down" markets, with a terminal control panel.**

BTC · ETH · XRP · SOL · DOGE · BNB · HYPE · ZEC — DRY-RUN by default — Linux · macOS · Windows

![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)
![Version 1.2.6](https://img.shields.io/badge/version-1.2.6-blue)
![Platforms](https://img.shields.io/badge/platforms-Linux%20%7C%20macOS%20%7C%20Windows-lightgrey)
![TUI: Textual](https://img.shields.io/badge/TUI-Textual-5A4FCF)
![Tests: pytest](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

[Versión en español](README.md)

![caro5m main panel with six coins in the current window, an open BTC position, recent trades, system status and warnings (demo data)](docs/tui.png)

<sub>All screenshots use made-up demo data (`scripts/screenshot.py`), not real trades or balances. The panel UI is in Spanish.</sub>

</div>

> [!WARNING]
> **Experimental software that can trade real money.** You can lose everything you deposit. This is not financial advice, it makes no profit claims and comes with no warranty. It always starts in **DRY-RUN mode** (simulated fills, no orders sent). You alone are responsible for complying with your local law and Polymarket's [terms of use](https://polymarket.com/tos), including its [geographic restrictions](https://docs.polymarket.com/developers/CLOB/geoblock). If Polymarket is not allowed where you live, do not use this.

## Contents

- [What it does](#what-it-does)
- [How it decides](#how-it-decides)
- [Features](#features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Getting started](#getting-started)
- [Panel keys](#panel-keys)
- [Settings](#settings)
- [Account setup](#account-setup)
- [Architecture](#architecture)
- [Security](#security)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Known limitations](#known-limitations)
- [Development and tests](#development-and-tests)
- [Contributing](#contributing)
- [License](#license)

## What it does

Every 5 minutes Polymarket opens one market per coin like "Will BTC go up or down between 10:00 and 10:05?". Each market has two tokens, **UP** and **DOWN**, priced between 0.01 and 0.99 USDC. caro5m watches those markets and, when the coin has moved clearly (and with confirmation) away from the window's opening reference, **buys the favoured side** and exits on a take-profit, a stop-loss or a time limit.

- **Coins** (`COINS` setting, default `btc,eth`): BTC, ETH, XRP, SOL, DOGE, BNB, HYPE and ZEC. Pick them with checkboxes in the panel; changes apply without a restart.
- **At most one position per coin**, with a fixed stake per trade (`BET_SIZE`, default 1 USDC).
- **DRY-RUN by default**: buys and sells are simulated at real order-book prices; nothing is signed or sent. Live mode is switched on by hand, with a confirmation, on every start.
- **Terminal panel** to watch everything live and control it with single keys: start/stop the bot, pause, sell now, switch mode, edit settings, read the log and set up the account.

### Price sources per coin

Markets resolve on each coin's Chainlink price. The bot uses ([`caro5m/coins.py`](caro5m/coins.py)):

| Coin | Live price | Momentum & RSI | Early reference (PTB) |
|---|---|---|---|
| BTC, ETH, XRP, SOL, DOGE, BNB | Chainlink `<COIN>/USD` on Polygon, read over public RPC | Binance `<COIN>USDT` | Polymarket RTDS |
| HYPE `*` | Binance spot (there is no Chainlink HYPE/USD on Polygon) | Binance `HYPEUSDT` | Polymarket RTDS |
| ZEC `*` | Binance spot (Polygon's Chainlink ZEC/USD only updates on a 1 % move or every 24 h) | Binance `ZECUSDT` | Polymarket RTDS |

`*` = **degraded mode**, flagged with an asterisk in the panel. The strategy was tuned on BTC and ETH; other coins have no track record with it, so try them in DRY-RUN first.

## How it decides

Everything below comes from [`caro5m/bot.py`](caro5m/bot.py). Thresholds are **relative** (% moves, token prices between 0 and 1), so they work the same for expensive and cheap coins. `COIN_SCALE` (advanced) lets you require X-times larger moves for a given coin.

### 1. Inputs

| Input | Source | How often |
|---|---|---|
| **Chainlink price** and its **30 s TWAP** | Chainlink contract on Polygon (`latestRoundData`), rotating over `POLYGON_RPCS` | ~2 s |
| **Opening reference** ("price to beat", PTB) | By default the 30 s TWAP at the start of the window; the early PTB value if the PTB process is running | once per window |
| **1-minute momentum** (`bin1m`) and **RSI(14)** | Binance ticker and 1-minute klines | ~2 s |
| **Window market** | Polymarket Gamma API, slug `<coin>-updown-5m-<start>` | every loop |
| **Best bid/ask for UP and DOWN** | Polymarket CLOB order book | every loop |

The main loop runs every ~2.5 s plus request time.

**Early PTB (optional).** The `caro5m.ptb` process listens to Polymarket's public RTDS WebSocket (1 s Chainlink prices) and computes the reference as the mean of the points between 62 and 3 seconds before the window starts. The bot only uses it if its quality is "ok" or "degraded", it was emitted between 10 s before and 5 s after the start, its data is at most 8 s old, and it is within 0.5 % of the on-chain price; otherwise it falls back to the legacy method. Read-only, no keys.

### 2. Entry filters

For each coin, on each loop, the signal must pass **all** of these:

| # | Filter | Rule (values from the code) |
|---|---|---|
| 1 | Fresh data | Binance under 10 s old, Chainlink under 45 s |
| 2 | Timing | Between second 45 and 125 of the window, with at least 120 s left |
| 3 | Direction | Sign of **Δ TWAP**: % change of the 30 s TWAP vs the PTB |
| 4 | Move size | \|Δ TWAP\| ≤ 0.30 %. **Lane B**: \|Δ\| ≥ 0.10 %. **Lane A** (early): \|Δ\| between 0.06 % and 0.10 %, no later than second 90 |
| 5 | Consistency | Spot price and TWAP move the same way |
| 6 | Contrary momentum veto | Rejects if Binance 1 min goes against the direction by more than 0.03 % (and UP with `bin1m` < −0.02 %) |
| 7 | Binance confirmation | Lane A: same-sign `bin1m` ≥ 0.04 %. Lane B: same-sign ≥ 0.03 %, or flat (\|bin1m\| ≤ 0.01 %) with a strong (\|Δ\| ≥ 0.15 %) or early (second ≤ 60, \|Δ\| ≥ 0.11 %) move |
| 8 | RSI vetoes | UP with RSI > 70; DOWN with RSI < 10 or > 75; DOWN with `bin1m` > +0.01 % and RSI < 28 |
| 9 | Anti-fakeout | Using the last ~60 s of history: prior momentum whipsaw (≥ 0.08 % against within 30 s), non-persistent momentum (15 s average < 0.02 %), momentum fading while Δ grows, token ask falling ≥ 0.05 from its 20 s peak, lane A UP with RSI < 40, and exhausted lane B moves (\|Δ\| ≥ 0.18 % without momentum ≥ 0.08 %) |
| 10 | Pause and breaker | No entry while paused (`P`) or while the circuit breaker is active |
| 11 | Book margin | Rejects if `1 − (UP ask + DOWN ask)` > 0.08 (the book's gap: how far the two asks are from adding up to 1). Shown in the panel as `margen` |
| 12 | Token price | Ask of the chosen side within `MIN_TOKEN`–`MAX_TOKEN` (default 0.45–0.70). Lane A: also ≤ 0.60 |
| 13 | Expensive token | Lane B with ask > 0.62 requires \|Δ\| ≥ 0.10 % and same-sign momentum ≥ 0.03 % |
| 14 | Re-check | The signal is evaluated again right before sending; if it changed, no entry |

If everything passes, it sends a **FOK** (fill-or-kill) market BUY for `BET_SIZE` USDC at the seen ask. In DRY-RUN the fill is simulated at that price. The panel's "Señal" column shows the word for the filter that stopped each coin: `delta`, `momento`, `rsi`, `precio`, `margen` (book margin, not the bid–ask spread), `ventana`, `datos`, and so on.

### 3. Exits

With an open position, each loop reads the best bid of its token:

- **Take-profit**: bid ≥ `entry + TP_OFFSET` (default +0.08) → FOK sell. The target never exceeds 0.99, Polymarket's maximum price. Resets the loss streak.
- **Stop-loss**: bid ≤ `entry − SL_OFFSET` (default −0.20) and the position is at least 20 s old → FOK sell. Adds one loss to the streak.
- **Time limit**: after `POSITION_TIMEOUT` seconds (default 360, longer than the 5-minute window) the bot **tries a FOK sell at the best bid**. A fill is recorded as `TIMEOUT` with PnL. If it does not fill (the market has usually already resolved): a losing position is closed at 0; a winning one is redeemed on-chain for EOA wallets (`AUTO_REDEEM=1`; simulated in DRY-RUN) and recorded as `REDEEM`; Safe or email wallets keep the position with the panel hint «posición resuelta pendiente de cobrar en polymarket.com».
- **Sell now**: key `S` sells immediately with a FOK order.

**Circuit breaker**: after `CB_LOSSES` consecutive stop-losses (default 2), no new positions for `CB_COOLDOWN` seconds (default 3600). It is global across coins and **only blocks entries**; open positions keep being managed.

```mermaid
flowchart TD
    A["Every ~2.5 s, for each coin"] --> B{"Open position?"}
    B -- Yes --> X{"Token bid"}
    X -- "≥ entry + TP (max 0.99)" --> TP["FOK sell · TP · streak = 0"]
    X -- "≤ entry − SL and age ≥ 20 s" --> SL["FOK sell · SL · streak +1"]
    SL --> CB{"Streak ≥ CB_LOSSES?"}
    CB -- Yes --> CD["Breaker: no new entries for CB_COOLDOWN"]
    X -- "age > POSITION_TIMEOUT" --> TO["FOK sell at bid; else EOA redeem or panel hint"]
    X -- "otherwise" --> H["Hold"]
    B -- No --> F{"Breaker or paused?"}
    F -- Yes --> Z["No entry"]
    F -- No --> D["Read Chainlink, Binance, PTB, market and book"]
    D --> S{"Filters 1-9: data, timing, Δ TWAP, momentum, RSI, anti-fakeout"}
    S -- fail --> Z
    S -- pass --> P{"Filters 11-13: book and token price"}
    P -- fail --> Z
    P -- pass --> R{"Re-check"}
    R -- changed --> Z
    R -- same --> O["FOK buy of BET_SIZE USDC (DRY-RUN: simulated)"]
```

## Features

- **Control panel (TUI)** built with [Textual](https://textual.textualize.io/): the 5-minute window with a progress bar; every coin with PTB, price, Δ vs PTB, UP/DOWN bid/ask and signal; the open position with an SL ↔ TP bar; recent trades; system health (bot, Chainlink, Binance, PTB, proxy, CLOB, Gamma, disk); and plain-language warnings.
- **Guided account setup** (opens on first run, or key `A`): asks *how you sign in to polymarket.com* and derives the wallet type — browser wallet → Safe (2), email/Google → Magic (1), your own wallet without the website → EOA (0). It generates/derives **CLOB API keys** from your private key, lets you add a **proxy**, runs read-only **preflight checks** and offers explicit, opt-in **approvals**. Terminal wizard: `python -m caro5m.setup`.
- **Hot reload**: almost every setting, including the coin list, applies within seconds without a restart. The few that need one (proxy, keys while live, CLOB host…) show a yellow banner and are applied with `R`, which keeps the current mode.
- **Process management**: the panel starts and stops the bot (and the PTB process) in the background. `B`, `R` and `Q` find **any** bot/PTB process of this folder (old version, started by hand or duplicated) and never touch other folders. Quitting the panel shuts everything down unless `KEEP_RUNNING_ON_EXIT=1`.
- **Copyable logs**: `L` shows the live bot log with filters and clipboard copy; `E` shows the masked setup log and error tracebacks.
- **Cross-platform**: Linux, macOS and Windows 10/11 (PowerShell installer); optional systemd services and logrotate on Linux.

![Account setup screen with the saved wallet, masked CLOB keys, all checks green and the green "Todo listo" (all set) box (demo data)](docs/tui_cuenta.png)

## Requirements

- **Python 3.10+** with `venv` (pinned dependencies verified on 3.12 and 3.13).
- Linux, macOS or Windows 10/11 (Windows Terminal recommended).
- Internet access. **No account or keys are needed for DRY-RUN.**
- For live trading: a Polymarket account with USDC, its private key and — for Safe or Magic — your Polymarket account address. EOA wallets also need a little POL for approval gas.

## Installation

### Linux and macOS

```bash
git clone https://github.com/cardenasmiguel/CARO.git
cd CARO
./install.sh      # .venv, pinned deps, .env (0600) and config/settings.env
./start.sh        # opens the panel
```

The installer is idempotent, stops a running bot/PTB of this folder before updating (asks; default yes), offers to set up the account (in the panel, recommended) and to open the panel. **It never enables live trading.** Options: `--panel` (default), `--cli`, `--no-wizard`, `--non-interactive` (reads `CARO5M_SETUP_*`), `--systemd`, `--start`, `--logrotate` (Linux, need `sudo`), `--no-lock`, `--python PATH`. On macOS the bot↔PTB shared folder is `data/shm`.

### Windows

```powershell
git clone https://github.com/cardenasmiguel/CARO.git
cd CARO
powershell -ExecutionPolicy Bypass -File install.ps1
.\caro5m.cmd      # or double-click start.bat
```

Options: `-Cli`, `-NoWizard`, `-NoLock`, `-NonInteractive`, `-NoOpen`, `-Python C:\path\python.exe`. Shortcuts: `caro5m.cmd`/`start.bat` (panel), `run-bot.cmd`, `run-ptb.cmd`. `.env` is restricted with `icacls`. No systemd/logrotate.

> [!NOTE]
> Windows support is covered by tests that simulate Windows and the installer was checked with the PowerShell parser, but it **has not yet been run on a real Windows machine**. Please open an issue with the `install.ps1` output and `data\caro5m.log` if something breaks.

## Getting started

1. **Open the panel**: `./start.sh` (Windows: `.\caro5m.cmd`). The account setup opens on first run; press `Esc` to skip it and try DRY-RUN without an account.
2. **Set up the account** (`A`): choose how you sign in, paste your private key, press "Guardar y continuar". CLOB keys are generated and checks run; a green **"✔ Todo listo"** box appears when everything passes.
3. **Start the bot** with `B` — always in **DRY-RUN**. It also starts the PTB process (`AUTO_START_PTB=1`).
4. **Watch it**: the signal column, the simulated position, trades tagged `·sim` and the log (`L`).
5. **Go live** with `M` and confirm with `S` in the red dialog. The bot checks the key, the balance (≥ `BET_SIZE × 1.03`) and the USDC allowance; if anything is missing it **stays in DRY-RUN** and the panel tells you why.

![Red "Pasar a modo REAL" confirmation dialog warning that the bot will send real orders after checking balance and allowance (demo data)](docs/tui_modo_real.png)

Live mode must be confirmed **on every start** unless you deliberately set `DRY_RUN=0`. Mode can't be switched with an open position.

## Panel keys

| Key | Action |
|---|---|
| `B` | Start/stop the bot (confirmation). Stops any bot of this folder: SIGTERM, up to 8 s, then SIGKILL. A systemd service is never stopped (you get the `systemctl` command) |
| `M` | Switch DRY-RUN ↔ LIVE (confirmation). Offers to start the bot if stopped; reports whether the bot accepted or refused and why |
| `P` | Pause/resume new entries (open positions keep being managed) |
| `S` | Sell the open position now (confirmation) |
| `R` | Restart the bot to apply restart-only changes or a version mismatch; keeps the mode (asks again for LIVE) |
| `C` | Settings: `↑`/`↓` select, `Enter` edit, `←`/`→` nudge |
| `L` | Live bot log: `1` all · `2` trades · `3` errors · `F` follow · `C` copy |
| `A` / `W` | Account setup (inside: `P`/`F2` proxy, `E` errors) |
| `E` | Setup errors and log viewer: `C` copy · `T` toggle · `R` reload |
| `Q` / `Ctrl+C` | Quit and stop the bot and PTB of this folder (asks if there is a live open position) |
| `Ctrl+L` | Full redraw |

The panel shows **DETENIDO** (stopped) when `status.json` hasn't been updated for 15 s.

![Main panel with the yellow banner warning that the running bot is a different version and a proxy change is pending a restart (demo data)](docs/tui_version.png)

![Live bot log viewer with scan lines, a simulated buy, a take-profit and a hot settings reload (demo data)](docs/tui_log.png)

## Settings

Edited with `C` and saved to `config/settings.env` (no secrets). Priority: **environment variable > `config/settings.env` > default**; invalid values fall back to the next layer. The bot re-reads the file every loop and logs `SETTINGS reloaded changed=… hot=… restart=…`.

![Settings screen with take-profit selected at 0.09, the ● hot-reload mark and the "saved and applied live" message (demo data)](docs/tui_settings.png)

● = applied live · ⟳ = needs a bot restart (`R`)

| Setting | Meaning | Default | Range | When |
|---|---|---|---|---|
| `BET_SIZE` | Stake per trade (USDC) | `1.00` | 1–1000 | ● |
| `TP_OFFSET` | Take-profit above entry | `0.08` | 0.01–1.00 (target capped at 0.99) | ● |
| `SL_OFFSET` | Stop-loss below entry | `0.20` | 0.01–0.99 (never triggers if below 0.01) | ● |
| `CB_LOSSES` | Consecutive losses for the breaker | `2` | 1–100 | ● |
| `CB_COOLDOWN` | Breaker pause (s) | `3600` | 60–86400 | ● |
| `MIN_TOKEN` / `MAX_TOKEN` | Buy price range of the token | `0.45` / `0.70` | 0.01–0.98 / 0.02–0.99; min < max | ● |
| `POSITION_TIMEOUT` | Max position time (s); then tries to sell and redeem | `360` | 30–3600 | ● |
| `AUTO_REDEEM` | On-chain redeem when a resolved position wins (EOA only; simulated in DRY-RUN) | `1` | 0/1 | ● |
| `COINS` | Coins to trade | `btc,eth` | btc, eth, xrp, sol, doge, bnb, hype, zec | ● |
| `COIN_SCALE` | Advanced per-coin threshold multiplier, e.g. `doge:2` | empty | 0.2–10 | ● |
| `POLYGON_RPCS` | Polygon RPCs (rotated) | 3 public RPCs | http(s) URLs | ● |
| `BINANCE_HOST` | Binance REST host | `https://api.binance.com` | URL (US: `https://api.binance.us`) | ● |
| `GAMMA_HOST` | Gamma API host (file only) | `https://gamma-api.polymarket.com` | URL | ● |
| `CARO5M_TZ` | Panel clock time zone | `Europe/Madrid` | IANA zone | ● |
| `CARO5M_USE_PTB` | Use the early PTB if running | `1` | 0/1 | ● |
| `AUTO_START_PTB` | Panel starts/stops the PTB with the bot | `1` | 0/1 | ● (panel) |
| `KEEP_RUNNING_ON_EXIT` | Keep the bot running after quitting the panel | `0` | 0/1 | ● (panel) |
| `DRY_RUN` | Start mode: `1` DRY-RUN, `0` LIVE (file only) | `1` | 0/1 | at start |
| `POLY_SIGNATURE_TYPE` | 0 EOA · 1 Magic · 2 Safe (set by the wizard) | `2` | 0–2 | ⟳ when live (auto in DRY-RUN) |
| `CARO5M_PROXY_SCAN` | Also route Gamma/book reads through the proxy | `0` | 0/1 | ⟳ |
| `CLOB_HOST` / `CHAIN_ID` | CLOB host and chain (don't change) | Polymarket / `137` | — | ⟳ |

`.env` changes: the **proxy** always needs a restart; **wallet/CLOB keys** apply automatically in DRY-RUN and need a restart while live. Reloading never changes the mode. Removing a coin that has an open position keeps managing it until it closes ("solo gestión"). The PTB process restarts itself to follow coin changes.

![Coin selector with checkboxes for the eight coins; HYPE and ZEC note that they use the Binance price (demo data)](docs/tui_monedas.png)

## Account setup

All in one panel screen (`A`) or the terminal wizard (`python -m caro5m.setup`, `--check` for checks only, `--log` for the error log).

![First run: the guided setup asks how you sign in to polymarket.com and explains where to find the private key and account address (demo data)](docs/tui_cartera.png)

1. **Wallet**: each field is validated separately; empty means "keep what's saved". It shows the (masked) address derived from your key. It can look up your account address on Polymarket's public profile (only your public address is sent) and **asks before saving** if something looks inconsistent.
2. **CLOB keys**: generated/recovered with the official client's `create_or_derive_api_key`; "Regenerar" uses a new nonce; "Validar" makes an authenticated read.
3. **Optional HTTP/SOCKS proxy**: for networks or ISPs that block DNS or restrict outbound traffic. Accepts `http://user:pass@host:port`, `host:port:user:pass`, `user:pass@host:port`, `host:port`, and `https://`, `socks5://`, `socks5h://`. If you paste several, the first valid one is used (no country preference). "Probar" shows latency and the answer of Polymarket's public geoblock endpoint. It is used for CLOB orders and authentication (and, with `CARO5M_PROXY_SCAN=1`, Gamma/book); Chainlink, Binance and RPCs always go direct.

   ![Proxy screen with an example proxy in host:port:user:pass form, credentials hidden (demo data)](docs/tui_proxy.png)

   > [!IMPORTANT]
   > Using a proxy or VPN to get around Polymarket's geographic restrictions may breach its terms and your local law. caro5m does not check whether you may legally trade from where you are; that is your responsibility.

4. **Preflight checks** (read-only): data/shared folders, `.env` permissions, wallet, DNS, clock skew, geoblock/CLOB, Polygon RPCs, Binance, CLOB keys, USDC balance vs stake, exchange allowance and, for EOA, POL for gas.
5. **Approvals**: nothing is approved automatically. EOA: the wizard can send the on-chain approvals (at most 4 transactions) after an explicit `S/N`. Safe/Magic: no transactions are sent; Polymarket sets them up on your first deposit/trade on its website.

Setup activity goes to `data/logs/setup.log` and errors with full tracebacks to `data/logs/errors.log` (0600, rotated at 1 MB, masked).

![Setup errors and log viewer with an example traceback ready to copy (demo data)](docs/tui_errores.png)

## Architecture

Three independent processes that talk through files. The panel never sends orders.

```mermaid
flowchart LR
    subgraph Panel["Panel · caro5m (TUI)"]
        T["Main screen"]
        AC["Account screen (A)"]
    end
    subgraph Bot["Bot · python -m caro5m.bot"]
        L["Decision loop"]
        FW["Feed thread"]
    end
    PTB["PTB · python -m caro5m.ptb (optional, read-only)"]
    T -- "data/control/: pause, mode, sell_now" --> L
    L -- "data/status.json (every loop)" --> T
    AC -- "reads/writes" --> ENV[(".env · 0600")]
    ENV -- reads --> L
    T -- writes --> SET[("config/settings.env")]
    SET -- "re-read every loop" --> L
    FW -- "chainlink_*.json, binance_*.json" --> SHM[("Shared folder")]
    PTB -- "pm_ptb_latest.json" --> SHM
    SHM --> L
    FW --> RPC["Polygon RPC (Chainlink)"]
    FW --> BN["Binance REST"]
    PTB --> RTDS["Polymarket RTDS (WebSocket)"]
    L --> GM["Gamma API"]
    L --> CLOB["CLOB: book and FOK orders"]
```

Key files: `caro5m/bot.py` (bot), `caro5m/ptb.py` (early PTB), `caro5m/tui/` (panel), `caro5m/config.py` (settings spec), `caro5m/coins.py` (coin table), `caro5m/account.py` · `wizard.py` · `setup.py` (account), `caro5m/checks.py` (balance/allowance), `caro5m/launcher.py` · `procs.py` · `shutdown.py` (processes), `caro5m/compat.py` (OS differences), `caro5m/status.py` · `messages.py` · `setuplog.py`. Runtime data lives in `data/` (`caro5m.log`, `status.json`, `state_dry.json`, `state_live.json`, `control/`, pid files, `ptb/`, `logs/`). DRY-RUN and LIVE keep separate state files, so simulations never touch the live loss streak or positions.

## Security

- **DRY-RUN by default**; every start begins in DRY-RUN and going live needs `M` + `S`. Pending mode requests are cleared on start.
- **Pre-live check** of key, balance (≥ `BET_SIZE × 1.03`) and allowance; on failure the bot stays in DRY-RUN.
- **Secrets only in `.env`** (0600; `icacls` on Windows), git-ignored. `config/settings.env` never holds secrets.
- **The main panel never reads keys** or talks to the trading API: it only reads `status.json` and writes control files. Only the account screen and the terminal wizard read/write `.env`, always showing secrets **masked**.
- **Masked logs** (keys, CLOB credentials, proxy user/password/IP, addresses, IPs).
- **No automatic approvals**; systemd services receive no secrets.
- **No silent fallback**: if a configured proxy cannot be applied, the bot stops instead of sending orders without it.

## Troubleshooting

| Symptom | What to do |
|---|---|
| Panel shows **DETENIDO** | The bot isn't running or hasn't updated `status.json` for 15 s. Press `B` and check `L` |
| Yellow **version** banner | You updated with an old bot running. Press `R` |
| **"saldo insuficiente"** when going live | You need ≥ `BET_SIZE × 1.03` USDC. Add funds or lower the stake |
| **Allowance missing** | Safe/Magic: deposit or trade once on polymarket.com. EOA: "Permisos" in the account screen |
| **"No address associated with hostname"** / **"Connection reset"** | Your ISP doesn't resolve or resets `polymarket.com`; the wizard detects it (confirmed via DoH). First check that using Polymarket is allowed where you are |
| **"Polymarket restringe el trading en algunos países (403)"** | Polymarket doesn't accept orders from your location. See its [geographic restrictions](https://docs.polymarket.com/developers/CLOB/geoblock) and comply with its terms. caro5m does not bypass that restriction |
| Binance **451** | Binance isn't available in your region; in the US use `BINANCE_HOST=https://api.binance.us` |
| **PTB: sin datos (modo legado)** | The PTB process isn't running; the bot works anyway. Start it with `B` or `./scripts/run-ptb.sh` |
| Garbled screen | `Ctrl+L`; on Windows use Windows Terminal |

## FAQ

**Do I need an account to try it?** No. DRY-RUN only reads public data and simulates trades.

**Does it make money?** There is no guarantee. This project publishes no results and promises no returns; fees are not included in the panel's PnL.

**Does it claim resolved positions?** For an EOA wallet (your own key), yes: with `AUTO_REDEEM=1` (the default) it calls `redeemPositions` on Polymarket's CTF (a little POL is needed for gas). In DRY-RUN this is only simulated. For Safe or email wallets no transaction is sent: the panel keeps the position with the hint «posición resuelta pendiente de cobrar en polymarket.com».

**What happens if I close the panel with an open position?** By default the bot stops; the position is saved and managed again on the next start. In LIVE the panel asks for confirmation first.

## Known limitations

- PnL is `(exit − entry) × size`, **without fees**. A time-limit exit (`TIMEOUT`) has PnL if it was sold or redeemed (1.00 if it won; 0 if it lost).
- Automatic on-chain redeem (`AUTO_REDEEM`) is implemented for EOA wallets only. Safe or email wallets must be claimed on polymarket.com (the panel keeps the position and shows a reminder).
- Real order placement and funded balance checks can't be covered by automated tests without risking capital (tests use a mocked client). Start with `BET_SIZE=1` and watch the first trades.
- **Windows hasn't been run on a real machine** yet.
- Coins other than BTC/ETH have no track record with this strategy; HYPE and ZEC use Binance instead of Chainlink.

## Development and tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest      # 351 tests: settings & hot reload, bot loop in DRY-RUN with mocked network, TUI headless, processes, simulated Windows/macOS
.venv/bin/python scripts/screenshot.py docs/tui.svg main   # README screenshots from made-up data
.venv/bin/python scripts/svg2png.py docs/*.svg
```

## Contributing

Issues and pull requests are welcome. Include the version (in the log), your OS and the relevant `L`/`E` lines (already masked, but double-check). **Never paste your `.env`, private key or CLOB keys.** Run `python -m pytest` before a PR and test strategy changes in DRY-RUN. Report security issues privately to **cardenasmiguel@gmail.com** or via [GitHub Security Advisories](https://github.com/cardenasmiguel/CARO/security/advisories/new); do not open a public issue. See [SECURITY.md](SECURITY.md). General contact: **cardenasma@gmail.com**.

## License

[MIT](LICENSE). Copyright © 2026 Miguel Cardenas.

caro5m is not affiliated with Polymarket, Chainlink or Binance. Changelog: see the [Spanish README](README.md#cambios).
