# Polymarket Trading Bot

Automated trading bot for [Polymarket](https://polymarket.com). Scans prediction markets, identifies trading signals using Claude AI and whale wallet tracking, and places trades with full P&L accounting. Includes a web dashboard for monitoring performance.

Supports **paper trading** (simulated) and **live trading** (real money on Polymarket via the CLOB API).

## Requirements

- Python 3.9+
- An [Anthropic API key](https://console.anthropic.com/) (required for Haiku strategy)
- For live trading: an Ethereum wallet (EOA) with USDC on Polygon

## Installation

```bash
pip install -r requirements.txt
```

## Setup

Copy the template and fill in your values:

```bash
cp .env.example .env
```

At minimum, set your API key in `.env`:

```
ANTHROPIC_API_KEY="sk-ant-..."
```

All other values are optional — defaults from `.env.example` apply. The `.env` file is already in `.gitignore` so your key will never be committed.

## Running

**Start the bot** (runs continuously, scans every hour by default):

```bash
caffeinate -i python3 polymarket_bot.py
```

`caffeinate` is built into macOS and prevents your Mac from sleeping while the bot runs. When you stop the bot with `Ctrl+C`, sleep behaviour returns to normal.

**Start the dashboard** (open in a second terminal while the bot is running):

```bash
python3 dashboard.py
```

Then open **http://localhost:5050** in your browser.

**Access from your phone (or any device, any network):**

Install [ngrok](https://ngrok.com/) and expose the dashboard:

```bash
brew install ngrok
ngrok http 5050
```

ngrok gives you a public URL (e.g. `https://abc123.ngrok-free.app`) that you can open on your phone from anywhere.

The bot creates these local files on first run:
- `paper_trades.db` — SQLite database of all trades and scan history
- `budget.json` — tracks your current simulated balance (paper modes)
- `live_budget.json` — tracks your live trading balance (live mode)
- `bot.log` — full log of every scan and trade decision

---

## Dashboard

The web dashboard gives a live view of the bot's performance. It auto-refreshes every 60 seconds.

| Section | What it shows |
|---|---|
| Stat cards | Current budget (paper, shadow, live), total P&L, win rate |
| P&L Chart | Cumulative profit over time (tabs for Real, Live, Shadow) |
| Real Trades | Strategy breakdown — win rate and P&L per strategy tag |
| Live Trades | Live trade strategy breakdown (visible when live data exists) |
| Shadow Comparison | Hypothetical outcomes for all strategies (see Shadow Mode below) |
| Trade History | Full trade table, filterable by open/resolved/live/shadow |
| Scan Log | Recent scans with markets checked, signals found, trades placed |

Live trades are marked with **LIVE** or **DRY** badges in the trade history to distinguish them from paper trades.

---

## Strategies

Three strategies are available, controlled by `ENABLE_HAIKU` and `ENABLE_WHALE_COPY`:

| Tag | Description |
|---|---|
| `haiku-analyse` | Claude Haiku fetches recent news and evaluates each market for a mispricing edge |
| `whale-copy` | Mirrors recent large trades from active, historically profitable wallets |
| `whale+haiku` | Whale signal confirmed by Haiku before entry |

### Strategy Mode

Set `STRATEGY_MODE` in `.env` to control how strategies interact:

| Mode | Behaviour |
|---|---|
| `compete` | Strategies share up to 3 trade slots per scan — whale signals are prioritised **(default)** |
| `parallel` | Each strategy gets its own independent trade slot per scan — all three can place a trade |
| `shadow` | No real trades placed. All three strategies are evaluated and logged to `shadow_trades` — use this to compare strategies risk-free before committing budget |
| `live` | **Real money.** Places actual orders on Polymarket via the CLOB API. Behaves like `compete` but executes Fill-or-Kill market orders. Requires `POLYMARKET_PRIVATE_KEY` |

**Recommended flow:** run `shadow` mode for a few weeks to collect data, check the Shadow Comparison tab in the dashboard, then switch to `compete` or `parallel` using the strategy that performed best. When confident, graduate to `live` mode.

In `compete`, `parallel`, and `live` mode, the bot will never open a second position on a market where a trade is already open. Shadow mode is unrestricted — contradictory signals on the same market are kept as useful data.

### Whale Discovery

Whale signals come from wallets that are **both active (traded recently) and profitable (on the leaderboard)**. The bot:

1. Fetches the top 30 wallets from the Polymarket leaderboard (cached 1 hour)
2. Pulls recent large trades from the global activity feed (1–2 API calls)
3. Only acts on trades where the wallet appears in both lists

To track specific wallets instead, set `WHALE_WALLETS` in `.env`:

```
WHALE_WALLETS="0xabc123...,0xdef456..."
```

---

## Live Trading

Live mode places real orders on Polymarket using the [py-clob-client](https://github.com/Polymarket/py-clob-client) SDK. Orders are Fill-or-Kill market orders for immediate execution.

### Prerequisites

1. An Ethereum wallet (EOA) — e.g. MetaMask, hardware wallet, or a generated key
2. USDC on Polygon in that wallet
3. Token allowances approved for Polymarket's exchange contracts (USDC + Conditional Tokens — see [Polymarket docs](https://docs.polymarket.com/developers/CLOB/clients/methods-l2))

### Setup

Add to your `.env`:

```
STRATEGY_MODE="live"
POLYMARKET_PRIVATE_KEY="0xYourPrivateKeyHere"
LIVE_DRY_RUN="true"
```

Start in **dry-run mode** first. The bot will run all strategies and log what it _would_ trade without calling the CLOB API. Check the dashboard for "DRY" badges on trades.

When ready to go live:

```
LIVE_DRY_RUN="false"
```

### Safety Controls

| Setting | Default | Description |
|---|---|---|
| `LIVE_DRY_RUN` | `true` | Log orders without placing them. Must explicitly set to `false` for real trades |
| `MAX_LIVE_TRADE_USDC` | `50` | Hard cap per trade in USDC — no single trade exceeds this regardless of budget |
| `LIVE_MAX_OPEN_TRADES` | `5` | Maximum concurrent live positions |

Additional protections (always on):
- Pre-trade wallet balance check — aborts if wallet USDC is below trade amount
- Startup validation — verifies private key, CLOB connectivity, and wallet balance before entering the scan loop
- Failed orders never deduct from budget
- Prominent "LIVE MODE" warning in logs and dashboard

### Switching Back to Paper

Change `STRATEGY_MODE` back to `shadow`, `compete`, or `parallel` at any time. Your live trades will continue to resolve normally and are tracked separately with their own budget file (`live_budget.json`).

---

## Configuration

All settings live in `.env`. See `.env.example` for the full list with defaults.

### Budget

| Variable | Default | Description |
|---|---|---|
| `STARTING_BUDGET` | `500` | Starting paper balance in USDC |
| `TRADE_SIZE_PCT` | `0.03` | Fraction of budget staked per trade (3%) |
| `MIN_TRADE_USDC` | `5` | Minimum trade size in USDC |

### Market Filters

| Variable | Default | Description |
|---|---|---|
| `MIN_LIQUIDITY` | `2000` | Skip markets below this liquidity |
| `MAX_TRADE_PRICE` | `0.85` | Skip near-certain outcomes; enforces both sides into [0.15, 0.85] |
| `MAX_RESOLVE_HOURS` | `0` | 0 = no limit. Set e.g. 24 for same-day markets only |
| `MARKET_POOL_SIZE` | `100` | Fetch this many markets, then rank and pick the best |
| `MARKETS_PER_SCAN` | `20` | Number of top-ranked markets to evaluate per scan |

### Strategy Settings

| Variable | Default | Description |
|---|---|---|
| `ENABLE_HAIKU` | `true` | Enable Claude Haiku analysis |
| `ENABLE_WHALE_COPY` | `true` | Enable whale wallet copy trading |
| `HAIKU_MIN_CONF` | `0.65` | Minimum confidence score to act on a Haiku signal |
| `SHADOW_HAIKU_MIN_CONF` | `0.40` | Lower threshold for shadow data collection |
| `HAIKU_SKIP_SPORTS` | `true` | Skip live game matchups/spreads (Haiku has no real-time sports data) |
| `WHALE_MIN_SIZE` | `500` | Minimum USD size to consider a whale trade |
| `WHALE_WALLETS` | _(empty)_ | Comma-separated wallet addresses to track manually |

### Operational

| Variable | Default | Description |
|---|---|---|
| `MAX_OPEN_TRADES` | `10` | Maximum unresolved trades at once (paper modes) |
| `SCAN_INTERVAL` | `3600` | Seconds between scans (default: 1 hour) |
| `SHADOW_SCAN_INTERVAL` | `900` | Faster interval for shadow mode (15 min) |
| `STRATEGY_MODE` | `compete` | `compete`, `parallel`, `shadow`, or `live` |

### Live Trading

| Variable | Default | Description |
|---|---|---|
| `POLYMARKET_PRIVATE_KEY` | _(empty)_ | Ethereum private key for your EOA wallet on Polygon |
| `POLYMARKET_FUNDER_ADDRESS` | _(empty)_ | Optional — only needed for proxy wallets |
| `MAX_LIVE_TRADE_USDC` | `50` | Hard cap per trade in USDC |
| `LIVE_MAX_OPEN_TRADES` | `5` | Maximum concurrent live positions |
| `LIVE_DRY_RUN` | `true` | Log orders without placing them |

---

## API Costs

All Polymarket APIs (Gamma, CLOB, Data) are free with no key required.

The only cost is the Anthropic API for Claude Haiku analysis:

| Scan interval | Scans/day | Estimated cost |
|---|---|---|
| 1 hour (default) | 24 | ~$3/month |
| 6 hours | 4 | ~$0.50/month |

The Anthropic API is billed separately from any Claude Pro subscription.

---

## Fees

The bot estimates Polymarket's taker fee using their parabolic formula (peaks at ~1.8% when price = 0.50). Maker orders earn a 0.2% rebate. Haiku prefers maker orders where possible to reduce costs.

---

## Querying the Database

The SQLite database can be queried directly if you want data beyond what the dashboard shows:

```bash
# All open trades
sqlite3 paper_trades.db "SELECT market_name, direction, price, amount, mode FROM trades WHERE resolved=0;"

# Resolved trades sorted by P&L
sqlite3 paper_trades.db "SELECT market_name, direction, outcome, pnl, mode FROM trades WHERE resolved=1 ORDER BY pnl DESC;"

# Total profit (paper)
sqlite3 paper_trades.db "SELECT ROUND(SUM(pnl), 2) AS total_pnl FROM trades WHERE resolved=1 AND mode='paper';"

# Total profit (live)
sqlite3 paper_trades.db "SELECT ROUND(SUM(pnl), 2) AS total_pnl FROM trades WHERE resolved=1 AND mode='live';"

# Shadow strategy comparison
sqlite3 paper_trades.db "SELECT strategy, COUNT(*) AS trades, ROUND(SUM(pnl),2) AS pnl FROM shadow_trades WHERE resolved=1 GROUP BY strategy;"
```
