# Polymarket Paper Trading Bot

Automated paper trading bot for [Polymarket](https://polymarket.com). Scans prediction markets, identifies trading signals using Claude AI and whale wallet tracking, and logs simulated trades with full P&L accounting. Includes a web dashboard for monitoring performance.

**No real money moves — paper trading only.**

## Requirements

- Python 3.9+
- An [Anthropic API key](https://console.anthropic.com/) (required for Haiku strategy)

## Installation

```bash
python3 -m pip install anthropic requests python-dotenv flask
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

Then open **http://localhost:5000** in your browser.

The bot creates these local files on first run:
- `paper_trades.db` — SQLite database of all trades and scan history
- `budget.json` — tracks your current simulated balance
- `bot.log` — full log of every scan and trade decision

---

## Dashboard

The web dashboard gives a live view of the bot's performance. It auto-refreshes every 60 seconds.

| Section | What it shows |
|---|---|
| Stat cards | Current budget, total P&L, win rate, open positions |
| P&L Chart | Cumulative profit over time |
| Real Trades | Strategy breakdown — win rate and P&L per strategy tag |
| Shadow Comparison | Hypothetical outcomes for all strategies (see Shadow Mode below) |
| Trade History | Full trade table, filterable by open/resolved |
| Scan Log | Recent scans with markets checked, signals found, trades placed |

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

**Recommended flow:** run `shadow` mode for a few weeks to collect data, check the Shadow Comparison tab in the dashboard, then switch to `compete` or `parallel` using the strategy that performed best.

In `compete` and `parallel` mode, the bot will never open a second position on a market where a trade is already open. Shadow mode is unrestricted — contradictory signals on the same market are kept as useful data.

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
| `PRICE_MIN` | `0.10` | Skip markets where YES price is below this |
| `PRICE_MAX` | `0.90` | Skip markets where YES price is above this |
| `MARKETS_PER_SCAN` | `30` | Number of top-volume markets to evaluate per scan |

### Strategy Settings

| Variable | Default | Description |
|---|---|---|
| `ENABLE_HAIKU` | `true` | Enable Claude Haiku analysis |
| `ENABLE_WHALE_COPY` | `true` | Enable whale wallet copy trading |
| `HAIKU_MIN_CONF` | `0.65` | Minimum confidence score to act on a Haiku signal |
| `WHALE_MIN_SIZE` | `500` | Minimum USD size to consider a whale trade |
| `WHALE_LOOKBACK_MIN` | `30` | How many minutes back to look for whale trades |
| `WHALE_WALLETS` | _(empty)_ | Comma-separated wallet addresses to track manually |

### Operational

| Variable | Default | Description |
|---|---|---|
| `MAX_OPEN_TRADES` | `10` | Maximum unresolved trades at once |
| `SCAN_INTERVAL` | `3600` | Seconds between scans (default: 1 hour) |
| `STRATEGY_MODE` | `compete` | `compete`, `parallel`, or `shadow` |

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
sqlite3 paper_trades.db "SELECT market_name, direction, price, amount FROM trades WHERE resolved=0;"

# Resolved trades sorted by P&L
sqlite3 paper_trades.db "SELECT market_name, direction, outcome, pnl FROM trades WHERE resolved=1 ORDER BY pnl DESC;"

# Total profit
sqlite3 paper_trades.db "SELECT ROUND(SUM(pnl), 2) AS total_pnl FROM trades WHERE resolved=1;"

# Shadow strategy comparison
sqlite3 paper_trades.db "SELECT strategy, COUNT(*) AS trades, ROUND(SUM(pnl),2) AS pnl FROM shadow_trades WHERE resolved=1 GROUP BY strategy;"
```
