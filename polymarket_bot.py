#!/usr/bin/env python3
"""
Polymarket Automated Paper Trading Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs in the background, scans markets every N minutes,
and logs simulated paper trades with full P&L tracking.

Strategies:
  1. haiku-analyse  — Claude Haiku checks for positive edge
  2. whale-copy     — mirrors trades from tracked whale wallets
  3. whale+haiku    — whale signal confirmed by Haiku before entry

Setup:
  pip install anthropic requests
  export ANTHROPIC_API_KEY="sk-ant-..."
  python polymarket_bot.py

All trades are paper-only. No real money moves.
"""

import os, json, re, time, sqlite3, logging, sys, traceback
import urllib.parse
import xml.etree.ElementTree as ET
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timezone
from typing import Optional
import requests
import anthropic

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Config (override via environment variables) ───────────────────────────────
CFG = {
    # Required
    "ANTHROPIC_API_KEY":  os.getenv("ANTHROPIC_API_KEY", ""),

    # Budget
    "STARTING_BUDGET":    float(os.getenv("STARTING_BUDGET",  "500")),
    "TRADE_SIZE_PCT":     float(os.getenv("TRADE_SIZE_PCT",   "0.03")),  # 3% of budget per trade
    "MIN_TRADE_USDC":     float(os.getenv("MIN_TRADE_USDC",   "5")),

    # Market filters
    "MIN_LIQUIDITY":      float(os.getenv("MIN_LIQUIDITY",    "2000")),  # skip thin markets
    "PRICE_MIN":          float(os.getenv("PRICE_MIN",        "0.10")),  # skip extreme longshots
    "PRICE_MAX":          float(os.getenv("PRICE_MAX",        "0.90")),
    "MARKETS_PER_SCAN":   int(os.getenv("MARKETS_PER_SCAN",   "30")),    # top N by volume

    # Strategies
    "ENABLE_HAIKU":       os.getenv("ENABLE_HAIKU",       "true").lower() == "true",
    "ENABLE_WHALE_COPY":  os.getenv("ENABLE_WHALE_COPY",  "true").lower() == "true",
    "HAIKU_MIN_CONF":     float(os.getenv("HAIKU_MIN_CONF",   "0.65")),  # confidence threshold
    "WHALE_MIN_SIZE":     float(os.getenv("WHALE_MIN_SIZE",   "500")),   # min USD for whale trade
    "WHALE_LOOKBACK_MIN": int(os.getenv("WHALE_LOOKBACK_MIN", "30")),    # minutes to look back

    # Limits
    "MAX_OPEN_TRADES":    int(os.getenv("MAX_OPEN_TRADES", "10")),
    "SCAN_INTERVAL":      int(os.getenv("SCAN_INTERVAL",   "3600")),     # seconds between scans

    # Whale wallet addresses to track (comma-separated env var or add below)
    "WHALE_WALLETS": [
        w.strip() for w in os.getenv("WHALE_WALLETS", "").split(",") if w.strip()
        # Add known profitable wallets here, e.g.:
        # "0xabc123...",
        # "0xdef456...",
    ],
}

DB_FILE     = "paper_trades.db"
BUDGET_FILE = "budget.json"

# API base URLs
GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_URL  = "https://data-api.polymarket.com"
CLOB_URL  = "https://clob.polymarket.com"

# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_FILE)
    con.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            market_id   TEXT    NOT NULL,
            market_name TEXT    NOT NULL,
            token_id    TEXT,
            direction   TEXT    NOT NULL,   -- yes | no
            price       REAL    NOT NULL,   -- entry price 0-1
            amount      REAL    NOT NULL,   -- USDC staked
            fee         REAL    NOT NULL,   -- estimated fee (positive = cost)
            order_type  TEXT    NOT NULL,   -- taker | maker
            tags        TEXT    NOT NULL,   -- JSON list
            notes       TEXT,
            resolved    INTEGER DEFAULT 0,
            outcome     TEXT,               -- yes | no | NULL
            pnl         REAL,               -- realised P&L after fee
            close_ts    TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS scan_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT,
            markets_checked INTEGER,
            signals_found   INTEGER,
            trades_placed   INTEGER
        )
    """)
    con.commit()
    return con

# ── Budget ────────────────────────────────────────────────────────────────────
def load_budget() -> float:
    if os.path.exists(BUDGET_FILE):
        with open(BUDGET_FILE) as f:
            return json.load(f)["budget"]
    b = CFG["STARTING_BUDGET"]
    save_budget(b)
    return b

def save_budget(b: float):
    with open(BUDGET_FILE, "w") as f:
        json.dump({"budget": round(b, 4)}, f)

def open_trade_count(con) -> int:
    return con.execute("SELECT COUNT(*) FROM trades WHERE resolved=0").fetchone()[0]

# ── Fee calculation ───────────────────────────────────────────────────────────
def calc_fee(amount: float, price: float, order_type: str = "taker") -> float:
    """
    Taker fee: parabolic, peaks at 1.8% when price=0.50.
    Maker order: earns a +0.2% rebate (returned as negative fee).
    Formula: fee_rate = 0.018 * 4 * p * (1-p)
    """
    if order_type == "maker":
        return -amount * price * 0.002  # rebate
    rate = 0.018 * 4 * price * (1 - price)
    return amount * price * rate

# ── HTTP helpers ──────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "polymarket-paper-bot/1.0"})

def get(url: str, params: dict = None, timeout: int = 10) -> Optional[dict]:
    try:
        r = SESSION.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"GET {url} failed: {e}")
        return None

# ── Gamma API — market discovery ──────────────────────────────────────────────
def fetch_markets() -> list[dict]:
    """Fetch active, liquid binary markets sorted by volume."""
    data = get(f"{GAMMA_URL}/markets", params={
        "active": "true",
        "closed": "false",
        "limit":  CFG["MARKETS_PER_SCAN"] * 3,  # fetch extra, then filter
        "order":  "volume24hr",
        "ascending": "false",
    })
    if not data:
        return []

    markets = data if isinstance(data, list) else data.get("markets", [])
    result = []

    for m in markets:
        try:
            liquidity = float(m.get("liquidity") or 0)
            if liquidity < CFG["MIN_LIQUIDITY"]:
                continue

            prices_raw = m.get("outcomePrices") or m.get("bestBid")
            if prices_raw is None:
                continue

            # Parse price for YES outcome
            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
                yes_price = float(prices[0]) if prices else None
            elif isinstance(prices_raw, list):
                yes_price = float(prices_raw[0])
            else:
                yes_price = float(prices_raw)

            if yes_price is None:
                continue
            if not (CFG["PRICE_MIN"] <= yes_price <= CFG["PRICE_MAX"]):
                continue

            tokens = m.get("tokens") or m.get("clobTokenIds") or []
            token_id = tokens[0] if tokens else m.get("conditionId")

            result.append({
                "id":         m.get("id") or m.get("conditionId", ""),
                "name":       m.get("question") or m.get("title", "Unknown"),
                "yes_price":  yes_price,
                "no_price":   round(1 - yes_price, 4),
                "liquidity":  liquidity,
                "volume_24h": float(m.get("volume24hr") or m.get("volume") or 0),
                "token_id":   token_id,
                "end_date":   m.get("endDate") or m.get("endDateIso"),
                "slug":       m.get("slug", ""),
                "tags":       m.get("tags") or [],
            })

            if len(result) >= CFG["MARKETS_PER_SCAN"]:
                break

        except (TypeError, ValueError, KeyError):
            continue

    log.info(f"Fetched {len(result)} qualifying markets")
    return result

# ── Data API — whale tracking ─────────────────────────────────────────────────
def fetch_whale_recent_trades(wallet: str, lookback_min: int = 30) -> list[dict]:
    """Get recent trades for a wallet address."""
    data = get(f"{DATA_URL}/activity", params={"user": wallet, "limit": 50})
    if not data:
        return []

    trades = data if isinstance(data, list) else data.get("history", [])
    cutoff = time.time() - lookback_min * 60
    recent = []

    for t in trades:
        try:
            ts = t.get("timestamp") or t.get("createdAt") or 0
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            if ts < cutoff:
                continue

            size = float(t.get("usdcSize") or t.get("size") or 0)
            if size < CFG["WHALE_MIN_SIZE"]:
                continue

            recent.append({
                "market_id":  t.get("conditionId") or t.get("market"),
                "market_name": t.get("title") or t.get("market", ""),
                "direction":  "yes" if str(t.get("outcome", "")).lower() in ("yes", "1", "true") else "no",
                "price":      float(t.get("price") or 0.5),
                "size":       size,
                "ts":         ts,
                "wallet":     wallet,
            })
        except (TypeError, ValueError):
            continue

    return recent

_WALLET_RE = re.compile(r'^0x[0-9a-fA-F]{40}$')

# Cache leaderboard so we don't re-fetch it every scan
_leaderboard_cache: tuple[float, set[str]] = (0.0, set())
LEADERBOARD_CACHE_TTL = 3600  # seconds

def fetch_profitable_wallet_set(top_n: int = 30) -> set[str]:
    """Return a set of historically profitable wallet addresses, cached for 1 hour."""
    global _leaderboard_cache
    now = time.time()
    cached_at, cached_set = _leaderboard_cache
    if now - cached_at < LEADERBOARD_CACHE_TTL and cached_set:
        return cached_set

    data = get(f"{DATA_URL}/leaderboard", params={"limit": top_n})
    if not data:
        return cached_set  # return stale cache on failure rather than empty

    entries = data if isinstance(data, list) else data.get("data", [])
    wallets = set()
    for e in entries:
        addr = e.get("address") or e.get("user", "")
        if addr and _WALLET_RE.match(addr):
            wallets.add(addr)

    _leaderboard_cache = (now, wallets)
    log.info(f"Leaderboard refreshed: {len(wallets)} profitable wallets cached")
    return wallets

def fetch_global_recent_trades(min_size: float, lookback_min: int) -> list[dict]:
    """
    Fetch recent large trades from the global activity feed.
    Returns parsed trade dicts with wallet, market_id, direction, size, ts.
    """
    cutoff = time.time() - lookback_min * 60

    # Try the global trades endpoint first
    data = get(f"{DATA_URL}/trades", params={"limit": 200, "sizeThreshold": min_size})
    if not data:
        # Fallback: global activity without user filter
        data = get(f"{DATA_URL}/activity", params={"limit": 200})
    if not data:
        return []

    trades_raw = data if isinstance(data, list) else (
        data.get("trades") or data.get("history") or data.get("data") or []
    )
    result = []
    for t in trades_raw:
        try:
            ts = t.get("timestamp") or t.get("createdAt") or 0
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
            if ts < cutoff:
                continue

            size = float(t.get("usdcSize") or t.get("size") or 0)
            if size < min_size:
                continue

            wallet = t.get("maker") or t.get("taker") or t.get("user") or t.get("address") or ""
            if not wallet or not _WALLET_RE.match(wallet):
                continue

            result.append({
                "wallet":    wallet,
                "market_id": t.get("conditionId") or t.get("market") or "",
                "direction": "yes" if str(t.get("outcome", "")).lower() in ("yes", "1", "true") else "no",
                "price":     float(t.get("price") or 0.5),
                "size":      size,
                "ts":        ts,
            })
        except (TypeError, ValueError):
            continue

    return result

def get_whale_signal(markets: list[dict]) -> Optional[dict]:
    """
    Find a trade signal from a wallet that is both:
      - Currently active (made a large trade recently), AND
      - Historically profitable (on the leaderboard)

    Strategy:
      1. Pull the profitable wallet set from the leaderboard (cached).
      2. Pull recent global large trades (1-2 API calls).
      3. Return the first trade whose wallet is in the profitable set
         and whose market is in our current watch list.
      4. If the global feed is unavailable, fall back to per-wallet polling
         of manually configured WHALE_WALLETS.
    """
    market_ids = {m["id"]: m for m in markets}

    # ── Path A: configured wallets (manual override) ──────────────────────────
    if CFG["WHALE_WALLETS"]:
        for wallet in CFG["WHALE_WALLETS"][:15]:
            for trade in fetch_whale_recent_trades(wallet, CFG["WHALE_LOOKBACK_MIN"]):
                if trade["market_id"] in market_ids:
                    m = market_ids[trade["market_id"]]
                    log.info(
                        f"🐋 Whale signal (manual): {wallet[:10]}… "
                        f"{trade['direction'].upper()} on '{m['name'][:50]}' ${trade['size']:.0f}"
                    )
                    return {
                        "market":     m,
                        "direction":  trade["direction"],
                        "price":      m["yes_price"] if trade["direction"] == "yes" else m["no_price"],
                        "signal":     "whale-copy",
                        "whale":      wallet,
                        "whale_size": trade["size"],
                        "notes":      f"Whale {wallet[:10]}… traded ${trade['size']:.0f}",
                    }
            time.sleep(0.2)
        return None

    # ── Path B: auto-discover active + profitable whales ──────────────────────
    profitable = fetch_profitable_wallet_set(top_n=30)
    if not profitable:
        log.info("Leaderboard unavailable — skipping whale strategy this scan")
        return None

    log.info(f"Scanning global trade feed for activity from {len(profitable)} profitable wallets...")
    recent_trades = fetch_global_recent_trades(CFG["WHALE_MIN_SIZE"], CFG["WHALE_LOOKBACK_MIN"])

    if not recent_trades:
        log.info("Global trade feed returned no results — skipping whale strategy")
        return None

    log.info(f"Global feed: {len(recent_trades)} large trades in last {CFG['WHALE_LOOKBACK_MIN']}min")

    for trade in recent_trades:
        if trade["wallet"] not in profitable:
            continue  # active but not proven profitable — skip
        if trade["market_id"] not in market_ids:
            continue  # not a market we're watching

        m = market_ids[trade["market_id"]]
        log.info(
            f"🐋 Whale signal (active+profitable): {trade['wallet'][:10]}… "
            f"{trade['direction'].upper()} on '{m['name'][:50]}' ${trade['size']:.0f}"
        )
        return {
            "market":     m,
            "direction":  trade["direction"],
            "price":      m["yes_price"] if trade["direction"] == "yes" else m["no_price"],
            "signal":     "whale-copy",
            "whale":      trade["wallet"],
            "whale_size": trade["size"],
            "notes":      f"Whale {trade['wallet'][:10]}… traded ${trade['size']:.0f}",
        }

    log.info("No overlap between active large traders and profitable leaderboard this scan")
    return None

# ── Google News — context for Haiku ──────────────────────────────────────────
_news_cache: dict[str, tuple[float, list[str]]] = {}
NEWS_CACHE_TTL = 3600  # seconds

def fetch_news_headlines(query: str, max_items: int = 5) -> list[str]:
    """Fetch recent headlines from Google News RSS. Cached for 1 hour."""
    now = time.time()
    if query in _news_cache:
        cached_at, headlines = _news_cache[query]
        if now - cached_at < NEWS_CACHE_TTL:
            return headlines

    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = SESSION.get(url, timeout=8)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        headlines = [
            item.findtext("title", "")
            for item in root.findall("./channel/item")[:max_items]
            if item.findtext("title", "")
        ]
        _news_cache[query] = (now, headlines)
        return headlines
    except Exception as e:
        log.debug(f"News fetch failed for '{query[:40]}': {e}")
        return []

# ── Claude Haiku — edge detection ─────────────────────────────────────────────
_haiku_client: Optional[anthropic.Anthropic] = None

def get_haiku_client():
    global _haiku_client
    if _haiku_client is None:
        if not CFG["ANTHROPIC_API_KEY"]:
            raise RuntimeError("ANTHROPIC_API_KEY not set. Export it before running.")
        _haiku_client = anthropic.Anthropic(api_key=CFG["ANTHROPIC_API_KEY"])
    return _haiku_client

HAIKU_SYSTEM = """You are a prediction market analyst. Given a market, decide if there is a clear positive edge to trade.

Reply ONLY with valid JSON — no prose, no markdown fences:
{
  "edge": true | false,
  "direction": "yes" | "no",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence max",
  "order_type": "maker" | "taker"
}

Lean toward "edge: false" unless you see a clear mispricing.
Prefer "maker" orders (limit orders) to save on fees.
Only say edge:true if confidence >= 0.60."""

def haiku_analyse(market: dict) -> Optional[dict]:
    """Ask Claude Haiku if a market has a tradeable edge."""
    client = get_haiku_client()

    headlines = fetch_news_headlines(market["name"])
    news_section = ""
    if headlines:
        news_section = "\nRecent news:\n" + "\n".join(f"- {h}" for h in headlines) + "\n"

    prompt = (
        f"Market: {market['name']}\n"
        f"Current YES price: {market['yes_price']:.3f} ({market['yes_price']*100:.1f}%)\n"
        f"Current NO price:  {market['no_price']:.3f} ({market['no_price']*100:.1f}%)\n"
        f"24h volume: ${market['volume_24h']:,.0f}\n"
        f"Liquidity:  ${market['liquidity']:,.0f}\n"
        f"Category tags: {', '.join(str(t) for t in market.get('tags', []))}\n"
        f"{news_section}\n"
        "Is there a positive edge here? Consider: does the news suggest the market is "
        "mispriced? If you have no strong signal, say edge:false."
    )

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=HAIKU_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip any accidental markdown fences
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)
        log.info(
            f"🤖 Haiku: '{market['name'][:45]}…' → "
            f"edge={result.get('edge')} conf={result.get('confidence'):.2f} "
            f"dir={result.get('direction')} [{result.get('reasoning','')[:60]}]"
        )
        return result
    except json.JSONDecodeError:
        log.warning(f"Haiku returned non-JSON: {raw[:100]}")
        return None
    except Exception as e:
        log.error(f"Haiku API error: {e}")
        return None

# ── Paper trade engine ─────────────────────────────────────────────────────────
def place_paper_trade(
    con, budget: float,
    market: dict,
    direction: str,
    price: float,
    order_type: str,
    tags: list[str],
    notes: str = "",
) -> Optional[float]:
    """
    Simulate placing a trade. Deducts from budget and writes to DB.
    Returns the new budget, or None if trade was skipped.
    """
    if open_trade_count(con) >= CFG["MAX_OPEN_TRADES"]:
        log.info("Max open trades reached — skipping")
        return None

    amount = round(budget * CFG["TRADE_SIZE_PCT"], 2)
    amount = max(amount, CFG["MIN_TRADE_USDC"])

    if amount > budget * 0.95:
        log.warning(f"Insufficient budget (${budget:.2f}) for trade of ${amount:.2f}")
        return None

    fee = calc_fee(amount, price, order_type)
    new_budget = budget - amount

    con.execute("""
        INSERT INTO trades
            (ts, market_id, market_name, token_id, direction, price, amount,
             fee, order_type, tags, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        market["id"],
        market["name"],
        market.get("token_id"),
        direction,
        price,
        amount,
        fee,
        order_type,
        json.dumps(tags),
        notes,
    ))
    con.commit()
    save_budget(new_budget)

    gross_if_win = amount * (1 - price) / price
    log.info(
        f"📝 Paper trade logged:\n"
        f"   Market:  {market['name'][:60]}\n"
        f"   {direction.upper()} @ {price:.3f}  |  ${amount:.2f} staked\n"
        f"   Fee est: {fee:+.4f} USDC  |  Win payoff: +${gross_if_win:.2f}\n"
        f"   Tags:    {', '.join(tags)}\n"
        f"   Budget:  ${budget:.2f} → ${new_budget:.2f}"
    )
    return new_budget

# ── Auto-resolver: checks if open trades have settled ─────────────────────────
def resolve_settled_trades(con) -> float:
    """
    Check open trades against current market data.
    If a market is resolved, mark the trade and adjust budget.
    Returns total P&L from newly resolved trades.
    """
    rows = con.execute(
        "SELECT id, market_id, direction, price, amount, fee FROM trades WHERE resolved=0"
    ).fetchall()
    if not rows:
        return 0.0

    total_pnl = 0.0
    budget = load_budget()

    for row_id, market_id, direction, price, amount, fee in rows:
        data = get(f"{GAMMA_URL}/markets/{market_id}")
        if not data:
            continue

        # Accept both list and dict responses
        m = data[0] if isinstance(data, list) else data

        if not m.get("closed") and not m.get("resolved"):
            continue  # still open

        # Determine winner
        winner = None
        if m.get("winner"):
            winner = str(m["winner"]).lower()
        elif m.get("resolvedAt") or m.get("resolution"):
            res = str(m.get("resolution") or "").lower()
            winner = "yes" if res in ("1", "true", "yes") else "no"

        if not winner:
            continue  # resolved but outcome unclear yet

        won = (direction == winner)
        gross = amount * (1 - price) / price if won else -amount
        pnl = gross - fee

        con.execute("""
            UPDATE trades
            SET resolved=1, outcome=?, pnl=?, close_ts=?
            WHERE id=?
        """, (winner, round(pnl, 4), datetime.now(timezone.utc).isoformat(), row_id))
        con.commit()

        budget += amount + pnl
        total_pnl += pnl

        status = "✅ WON" if won else "❌ LOST"
        log.info(
            f"{status} — resolved trade #{row_id}: {direction.upper()} "
            f"on market {market_id[:20]}… | P&L: {pnl:+.2f} USDC"
        )
        time.sleep(0.3)

    if total_pnl != 0.0:
        save_budget(budget)
    return total_pnl

# ── Stats printer ─────────────────────────────────────────────────────────────
def print_stats(con):
    rows = con.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) as resolved,
            SUM(CASE WHEN resolved=0 THEN 1 ELSE 0 END) as open,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            ROUND(SUM(COALESCE(pnl, 0)), 2) as total_pnl,
            ROUND(SUM(fee), 4) as total_fees
        FROM trades
    """).fetchone()

    total, resolved, open_c, wins, pnl, fees = rows
    win_rate = f"{wins/resolved*100:.1f}%" if resolved else "n/a"
    budget = load_budget()

    log.info(
        f"\n{'━'*55}\n"
        f"  📊 Stats  |  Budget: ${budget:.2f}  |  P&L: {pnl:+.2f} USDC\n"
        f"  Trades: {total} total  |  {open_c} open  |  {resolved} resolved\n"
        f"  Win rate: {win_rate}  |  Fees paid: {fees:.4f} USDC\n"
        f"{'━'*55}"
    )

    # Per-tag breakdown
    tag_rows = con.execute(
        "SELECT tags, pnl FROM trades WHERE resolved=1 AND pnl IS NOT NULL"
    ).fetchall()

    tag_stats: dict = {}
    for tags_json, t_pnl in tag_rows:
        for tag in json.loads(tags_json):
            s = tag_stats.setdefault(tag, {"count": 0, "pnl": 0.0, "wins": 0})
            s["count"] += 1
            s["pnl"] += t_pnl
            if t_pnl > 0:
                s["wins"] += 1

    if tag_stats:
        log.info("  Strategy breakdown:")
        for tag, s in sorted(tag_stats.items(), key=lambda x: -x[1]["pnl"]):
            wr = f"{s['wins']/s['count']*100:.0f}%" if s["count"] else "—"
            log.info(f"    [{tag}]  {s['count']} trades  wr={wr}  P&L={s['pnl']:+.2f}")

# ── Main scan loop ─────────────────────────────────────────────────────────────
def run_scan(con):
    budget = load_budget()
    log.info(f"\n{'═'*55}\n  🔍 Starting scan  |  Budget: ${budget:.2f}\n{'═'*55}")

    # First, resolve any settled open trades
    resolve_settled_trades(con)

    # Check capacity
    if open_trade_count(con) >= CFG["MAX_OPEN_TRADES"]:
        log.info("At max open trades — skipping new entries this scan")
        return

    # Fetch live markets
    markets = fetch_markets()
    if not markets:
        log.warning("No markets returned — API may be down")
        return

    signals_found = 0
    trades_placed = 0
    # Track which market IDs we already traded this scan to avoid duplication
    traded_this_scan: set = set()

    # ── Strategy: Whale copy ──────────────────────────────────────────────────
    if CFG["ENABLE_WHALE_COPY"]:
        log.info("Checking whale wallets...")
        whale_signal = get_whale_signal(markets)

        if whale_signal and whale_signal["market"]["id"] not in traded_this_scan:
            signals_found += 1
            m      = whale_signal["market"]
            tags   = ["whale-copy"]
            dir_   = whale_signal["direction"]
            price  = whale_signal["price"]
            otype  = "taker"  # whale copy needs immediate execution

            # Optionally confirm with Haiku before copying
            skip_trade = False
            if CFG["ENABLE_HAIKU"]:
                log.info("Confirming whale signal with Haiku...")
                analysis = haiku_analyse(m)
                if analysis and analysis.get("edge") and analysis.get("confidence", 0) >= CFG["HAIKU_MIN_CONF"]:
                    tags  = ["whale+haiku"]
                    otype = analysis.get("order_type", "taker")
                elif analysis and not analysis.get("edge"):
                    log.info("Haiku rejected whale signal — skipping trade")
                    skip_trade = True
                    analysis = None
                else:
                    analysis = None  # haiku inconclusive — proceed as pure whale copy
            else:
                analysis = None

            notes = whale_signal["notes"]
            if analysis:
                notes += f" | Haiku conf={analysis.get('confidence'):.2f}: {analysis.get('reasoning','')}"

            if skip_trade:
                new_budget = None
            else:
                new_budget = place_paper_trade(con, budget, m, dir_, price, otype, tags, notes)
            if new_budget is not None:
                budget = new_budget
                trades_placed += 1
                traded_this_scan.add(m["id"])

    # ── Strategy: Haiku standalone ────────────────────────────────────────────
    if CFG["ENABLE_HAIKU"] and trades_placed < 3:
        log.info(f"Running Haiku analysis on top {min(10, len(markets))} markets...")
        for m in markets[:10]:
            if m["id"] in traded_this_scan:
                continue
            if open_trade_count(con) >= CFG["MAX_OPEN_TRADES"]:
                break

            analysis = haiku_analyse(m)
            time.sleep(0.5)  # rate limit

            if not analysis:
                continue
            if not analysis.get("edge"):
                continue
            if analysis.get("confidence", 0) < CFG["HAIKU_MIN_CONF"]:
                continue

            signals_found += 1
            dir_   = analysis.get("direction", "yes")
            price  = m["yes_price"] if dir_ == "yes" else m["no_price"]
            otype  = analysis.get("order_type", "maker")
            notes  = analysis.get("reasoning", "")

            new_budget = place_paper_trade(
                con, budget, m, dir_, price, otype,
                tags=["haiku-analyse"],
                notes=notes,
            )
            if new_budget is not None:
                budget = new_budget
                trades_placed += 1
                traded_this_scan.add(m["id"])

            if trades_placed >= 3:  # cap new trades per scan
                break

    # Log scan summary
    con.execute(
        "INSERT INTO scan_log (ts, markets_checked, signals_found, trades_placed) VALUES (?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), len(markets), signals_found, trades_placed),
    )
    con.commit()
    log.info(f"Scan complete: {len(markets)} markets checked, {signals_found} signals, {trades_placed} trades placed")


# ── Entry point ────────────────────────────────────────────────────────────────
def main():
    log.info("━" * 55)
    log.info("  Polymarket Paper Trading Bot  (paper money only)")
    log.info("━" * 55)

    if not CFG["ANTHROPIC_API_KEY"] and CFG["ENABLE_HAIKU"]:
        log.error("ANTHROPIC_API_KEY not set. Run: export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    con = init_db()
    try:
        budget = load_budget()
        log.info(f"Starting budget: ${budget:.2f} USDC")
        log.info(f"Strategies: haiku={CFG['ENABLE_HAIKU']}, whale_copy={CFG['ENABLE_WHALE_COPY']}")
        log.info(f"Scan interval: {CFG['SCAN_INTERVAL']}s  |  Max open trades: {CFG['MAX_OPEN_TRADES']}")
        log.info(f"Trade size: {CFG['TRADE_SIZE_PCT']*100:.0f}% of budget per trade (~${budget*CFG['TRADE_SIZE_PCT']:.2f})")

        scan_count = 0
        while True:
            try:
                run_scan(con)
                scan_count += 1
                if scan_count % 12 == 0:  # print stats every ~hour
                    print_stats(con)
            except KeyboardInterrupt:
                log.info("Stopped by user.")
                print_stats(con)
                break
            except Exception:
                log.error(f"Scan error:\n{traceback.format_exc()}")

            log.info(f"Sleeping {CFG['SCAN_INTERVAL']}s until next scan...")
            time.sleep(CFG["SCAN_INTERVAL"])
    finally:
        con.close()


if __name__ == "__main__":
    main()
