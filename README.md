# Polymarket Paper Trading Bot

Automated paper trading bot for [Polymarket](https://polymarket.com). Scans prediction markets, identifies trading signals using Claude AI and whale wallet tracking, and logs simulated trades with full P&L accounting. No real money moves.

## Requirements

- Python 3.10+
- An [Anthropic API key](https://console.anthropic.com/) (only needed if using the Haiku strategy)

## Installation

```bash
pip install anthropic requests
```

## Setup

Set your Anthropic API key as an environment variable:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```

Then run:

```bash
python3 polymarket_bot.py
```

The bot creates two local files on first run:
- `paper_trades.db` — SQLite database of all trades
- `budget.json` — tracks your current simulated balance

## Strategies

The bot supports three modes, controlled by environment variables:

| Strategy | Description |
|---|---|
| `haiku-analyse` | Claude Haiku evaluates each market for a mispricing edge |
| `whale-copy` | Mirrors recent large trades from tracked whale wallets |
| `whale+haiku` | Whale signal confirmed by Haiku before entry |

Both strategies are enabled by default. When both are on, whale signals are confirmed by Haiku before a trade is placed. If Haiku rejects the signal, the trade is skipped.

## Configuration

All settings can be overridden with environment variables. Defaults are shown below.

### Budget

| Variable | Default | Description |
|---|---|---|
| `STARTING_BUDGET` | `500` | Starting paper balance in USDC |
| `TRADE_SIZE_PCT` | `0.03` | Fraction of budget staked per trade (3%) |
| `MIN_TRADE_USDC` | `5` | Minimum trade size in USDC |

### Market Filters

| Variable | Default | Description |
|---|---|---|
| `MIN_LIQUIDITY` | `2000` | Skip markets with less than this much liquidity |
| `PRICE_MIN` | `0.10` | Skip markets where YES price is below this |
| `PRICE_MAX` | `0.90` | Skip markets where YES price is above this |
| `MARKETS_PER_SCAN` | `30` | Number of top-volume markets to evaluate per scan |

### Strategy Settings

| Variable | Default | Description |
|---|---|---|
| `ENABLE_HAIKU` | `true` | Enable Claude Haiku analysis |
| `ENABLE_WHALE_COPY` | `true` | Enable whale wallet copy trading |
| `HAIKU_MIN_CONF` | `0.65` | Minimum confidence score to act on a Haiku signal |
| `WHALE_MIN_SIZE` | `500` | Minimum USD trade size to consider a whale signal |
| `WHALE_LOOKBACK_MIN` | `30` | How many minutes back to look for whale trades |
| `WHALE_WALLETS` | _(empty)_ | Comma-separated wallet addresses to track (see below) |

### Operational

| Variable | Default | Description |
|---|---|---|
| `MAX_OPEN_TRADES` | `10` | Maximum number of unresolved trades at once |
| `SCAN_INTERVAL` | `3600` | Seconds between scans (default: 1 hour) |

### Example: custom configuration

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export STARTING_BUDGET="1000"
export TRADE_SIZE_PCT="0.05"
export SCAN_INTERVAL="600"
export ENABLE_WHALE_COPY="false"
python3 polymarket_bot.py
```

## Whale Wallets

To track specific wallets, pass a comma-separated list:

```bash
export WHALE_WALLETS="0xabc123...,0xdef456..."
```

If no wallets are configured, the bot automatically fetches the top 10 wallets from the Polymarket leaderboard each scan.

## Output

The bot logs to both the terminal and `bot.log`. A summary is printed every ~12 scans (roughly every hour):

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  📊 Stats  |  Budget: $523.40  |  P&L: +23.40 USDC
  Trades: 14 total  |  3 open  |  11 resolved
  Win rate: 63.6%  |  Fees paid: 0.8120 USDC
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Strategy breakdown:
    [haiku-analyse]  8 trades  wr=63%  P&L=+18.20
    [whale-copy]     3 trades  wr=67%  P&L=+5.20
```

Stop the bot at any time with `Ctrl+C` — stats are printed on exit.

## Querying the Database

Trades are stored in `paper_trades.db` and can be queried directly with SQLite:

```bash
# All open trades
sqlite3 paper_trades.db "SELECT market_name, direction, price, amount FROM trades WHERE resolved=0;"

# Resolved P&L summary
sqlite3 paper_trades.db "SELECT market_name, direction, pnl FROM trades WHERE resolved=1 ORDER BY pnl DESC;"

# Total profit
sqlite3 paper_trades.db "SELECT ROUND(SUM(pnl), 2) as total_pnl FROM trades WHERE resolved=1;"
```

## Fees

The bot estimates Polymarket's taker fee using their parabolic formula (peaks at ~1.8% when price = 0.50). Maker orders earn a 0.2% rebate. Claude Haiku will prefer maker orders where possible to reduce costs.
