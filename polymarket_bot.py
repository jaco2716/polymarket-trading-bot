#!/usr/bin/env python3
"""
Polymarket Automated Trading Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Runs in the background, scans markets every N minutes,
and places trades with full P&L tracking.

Modes:
  shadow   — no real trades; logs hypothetical outcomes for all strategies
  compete  — paper trades; strategies share 3 slots per scan
  parallel — paper trades; each strategy places independently
  live     — REAL trades via Polymarket CLOB API (requires private key)

Strategies:
  1. haiku-analyse  — Claude Haiku checks for positive edge
  2. whale-copy     — mirrors trades from tracked whale wallets (runs independently, not competing with Haiku)

Setup:
  pip install -r requirements.txt
  export ANTHROPIC_API_KEY="sk-ant-..."
  python polymarket_bot.py

For live trading, also set POLYMARKET_PRIVATE_KEY and STRATEGY_MODE=live in .env.
"""

import os, json, re, time, sqlite3, logging, logging.handlers, sys, traceback, random
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
        logging.handlers.RotatingFileHandler(
            "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
        ),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ── Config (override via environment variables) ───────────────────────────────
CFG = {
    # Required
    "ANTHROPIC_API_KEY":  os.getenv("ANTHROPIC_API_KEY", ""),

    # Budget
    "STARTING_BUDGET":         float(os.getenv("STARTING_BUDGET",         "500")),
    "SHADOW_STARTING_BUDGET":  float(os.getenv("SHADOW_STARTING_BUDGET",  os.getenv("STARTING_BUDGET", "500"))),
    "TRADE_SIZE_PCT":          float(os.getenv("TRADE_SIZE_PCT",          "0.03")),  # 3% of budget per trade
    "MIN_TRADE_USDC":          float(os.getenv("MIN_TRADE_USDC",          "5")),

    # Market filters
    "MIN_LIQUIDITY":      float(os.getenv("MIN_LIQUIDITY",    "2000")),  # skip thin markets
    "MAX_TRADE_PRICE":    float(os.getenv("MAX_TRADE_PRICE",  "0.85")),  # skip near-certain outcomes; forces yes/no into [0.15, 0.85]
    "MIN_TRADE_PRICE":    float(os.getenv("MIN_TRADE_PRICE",  "0.15")),  # skip very cheap markets (high-loss territory)
    "MAX_RESOLVE_HOURS":       float(os.getenv("MAX_RESOLVE_HOURS",       "0")),  # 0 = no limit; set e.g. 24 for same-day markets only
    "MAX_WHALE_RESOLVE_HOURS": float(os.getenv("MAX_WHALE_RESOLVE_HOURS", "0")),  # 0 = no limit; whales often hold long-term positions
    "MARKET_POOL_SIZE":   int(os.getenv("MARKET_POOL_SIZE",   "100")),   # fetch this many, then rank and pick best
    "MARKETS_PER_SCAN":   int(os.getenv("MARKETS_PER_SCAN",   "20")),    # top N to send to Haiku after ranking

    # Strategies
    "ENABLE_HAIKU":           os.getenv("ENABLE_HAIKU",       "true").lower() == "true",
    "ENABLE_WHALE_COPY":      os.getenv("ENABLE_WHALE_COPY",  "true").lower() == "true",
    "HAIKU_MIN_CONF":         float(os.getenv("HAIKU_MIN_CONF",        "0.65")),  # confidence threshold for real trades
    "SHADOW_HAIKU_MIN_CONF":  float(os.getenv("SHADOW_HAIKU_MIN_CONF", "0.35")),  # lower threshold for shadow data collection
    "CONF_FLOOR":             float(os.getenv("CONF_FLOOR",             "0.45")),  # lowest confidence accepted (for low-price markets)
    "SIZE_FLOOR":             float(os.getenv("SIZE_FLOOR",              "0.5")),   # min trade size fraction (for low-price markets)
    "HAIKU_SKIP_SPORTS":          os.getenv("HAIKU_SKIP_SPORTS", "true").lower() == "true",  # block game matchups/spreads
    "WHALE_MIN_SIZE":             float(os.getenv("WHALE_MIN_SIZE",             "500")),  # min USD for whale trade
    "MAX_WHALE_TRADES_PER_SCAN":  int(os.getenv("MAX_WHALE_TRADES_PER_SCAN",    "5")),   # cap on whale trades in a single scan
    "WHALE_LEADERBOARD_SIZE":     int(os.getenv("WHALE_LEADERBOARD_SIZE",       "30")),  # how many top wallets to fetch from leaderboard
    "WHALE_WALLETS_PER_SCAN":     int(os.getenv("WHALE_WALLETS_PER_SCAN",       "15")),  # how many wallets to query for positions each scan

    # Limits
    "MAX_OPEN_TRADES":    int(os.getenv("MAX_OPEN_TRADES", "10")),
    "SCAN_INTERVAL":      int(os.getenv("SCAN_INTERVAL",   "3600")),     # seconds between scans

    # Strategy mode
    # "compete"  — strategies share 3 trade slots per scan (default)
    # "parallel" — each strategy gets its own slot; use this to A/B test
    # "shadow"   — no real trades; logs hypothetical outcomes for all 3 strategies
    "STRATEGY_MODE":      os.getenv("STRATEGY_MODE", "compete"),

    # Shadow scan interval (seconds) — only used in shadow mode.
    # Set lower than SCAN_INTERVAL to collect training data faster.
    # Cost scales linearly: 15min ≈ $12/mo, 10min ≈ $18/mo, 5min ≈ $36/mo
    "SHADOW_SCAN_INTERVAL": int(os.getenv("SHADOW_SCAN_INTERVAL", os.getenv("SCAN_INTERVAL", "3600"))),

    # Low budget cooldown (seconds) — when trade size (TRADE_SIZE_PCT * budget) < MIN_TRADE_USDC,
    # pause trading for this duration to let open trades resolve and free up budget.
    "LOW_BUDGET_COOLDOWN": int(os.getenv("LOW_BUDGET_COOLDOWN", "7200")),  # default 2 hours

    # Whale wallet addresses to track (comma-separated env var or add below)
    "WHALE_WALLETS": [
        w.strip() for w in os.getenv("WHALE_WALLETS", "").split(",") if w.strip()
        # Add known profitable wallets here, e.g.:
        # "0xabc123...",
        # "0xdef456...",
    ],

    # Live trading (CLOB client) — only used when STRATEGY_MODE="live"
    "POLYMARKET_PRIVATE_KEY":    os.getenv("POLYMARKET_PRIVATE_KEY", ""),
    "POLYMARKET_FUNDER_ADDRESS": os.getenv("POLYMARKET_FUNDER_ADDRESS", ""),
    "MAX_LIVE_TRADE_USDC":       float(os.getenv("MAX_LIVE_TRADE_USDC", "50")),
    "LIVE_MAX_OPEN_TRADES":      int(os.getenv("LIVE_MAX_OPEN_TRADES", "5")),
    "LIVE_DRY_RUN":              os.getenv("LIVE_DRY_RUN", "true").lower() == "true",
}

DB_FILE            = "paper_trades.db"
BUDGET_FILE        = "budget.json"
SHADOW_BUDGET_FILE = "shadow_budget.json"
LIVE_BUDGET_FILE   = "live_budget.json"

# API base URLs
GAMMA_URL = "https://gamma-api.polymarket.com"
DATA_URL  = "https://data-api.polymarket.com"
CLOB_URL  = "https://clob.polymarket.com"

# ── CLOB client (live trading) ───────────────────────────────────────────────
_clob_client = None

def get_clob_client():
    """Lazy-init singleton for the Polymarket CLOB client."""
    global _clob_client
    if _clob_client is not None:
        return _clob_client
    if not CFG["POLYMARKET_PRIVATE_KEY"]:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY not set — cannot trade in live mode")
    from py_clob_client.client import ClobClient
    _clob_client = ClobClient(
        CLOB_URL,
        key=CFG["POLYMARKET_PRIVATE_KEY"],
        chain_id=137,
        signature_type=0,
        funder=CFG["POLYMARKET_FUNDER_ADDRESS"] or None,
    )
    _clob_client.set_api_creds(_clob_client.create_or_derive_api_creds())
    log.info("CLOB client initialised (Polygon mainnet)")
    return _clob_client


def validate_live_trading_setup():
    """Pre-flight checks for live trading. Raises on failure."""
    if not CFG["POLYMARKET_PRIVATE_KEY"]:
        raise RuntimeError("POLYMARKET_PRIVATE_KEY is required for live trading")
    get_clob_client()  # eagerly init to catch key/auth errors early
    # Verify connectivity by fetching a known market
    try:
        test = requests.get(f"{CLOB_URL}/markets", params={"limit": 1}, timeout=10)
        test.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Cannot reach CLOB API: {e}")
    log.info("Live trading setup validated — CLOB API reachable")


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
            close_ts    TEXT,
            end_date    TEXT,               -- market resolution date (ISO)
            liquidity   REAL,               -- market liquidity at entry
            volume_24h  REAL,               -- 24h volume at entry
            market_slug TEXT,               -- polymarket URL slug
            market_tags TEXT,               -- market category tags (JSON list)
            whale_size  REAL                -- USD size of whale position copied (NULL for non-whale trades)
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
    con.execute("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            strategy    TEXT    NOT NULL,   -- haiku-analyse | whale-copy
            market_id   TEXT    NOT NULL,
            market_name TEXT    NOT NULL,
            direction   TEXT    NOT NULL,
            price       REAL    NOT NULL,
            amount      REAL    NOT NULL,   -- hypothetical stake (no real budget impact)
            fee         REAL,               -- estimated taker fee (for apples-to-apples P&L vs real trades)
            confidence  REAL,               -- haiku confidence score (NULL for whale-copy)
            notes       TEXT,               -- reasoning: haiku explanation or whale context
            resolved    INTEGER DEFAULT 0,
            outcome     TEXT,
            pnl         REAL,               -- realised P&L after fee
            close_ts    TEXT,
            end_date    TEXT,               -- market resolution date (ISO)
            liquidity   REAL,               -- market liquidity at entry
            volume_24h  REAL,               -- 24h volume at entry
            market_slug TEXT,               -- polymarket URL slug
            market_tags TEXT                -- market category tags (JSON list)
        )
    """)
    # Migrate existing shadow_trades table if columns are missing
    for col, definition in [
        ("confidence",  "REAL"),
        ("notes",       "TEXT"),
        ("fee",         "REAL"),
        ("end_date",    "TEXT"),
        ("liquidity",   "REAL"),
        ("volume_24h",  "REAL"),
        ("market_slug", "TEXT"),
        ("market_tags", "TEXT"),
    ]:
        try:
            con.execute(f"ALTER TABLE shadow_trades ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # column already exists
    # Migrate trades table
    for col, definition in [
        ("mode",        "TEXT DEFAULT 'paper'"),
        ("order_id",    "TEXT"),
        ("end_date",    "TEXT"),
        ("liquidity",   "REAL"),
        ("volume_24h",  "REAL"),
        ("market_slug", "TEXT"),
        ("market_tags", "TEXT"),
        ("whale_size",  "REAL"),
    ]:
        try:
            con.execute(f"ALTER TABLE trades ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass
    # Indexes for common queries
    con.execute("CREATE INDEX IF NOT EXISTS idx_trades_resolved ON trades(resolved)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_trades_market_id ON trades(market_id)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_shadow_resolved ON shadow_trades(resolved)")
    con.execute("CREATE INDEX IF NOT EXISTS idx_shadow_market_id ON shadow_trades(market_id)")
    con.execute("PRAGMA journal_mode=WAL")
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
    tmp = BUDGET_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"budget": round(b, 4)}, f)
    os.replace(tmp, BUDGET_FILE)

def load_shadow_budget() -> float:
    if os.path.exists(SHADOW_BUDGET_FILE):
        with open(SHADOW_BUDGET_FILE) as f:
            return json.load(f)["budget"]
    b = CFG["SHADOW_STARTING_BUDGET"]
    save_shadow_budget(b)
    return b

def save_shadow_budget(b: float):
    tmp = SHADOW_BUDGET_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"budget": round(b, 4)}, f)
    os.replace(tmp, SHADOW_BUDGET_FILE)

def load_live_budget() -> float:
    if os.path.exists(LIVE_BUDGET_FILE):
        with open(LIVE_BUDGET_FILE) as f:
            return json.load(f)["budget"]
    b = CFG["STARTING_BUDGET"]
    save_live_budget(b)
    return b

def save_live_budget(b: float):
    tmp = LIVE_BUDGET_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"budget": round(b, 4)}, f)
    os.replace(tmp, LIVE_BUDGET_FILE)

def open_trade_count(con, mode_filter: str = None) -> int:
    if mode_filter:
        return con.execute(
            "SELECT COUNT(*) FROM trades WHERE resolved=0 AND mode=?", (mode_filter,)
        ).fetchone()[0]
    return con.execute("SELECT COUNT(*) FROM trades WHERE resolved=0").fetchone()[0]

def open_market_ids(con) -> set:
    """Return market IDs that already have an open (unresolved) real trade."""
    rows = con.execute("SELECT market_id FROM trades WHERE resolved=0").fetchall()
    return {r[0] for r in rows}

def open_shadow_market_ids(con) -> set:
    """Return market IDs that already have an open (unresolved) shadow trade."""
    rows = con.execute("SELECT market_id FROM shadow_trades WHERE resolved=0").fetchall()
    return {r[0] for r in rows}

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


def price_scaled_params(price: float, base_conf: float, base_amount: float) -> tuple[float, float]:
    """Scale confidence threshold and trade amount based on entry price.

    Low-price markets (high payout) get a lower confidence bar and smaller
    position size — the asymmetric payout compensates for lower conviction.

    Returns (min_confidence, adjusted_amount).
    """
    lo, hi = 0.15, CFG["MAX_TRADE_PRICE"]
    ratio = max(0.0, min(1.0, (price - lo) / (hi - lo)))
    min_conf  = CFG["CONF_FLOOR"] + (base_conf - CFG["CONF_FLOOR"]) * ratio
    size_mult = CFG["SIZE_FLOOR"] + (1.0 - CFG["SIZE_FLOOR"]) * ratio
    return min_conf, round(base_amount * size_mult, 2)


# ── HTTP helpers ──────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "polymarket-paper-bot/1.0"})

def get(url: str, params: dict = None, timeout: int = 10, retries: int = 3) -> Optional[dict]:
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 2 ** attempt
                log.warning(f"GET {url} → {r.status_code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                log.warning(f"GET {url} failed (attempt {attempt+1}): {e}")
                time.sleep(2 ** attempt)
                continue
            log.warning(f"GET {url} failed after {retries} attempts: {e}")
            return None
        except json.JSONDecodeError as e:
            log.warning(f"GET {url} returned invalid JSON: {e} — body: {r.text[:200]}")
            return None
    return None

# ── Gamma API — market discovery ──────────────────────────────────────────────
def fetch_markets(randomize: bool = False) -> list[dict]:
    """Fetch a large pool of active markets, rank by interest score, return top MARKETS_PER_SCAN.

    Scoring (higher = more worth analysing):
      - 70% closeness to 50/50: markets near fair value are most likely to be mispriced
      - 30% normalised volume:  liquid markets are easier to act on

    In shadow mode (randomize=True) shuffles the scored pool so different markets
    get coverage across scans instead of always seeing the same top-N.
    """
    data = get(f"{GAMMA_URL}/markets", params={
        "active":    "true",
        "closed":    "false",
        "limit":     CFG["MARKET_POOL_SIZE"],
        "order":     "volume24hr",
        "ascending": "false",
    })
    if not data:
        return []

    markets = data if isinstance(data, list) else data.get("markets", [])
    pool = []

    for m in markets:
        try:
            liquidity = float(m.get("liquidity") or 0)
            if liquidity < CFG["MIN_LIQUIDITY"]:
                continue

            prices_raw = m.get("outcomePrices") or m.get("bestBid")
            if prices_raw is None:
                continue

            if isinstance(prices_raw, str):
                prices = json.loads(prices_raw)
                yes_price = float(prices[0]) if prices else None
            elif isinstance(prices_raw, list):
                yes_price = float(prices_raw[0])
            else:
                yes_price = float(prices_raw)

            if yes_price is None:
                continue

            # Skip near-certain outcomes — tiny payout, high price risk.
            # MAX_TRADE_PRICE=0.85 enforces both sides into [0.15, 0.85].
            no_price = round(1 - yes_price, 4)
            if max(yes_price, no_price) > CFG["MAX_TRADE_PRICE"]:
                continue

            # Skip if market resolves too far in the future (MAX_RESOLVE_HOURS > 0)
            if CFG["MAX_RESOLVE_HOURS"] > 0:
                end_date_str = m.get("endDate") or m.get("endDateIso")
                if end_date_str:
                    try:
                        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                        if hours_left > CFG["MAX_RESOLVE_HOURS"] or hours_left < 0:
                            continue
                    except ValueError:
                        pass  # can't parse date, allow it through

            tokens = m.get("tokens") or m.get("clobTokenIds") or []
            yes_token_id = None
            no_token_id  = None
            if tokens:
                if isinstance(tokens[0], dict):
                    for t in tokens:
                        outcome = str(t.get("outcome", "")).lower()
                        tid = t.get("token_id") or t.get("id", "")
                        if outcome == "yes":
                            yes_token_id = tid
                        elif outcome == "no":
                            no_token_id = tid
                elif isinstance(tokens[0], str):
                    yes_token_id = tokens[0]
                    no_token_id = tokens[1] if len(tokens) > 1 else None
            token_id = yes_token_id or m.get("conditionId")
            volume = float(m.get("volume24hr") or m.get("volume") or 0)

            pool.append({
                "id":            m.get("id") or m.get("conditionId", ""),
                "name":          m.get("question") or m.get("title", "Unknown"),
                "yes_price":     yes_price,
                "no_price":      round(1 - yes_price, 4),
                "liquidity":     liquidity,
                "volume_24h":    volume,
                "token_id":      token_id,
                "yes_token_id":  yes_token_id,
                "no_token_id":   no_token_id,
                "condition_id":  m.get("conditionId") or m.get("condition_id"),
                "end_date":      m.get("endDate") or m.get("endDateIso"),
                "slug":          m.get("slug", ""),
                "tags":          m.get("tags") or [],
            })
        except (TypeError, ValueError, KeyError):
            continue

    if not pool:
        return []

    # Score each market
    max_vol = max(m["volume_24h"] for m in pool) or 1
    for m in pool:
        closeness = 1.0 - abs(m["yes_price"] - 0.5) * 2   # 1.0 at 50/50, 0.0 at extremes
        vol_norm  = m["volume_24h"] / max_vol
        m["_score"] = closeness * 0.7 + vol_norm * 0.3

    if randomize:
        # Pure random sample across the full pool — no scoring bias.
        # Shadow mode needs coverage of all market types to learn which are actually profitable.
        random.shuffle(pool)
        result = pool[:CFG["MARKETS_PER_SCAN"]]
    else:
        pool.sort(key=lambda m: -m["_score"])
        result = pool[:CFG["MARKETS_PER_SCAN"]]

    selection_method = "randomly sampled" if randomize else "top by interest score"
    log.info(f"Fetched {len(pool)} qualifying markets → {selection_method} {len(result)}")
    return result

# ── Data API — whale tracking ─────────────────────────────────────────────────
def fetch_market_by_id(condition_id: str) -> Optional[dict]:
    """Fetch a single market by conditionId using the CLOB API."""
    data = get(f"{CLOB_URL}/markets/{condition_id}")
    if not data or not isinstance(data, dict):
        return None
    try:
        if data.get("closed") or not data.get("active", True):
            return None
        tokens = data.get("tokens") or []
        yes_token = next((t for t in tokens if t.get("outcome", "").lower() == "yes"), None)
        no_token  = next((t for t in tokens if t.get("outcome", "").lower() == "no"), None)
        if not yes_token:
            return None
        yes_price = float(yes_token.get("price") or 0)
        if yes_price <= 0:
            return None
        yes_token_id = yes_token.get("token_id")
        no_token_id  = no_token.get("token_id") if no_token else None
        return {
            "id":            condition_id,
            "name":          data.get("question", "Unknown"),
            "yes_price":     yes_price,
            "no_price":      round(1 - yes_price, 4),
            "liquidity":     0.0,   # not available from CLOB endpoint
            "volume_24h":    0.0,
            "token_id":      yes_token_id,
            "yes_token_id":  yes_token_id,
            "no_token_id":   no_token_id,
            "condition_id":  condition_id,
            "end_date":      data.get("end_date_iso"),
            "slug":          data.get("market_slug", ""),
            "tags":          data.get("tags") or [],
        }
    except (TypeError, ValueError, KeyError):
        return None


def resolve_token_ids(market: dict) -> tuple[Optional[str], Optional[str]]:
    """Return (yes_token_id, no_token_id) for a market, falling back to CLOB API if needed."""
    yes_tid = market.get("yes_token_id")
    no_tid  = market.get("no_token_id")
    if yes_tid and no_tid:
        return yes_tid, no_tid
    # Fallback: query CLOB API by conditionId
    cid = market.get("condition_id") or market.get("id")
    if not cid:
        return yes_tid, no_tid
    data = get(f"{CLOB_URL}/markets/{cid}")
    if not data or not isinstance(data, dict):
        return yes_tid, no_tid
    for t in (data.get("tokens") or []):
        outcome = str(t.get("outcome", "")).lower()
        tid = t.get("token_id")
        if outcome == "yes" and not yes_tid:
            yes_tid = tid
        elif outcome == "no" and not no_tid:
            no_tid = tid
    return yes_tid, no_tid


def fetch_whale_positions(wallet: str, min_size: float) -> list[dict]:
    """Get current open positions for a wallet above min_size USDC."""
    data = get(f"{DATA_URL}/positions", params={
        "user":          wallet,
        "sizeThreshold": 1,
        "limit":         100,
        "sortBy":        "CURRENT",
        "sortDirection": "DESC",
    })
    if not data:
        return []

    positions = data if isinstance(data, list) else data.get("data", [])
    result = []
    for p in positions:
        try:
            # Skip resolved/redeemable positions
            if p.get("redeemable") or float(p.get("curPrice") or 0) <= 0:
                continue

            current_value = float(p.get("currentValue") or 0)
            if current_value < min_size:
                continue

            direction = "yes" if p.get("outcomeIndex") == 0 else "no"
            result.append({
                "market_id":   p.get("conditionId") or "",
                "market_name": p.get("title") or "",
                "direction":   direction,
                "price":       float(p.get("curPrice") or 0.5),
                "size":        current_value,
                "wallet":      wallet,
            })
        except (TypeError, ValueError):
            continue
    return result

_WALLET_RE = re.compile(r'^0x[0-9a-fA-F]{40}$')

# Cache leaderboard so we don't re-fetch it every scan
_leaderboard_cache: tuple[float, list[str]] = (0.0, [])
LEADERBOARD_CACHE_TTL = 3600  # seconds

def fetch_profitable_wallet_list(top_n: int = 30) -> list[str]:
    """Return profitable wallet addresses ordered by weekly PNL (best first), cached for 1 hour."""
    global _leaderboard_cache
    now = time.time()
    cached_at, cached_list = _leaderboard_cache
    if now - cached_at < LEADERBOARD_CACHE_TTL and cached_list:
        return cached_list

    data = get(f"{DATA_URL}/v1/leaderboard", params={
        "limit":      top_n,
        "category":   "OVERALL",
        "timePeriod": "WEEK",
        "orderBy":    "PNL",
    })
    if not data:
        return cached_list  # return stale cache on failure rather than empty

    entries = data if isinstance(data, list) else data.get("data", [])
    wallets = []
    seen = set()
    for e in entries:
        addr = e.get("proxyWallet") or e.get("address") or e.get("user", "")
        if addr and _WALLET_RE.match(addr) and addr not in seen:
            wallets.append(addr)
            seen.add(addr)

    _leaderboard_cache = (now, wallets)
    log.info(f"Leaderboard refreshed: {len(wallets)} profitable wallets cached (ordered by PNL)")
    return wallets


def get_whale_signals(markets: list[dict]) -> list[dict]:
    """
    Collect all valid whale signals from open positions of profitable wallets.
    Returns a list of signal dicts (may be empty). Deduplicates by market_id.
    Path A: manually configured WHALE_WALLETS (override).
    Path B: top weekly profitable wallets from leaderboard (auto-discover).
    """
    market_ids = {m["id"]: m for m in markets}
    signals: list[dict] = []
    seen_markets: set[str] = set()

    def _make_signal(m, pos, wallet):
        price = m["yes_price"] if pos["direction"] == "yes" else m["no_price"]
        if price > CFG["MAX_TRADE_PRICE"]:
            log.debug(f"🐋 Skipping whale position (price {price:.3f} > MAX_TRADE_PRICE): {m['name'][:50]}")
            return None
        if price < CFG["MIN_TRADE_PRICE"]:
            log.debug(f"🐋 Skipping whale position (price {price:.3f} < MIN_TRADE_PRICE): {m['name'][:50]}")
            return None
        if CFG["MAX_WHALE_RESOLVE_HOURS"] > 0:
            end_date_str = m.get("end_date") or m.get("endDate") or m.get("endDateIso")
            if end_date_str:
                try:
                    end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                    hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                    if hours_left > CFG["MAX_WHALE_RESOLVE_HOURS"] or hours_left < 0:
                        log.debug(f"🐋 Skipping whale position ({hours_left:.0f}h left > MAX_WHALE_RESOLVE_HOURS): {m['name'][:50]}")
                        return None
                except ValueError:
                    pass  # can't parse date, allow it through
        return {
            "market":     m,
            "direction":  pos["direction"],
            "price":      price,
            "signal":     "whale-copy",
            "whale":      wallet,
            "whale_size": pos["size"],
            "notes":      f"Whale {wallet[:10]}… holds ${pos['size']:.0f}",
        }

    # ── Path A: configured wallets (manual override) ──────────────────────────
    if CFG["WHALE_WALLETS"]:
        for wallet in CFG["WHALE_WALLETS"][:15]:
            for pos in fetch_whale_positions(wallet, CFG["WHALE_MIN_SIZE"]):
                if pos["market_id"] in market_ids and pos["market_id"] not in seen_markets:
                    m = market_ids[pos["market_id"]]
                    sig = _make_signal(m, pos, wallet)
                    if sig:
                        log.info(
                            f"🐋 Whale position (manual): {wallet[:10]}… "
                            f"{pos['direction'].upper()} on '{m['name'][:50]}' holding ${pos['size']:.0f}"
                        )
                        signals.append(sig)
                        seen_markets.add(pos["market_id"])
            time.sleep(0.15)
        return signals

    # ── Path B: positions-based whale discovery ───────────────────────────────
    profitable = fetch_profitable_wallet_list(top_n=CFG["WHALE_LEADERBOARD_SIZE"])
    if not profitable:
        log.info("Leaderboard unavailable — skipping whale strategy this scan")
        return signals

    log.info(f"Scanning positions for {len(profitable)} top weekly wallets (min size=${CFG['WHALE_MIN_SIZE']:.0f})...")
    wallets = list(profitable)[:CFG["WHALE_WALLETS_PER_SCAN"]]
    total_positions = 0
    for wallet in wallets:
        positions = fetch_whale_positions(wallet, CFG["WHALE_MIN_SIZE"])
        total_positions += len(positions)
        for pos in positions:
            if pos["market_id"] in seen_markets:
                continue
            m = market_ids.get(pos["market_id"])
            if m is None:
                m = fetch_market_by_id(pos["market_id"])
                if m is None:
                    continue
            sig = _make_signal(m, pos, wallet)
            if sig:
                log.info(
                    f"🐋 Whale position: {wallet[:10]}… "
                    f"{pos['direction'].upper()} on '{m['name'][:50]}' @ {sig['price']:.3f} (holding ${pos['size']:.0f})"
                )
                signals.append(sig)
                seen_markets.add(pos["market_id"])
        time.sleep(0.15)
    log.info(f"Whale scan: {total_positions} open positions checked across {len(wallets)} wallets — {len(signals)} signal(s) found")
    return signals

# ── Google News — context for Haiku ──────────────────────────────────────────
_news_cache: dict[str, tuple[float, list[str]]] = {}
NEWS_CACHE_TTL = 3600  # seconds

def fetch_news_headlines(query: str, max_items: int = 5) -> list[str]:
    """Fetch recent headlines from Google News RSS. Cached for 1 hour."""
    now = time.time()
    # Evict expired entries to prevent unbounded growth
    expired = [k for k, (ts, _) in _news_cache.items() if now - ts >= NEWS_CACHE_TTL]
    for k in expired:
        del _news_cache[k]
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
  "reasoning": "one sentence citing the specific fact that justifies the edge",
  "order_type": "maker" | "taker"
}

Rules:
- Only say edge:true if you can cite a SPECIFIC piece of news or data showing the market is mispriced.
- "No clear signal", "market appears efficient", or "insufficient information" means edge:false, not edge:true.
- Do not have a directional bias — evaluate YES and NO equally based on evidence alone.
- Only say edge:true if confidence >= 0.60.
- Prefer "maker" orders (limit orders) to save on fees."""

def _haiku_system_shadow() -> str:
    threshold = CFG["SHADOW_HAIKU_MIN_CONF"]
    return f"""You are a prediction market analyst collecting training data. Evaluate every market honestly.

Reply ONLY with valid JSON — no prose, no markdown fences:
{{
  "edge": true | false,
  "direction": "yes" | "no",
  "confidence": 0.0-1.0,
  "reasoning": "one sentence citing the specific fact or signal behind your view",
  "order_type": "maker" | "taker"
}}

Rules:
- Say edge:true whenever confidence >= {threshold} — this is data collection, not live trading.
- Do not have a directional bias — evaluate YES and NO equally based on evidence alone.
- Always give your honest best-guess direction even when uncertain; we want to learn which signals work.
- Prefer "maker" orders (limit orders) to save on fees."""

def haiku_analyse(market: dict, shadow: bool = False) -> Optional[dict]:
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

    system = _haiku_system_shadow() if shadow else HAIKU_SYSTEM

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        # Strip any accidental markdown fences
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        result = json.loads(raw)
        # Validate response fields
        if result.get("direction") not in ("yes", "no"):
            log.warning(f"Haiku returned invalid direction: {result.get('direction')}")
            return None
        conf = result.get("confidence")
        if conf is not None:
            try:
                conf = max(0.0, min(1.0, float(conf)))
                result["confidence"] = conf
            except (ValueError, TypeError):
                result["confidence"] = 0.0
        if "edge" in result and not isinstance(result["edge"], bool):
            result["edge"] = str(result["edge"]).lower() == "true"
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
    whale_size: Optional[float] = None,
) -> Optional[float]:
    """
    Simulate placing a trade. Deducts from budget and writes to DB.
    Returns the new budget, or None if trade was skipped.
    """
    if open_trade_count(con) >= CFG["MAX_OPEN_TRADES"]:
        log.info("Max open trades reached — skipping")
        return None

    base_amount = round(budget * CFG["TRADE_SIZE_PCT"], 2)
    _, amount = price_scaled_params(price, CFG["HAIKU_MIN_CONF"], base_amount)
    amount = max(amount, CFG["MIN_TRADE_USDC"])

    if amount > budget * 0.95:
        log.warning(f"Insufficient budget (${budget:.2f}) for trade of ${amount:.2f}")
        return None

    fee = calc_fee(amount, price, order_type)
    new_budget = budget - amount - fee

    con.execute("""
        INSERT INTO trades
            (ts, market_id, market_name, token_id, direction, price, amount,
             fee, order_type, tags, notes, end_date, liquidity, volume_24h, market_slug, market_tags,
             whale_size)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        market.get("end_date"),
        market.get("liquidity"),
        market.get("volume_24h"),
        market.get("slug"),
        json.dumps(market.get("tags") or []),
        whale_size,
    ))
    con.commit()
    save_budget(new_budget)

    gross_if_win = amount * (1 - price) / price
    size_pct = round(amount / base_amount * 100) if base_amount > 0 else 100
    log.info(
        f"📝 Paper trade logged:\n"
        f"   Market:  {market['name'][:60]}\n"
        f"   {direction.upper()} @ {price:.3f}  |  ${amount:.2f} staked ({size_pct}% of base)\n"
        f"   Fee est: {fee:+.4f} USDC  |  Win payoff: +${gross_if_win:.2f}\n"
        f"   Tags:    {', '.join(tags)}\n"
        f"   Budget:  ${budget:.2f} → ${new_budget:.2f}"
    )
    return new_budget


# ── Live trade engine ─────────────────────────────────────────────────────────
def place_live_trade(
    con, budget: float,
    market: dict,
    direction: str,
    price: float,
    order_type: str,
    tags: list[str],
    notes: str = "",
    whale_size: Optional[float] = None,
) -> Optional[float]:
    """
    Place a real trade on Polymarket via the CLOB API.
    Returns the new budget, or None if trade was skipped/failed.
    """
    live_open = open_trade_count(con, mode_filter="live")
    if live_open >= CFG["LIVE_MAX_OPEN_TRADES"]:
        log.info(f"Max live open trades ({CFG['LIVE_MAX_OPEN_TRADES']}) reached — skipping")
        return None

    base_amount = round(budget * CFG["TRADE_SIZE_PCT"], 2)
    _, amount = price_scaled_params(price, CFG["HAIKU_MIN_CONF"], base_amount)
    amount = max(amount, CFG["MIN_TRADE_USDC"])
    amount = min(amount, CFG["MAX_LIVE_TRADE_USDC"])  # hard cap

    if amount > budget * 0.95:
        log.warning(f"Insufficient live budget (${budget:.2f}) for trade of ${amount:.2f}")
        return None

    # Resolve the correct token_id for the direction
    yes_tid, no_tid = resolve_token_ids(market)
    token_id = yes_tid if direction == "yes" else no_tid
    if not token_id:
        log.error(f"Cannot resolve {direction} token_id for market {market['id']} — skipping")
        return None

    fee = calc_fee(amount, price, order_type)
    new_budget = budget - amount - fee

    # Dry-run mode: log everything but don't call the API
    if CFG["LIVE_DRY_RUN"]:
        con.execute("""
            INSERT INTO trades
                (ts, market_id, market_name, token_id, direction, price, amount,
                 fee, order_type, tags, notes, mode,
                 end_date, liquidity, volume_24h, market_slug, market_tags,
                 whale_size)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            market["id"], market["name"], token_id,
            direction, price, amount, fee, order_type,
            json.dumps(tags), notes, "live-dry",
            market.get("end_date"), market.get("liquidity"), market.get("volume_24h"),
            market.get("slug"), json.dumps(market.get("tags") or []),
            whale_size,
        ))
        con.commit()
        save_live_budget(new_budget)
        size_pct = round(amount / base_amount * 100) if base_amount > 0 else 100
        log.info(
            f"🔸 DRY RUN — would place live trade:\n"
            f"   Market:   {market['name'][:60]}\n"
            f"   {direction.upper()} @ {price:.3f}  |  ${amount:.2f} ({size_pct}% of base)  |  token={token_id[:16]}…\n"
            f"   Budget:   ${budget:.2f} → ${new_budget:.2f}"
        )
        return new_budget

    # Real order placement
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        client = get_clob_client()
        side = BUY if direction == "yes" else SELL

        mo = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=side,
        )
        signed_order = client.create_market_order(mo)
        resp = client.post_order(signed_order, OrderType.FOK)

        # Extract order ID from response
        order_id = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
        elif hasattr(resp, "orderID"):
            order_id = resp.orderID

        size_pct = round(amount / base_amount * 100) if base_amount > 0 else 100
        log.info(
            f"💰 LIVE trade placed!\n"
            f"   Market:   {market['name'][:60]}\n"
            f"   {direction.upper()} @ {price:.3f}  |  ${amount:.2f} staked ({size_pct}% of base)\n"
            f"   Order ID: {order_id}\n"
            f"   Tags:     {', '.join(tags)}\n"
            f"   Budget:   ${budget:.2f} → ${new_budget:.2f}"
        )

    except Exception as e:
        log.error(f"LIVE order FAILED for {market['name'][:50]}: {e}")
        return None

    # Record in DB
    con.execute("""
        INSERT INTO trades
            (ts, market_id, market_name, token_id, direction, price, amount,
             fee, order_type, tags, notes, mode, order_id,
             end_date, liquidity, volume_24h, market_slug, market_tags,
             whale_size)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        market["id"], market["name"], token_id,
        direction, price, amount, fee, order_type,
        json.dumps(tags), notes, "live", order_id,
        market.get("end_date"), market.get("liquidity"), market.get("volume_24h"),
        market.get("slug"), json.dumps(market.get("tags") or []),
        whale_size,
    ))
    con.commit()
    save_live_budget(new_budget)
    return new_budget


# ── Shared resolution helper ──────────────────────────────────────────────────
def get_market_winner(market_id: str) -> Optional[str]:
    """
    Return 'yes', 'no', or None (still open / unclear).
    Numeric IDs → Gamma API. ConditionId hashes (0x...) → CLOB API.
    """
    is_hash = str(market_id).startswith("0x")

    if is_hash:
        data = get(f"{CLOB_URL}/markets/{market_id}")
        if not data or not isinstance(data, dict):
            return None
        if not data.get("closed"):
            return None
        tokens = data.get("tokens") or []
        for token in tokens:
            if token.get("winner"):
                outcome = str(token.get("outcome", "")).lower()
                if outcome in ("yes", "no"):
                    return outcome
                # Non-binary outcome (team name etc.) — infer from token index: first token = YES
                return "yes" if tokens.index(token) == 0 else "no"
        return None
    else:
        data = get(f"{GAMMA_URL}/markets/{market_id}")
        if not data:
            return None
        m = data[0] if isinstance(data, list) else data
        if not m.get("closed") and not m.get("resolved"):
            return None
        # Standard resolution fields
        if m.get("winner"):
            return str(m["winner"]).lower()
        if m.get("resolvedAt") or m.get("resolution"):
            res = str(m.get("resolution") or "").lower()
            return "yes" if res in ("1", "true", "yes") else "no"
        # outcomePrices fallback: ["1","0"] = YES won, ["0","1"] = NO won
        prices = m.get("outcomePrices")
        if prices and len(prices) >= 2:
            try:
                yes_p, no_p = float(prices[0]), float(prices[1])
                if yes_p >= 0.99:
                    return "yes"
                if no_p >= 0.99:
                    return "no"
            except (ValueError, TypeError):
                pass
        # CLOB fallback via conditionId
        condition_id = m.get("conditionId") or m.get("condition_id")
        if condition_id:
            clob_data = get(f"{CLOB_URL}/markets/{condition_id}")
            if clob_data and isinstance(clob_data, dict) and clob_data.get("closed"):
                clob_tokens = clob_data.get("tokens") or []
                for token in clob_tokens:
                    if token.get("winner"):
                        outcome = str(token.get("outcome", "")).lower()
                        if outcome in ("yes", "no"):
                            return outcome
                        return "yes" if clob_tokens.index(token) == 0 else "no"
        return None

# ── Auto-resolver: checks if open trades have settled ─────────────────────────
def resolve_settled_trades(con) -> float:
    """
    Check open trades against current market data.
    If a market is resolved, mark the trade and adjust budget.
    Returns total P&L from newly resolved trades.
    """
    rows = con.execute(
        "SELECT id, market_id, direction, price, amount, fee, ts, mode FROM trades WHERE resolved=0"
    ).fetchall()
    if not rows:
        return 0.0

    total_pnl = 0.0
    paper_budget = load_budget()
    live_budget  = load_live_budget() if os.path.exists(LIVE_BUDGET_FILE) else None
    paper_changed = False
    live_changed  = False
    now = datetime.now(timezone.utc)

    for row in rows:
        row_id, market_id, direction, price, amount, fee, ts = row[:7]
        trade_mode = row[7] if len(row) > 7 else "paper"
        # Warn about stale trades
        try:
            age = (now - datetime.fromisoformat(ts)).days
            if age > 14:
                log.warning(f"Trade #{row_id} has been open for {age} days — may need manual review")
        except (ValueError, TypeError):
            pass

        winner = get_market_winner(market_id)
        if not winner:
            continue

        won = (direction == winner)
        gross = amount * (1 - price) / price if won else -amount
        pnl = gross - fee

        con.execute("""
            UPDATE trades
            SET resolved=1, outcome=?, pnl=?, close_ts=?
            WHERE id=?
        """, (winner, round(pnl, 4), datetime.now(timezone.utc).isoformat(), row_id))
        con.commit()

        # Update the correct budget based on trade mode
        if trade_mode in ("live", "live-dry"):
            if live_budget is not None:
                live_budget += amount + fee + pnl
                live_changed = True
        else:
            paper_budget += amount + fee + pnl
            paper_changed = True
        total_pnl += pnl

        mode_tag = " [LIVE]" if trade_mode == "live" else ""
        status = "✅ WON" if won else "❌ LOST"
        log.info(
            f"{status}{mode_tag} — resolved trade #{row_id}: {direction.upper()} "
            f"on market {market_id[:20]}… | P&L: {pnl:+.2f} USDC"
        )
        time.sleep(0.3)

    if paper_changed:
        save_budget(paper_budget)
    if live_changed and live_budget is not None:
        save_live_budget(live_budget)
    return total_pnl

# ── Shadow trade engine ───────────────────────────────────────────────────────
def log_shadow_trade(con, strategy: str, market: dict, direction: str, price: float,
                     confidence: Optional[float] = None, notes: Optional[str] = None):
    """Record a hypothetical trade, deducting from the virtual shadow budget."""
    shadow_budget = load_shadow_budget()
    base_amount = round(shadow_budget * CFG["TRADE_SIZE_PCT"], 2)
    _, amount = price_scaled_params(price, CFG["SHADOW_HAIKU_MIN_CONF"], base_amount)
    amount = max(amount, CFG["MIN_TRADE_USDC"])

    if amount > shadow_budget * 0.95:
        log.info(f"Shadow budget too low (${shadow_budget:.2f}) — skipping shadow trade")
        return

    fee = calc_fee(amount, price)
    new_shadow_budget = shadow_budget - amount - fee

    con.execute("""
        INSERT INTO shadow_trades
            (ts, strategy, market_id, market_name, direction, price, amount, fee, confidence, notes,
             end_date, liquidity, volume_24h, market_slug, market_tags)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        datetime.now(timezone.utc).isoformat(),
        strategy,
        market["id"],
        market["name"],
        direction,
        price,
        amount,
        fee,
        confidence,
        notes,
        market.get("end_date"),
        market.get("liquidity"),
        market.get("volume_24h"),
        market.get("slug"),
        json.dumps(market.get("tags") or []),
    ))
    con.commit()
    save_shadow_budget(new_shadow_budget)

    size_pct = round(amount / base_amount * 100) if base_amount > 0 else 100
    conf_str = f" conf={confidence:.2f}" if confidence is not None else ""
    log.info(
        f"👻 Shadow [{strategy}]{conf_str}: {direction.upper()} on '{market['name'][:55]}' @ {price:.3f}"
        f"  |  ${amount:.2f} staked ({size_pct}%)  |  Shadow budget: ${shadow_budget:.2f} → ${new_shadow_budget:.2f}"
        + (f" | {notes[:80]}" if notes else "")
    )

def resolve_shadow_trades(con):
    """Check open shadow trades against market outcomes, record P&L, update shadow budget."""
    rows = con.execute(
        "SELECT id, market_id, direction, price, amount, fee, ts FROM shadow_trades WHERE resolved=0"
    ).fetchall()
    if not rows:
        return

    shadow_budget = load_shadow_budget()
    total_pnl = 0.0
    now = datetime.now(timezone.utc)

    for row_id, market_id, direction, price, amount, fee, ts in rows:
        try:
            age = (now - datetime.fromisoformat(ts)).days
            if age > 14:
                log.warning(f"Shadow trade #{row_id} has been open for {age} days — may need manual review")
        except (ValueError, TypeError):
            pass

        winner = get_market_winner(market_id)
        if not winner:
            continue

        won = (direction == winner)
        pnl = (amount * (1 - price) / price if won else -amount) - (fee or 0)

        con.execute("""
            UPDATE shadow_trades SET resolved=1, outcome=?, pnl=?, close_ts=? WHERE id=?
        """, (winner, round(pnl, 4), datetime.now(timezone.utc).isoformat(), row_id))
        con.commit()

        shadow_budget += amount + (fee or 0) + pnl
        total_pnl += pnl

        log.info(
            f"👻 Shadow resolved #{row_id}: {'WON' if won else 'LOST'} "
            f"P&L: {pnl:+.2f} USDC  |  Shadow budget: ${shadow_budget:.2f}"
        )
        time.sleep(0.2)

    if total_pnl != 0.0:
        save_shadow_budget(shadow_budget)

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
    pnl   = pnl   or 0.0
    fees  = fees  or 0.0
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

    # Shadow trade comparison
    shadow_rows = con.execute("""
        SELECT strategy,
               COUNT(*) as total,
               SUM(CASE WHEN resolved=1 THEN 1 ELSE 0 END) as resolved,
               SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
               ROUND(SUM(COALESCE(pnl, 0)), 2) as total_pnl
        FROM shadow_trades
        GROUP BY strategy
    """).fetchall()
    if shadow_rows:
        log.info("  Shadow strategy comparison (hypothetical, no real budget):")
        for strategy, total, resolved, wins, spnl in sorted(shadow_rows, key=lambda x: -(x[4] or 0)):
            wr = f"{wins/resolved*100:.0f}%" if resolved else "n/a"
            log.info(f"    [{strategy}]  {total} evaluated  wr={wr}  hypothetical P&L={spnl:+.2f}")

# ── Shared strategy helpers ────────────────────────────────────────────────────
_SPORTS_PATTERNS = ("Spread:", " vs. ", "O/U ", ": O/U")

def _is_sports_matchup(name: str) -> bool:
    """Return True if the market name looks like a live sports game or spread bet."""
    return any(p in name for p in _SPORTS_PATTERNS)


def _place_trade(con, budget, market, direction, price, order_type, tags, notes="", whale_size=None):
    """Dispatch to live or paper trade placement based on mode."""
    if CFG["STRATEGY_MODE"] == "live":
        return place_live_trade(con, budget, market, direction, price, order_type, tags, notes, whale_size)
    return place_paper_trade(con, budget, market, direction, price, order_type, tags, notes, whale_size)


def _run_haiku_slot(con, budget: float, markets: list[dict], traded: set,
                    shadow: bool = False) -> tuple[float, int, int]:
    """Run the standalone Haiku strategy. Returns (new_budget, signals, trades)."""
    signals = trades = 0
    scan_limit = CFG["MARKETS_PER_SCAN"]
    log.info(f"[haiku-analyse] Scanning top {min(scan_limit, len(markets))} markets...")
    for m in markets[:scan_limit]:
        if m["id"] in traded:
            continue
        if CFG["HAIKU_SKIP_SPORTS"] and _is_sports_matchup(m["name"]):
            log.debug(f"[haiku-analyse] Skipping sports market: {m['name'][:60]}")
            continue
        if not shadow and open_trade_count(con, mode_filter="live" if CFG["STRATEGY_MODE"] == "live" else None) >= (CFG["LIVE_MAX_OPEN_TRADES"] if CFG["STRATEGY_MODE"] == "live" else CFG["MAX_OPEN_TRADES"]):
            break
        analysis = haiku_analyse(m, shadow=shadow)
        time.sleep(0.5)
        if not analysis:
            continue
        conf  = float(analysis.get("confidence") or 0)
        dir_  = analysis.get("direction", "yes")
        price = m["yes_price"] if dir_ == "yes" else m["no_price"]
        if price < CFG["MIN_TRADE_PRICE"]:
            log.debug(f"[haiku-analyse] Skipping {m['name'][:50]}: price {price:.3f} < MIN_TRADE_PRICE {CFG['MIN_TRADE_PRICE']:.2f}")
            continue
        base_conf = CFG["SHADOW_HAIKU_MIN_CONF"] if shadow else CFG["HAIKU_MIN_CONF"]
        min_conf, _ = price_scaled_params(price, base_conf, 0)
        # In shadow mode bypass the edge gate — confidence threshold alone decides.
        # In real mode both edge=true AND confidence threshold must pass.
        if shadow:
            if conf < min_conf:
                continue
        else:
            if not analysis.get("edge") or conf < min_conf:
                continue
        signals += 1
        if shadow:
            log_shadow_trade(con, "haiku-analyse", m, dir_, price,
                             confidence=conf, notes=analysis.get("reasoning", ""))
        else:
            otype = analysis.get("order_type", "maker")
            new_budget = _place_trade(
                con, budget, m, dir_, price, otype,
                tags=["haiku-analyse"], notes=analysis.get("reasoning", ""),
            )
            if new_budget is not None:
                budget = new_budget
        trades += 1
        traded.add(m["id"])
        break  # one trade per slot
    return budget, signals, trades


def _run_whale_slot(con, budget: float, whale_signal: Optional[dict], traded: set,
                    shadow: bool = False) -> tuple[float, int, int]:
    """
    Execute a single whale-copy signal. No Haiku confirmation — whales trade on their own merit.
    Returns (new_budget, signals, trades).
    """
    if not whale_signal or whale_signal["market"]["id"] in traded:
        return budget, 0, 0

    m     = whale_signal["market"]
    dir_  = whale_signal["direction"]
    price = whale_signal["price"]

    log.info(f"[whale-copy] Copying whale signal: {dir_.upper()} on '{m['name'][:50]}' @ {price:.3f}")

    if shadow:
        log_shadow_trade(con, "whale-copy", m, dir_, price, confidence=None, notes=whale_signal["notes"])
        traded.add(m["id"])
        return budget, 1, 1

    new_budget = _place_trade(con, budget, m, dir_, price, "taker", ["whale-copy"], whale_signal["notes"], whale_signal.get("whale_size"))
    if new_budget is not None:
        traded.add(m["id"])
        return new_budget, 1, 1
    return budget, 1, 0


# ── Main scan loop ─────────────────────────────────────────────────────────────
def run_scan(con):
    if CFG["STRATEGY_MODE"] == "live":
        budget = load_live_budget()
    else:
        budget = load_budget()
    mode   = CFG["STRATEGY_MODE"]
    log.info(f"\n{'═'*55}\n  🔍 Starting scan  |  Budget: ${budget:.2f}  |  mode={mode}\n{'═'*55}")

    resolve_settled_trades(con)
    resolve_shadow_trades(con)

    if mode == "live":
        max_open = CFG["LIVE_MAX_OPEN_TRADES"]
        if open_trade_count(con, mode_filter="live") >= max_open:
            log.info(f"At max live open trades ({max_open}) — skipping new entries this scan")
            return
    elif mode != "shadow" and open_trade_count(con) >= CFG["MAX_OPEN_TRADES"]:
        log.info("At max open trades — skipping new entries this scan")
        return

    markets = fetch_markets(randomize=(mode == "shadow"))
    if not markets:
        log.warning("No markets returned — API may be down")
        return

    signals_found = 0
    trades_placed = 0
    # Pre-seed with markets that already have an open trade so we never
    # open a second position on the same market.
    if mode == "shadow":
        traded_this_scan: set = open_shadow_market_ids(con)
    else:
        traded_this_scan: set = open_market_ids(con)

    # Fetch all whale signals — use the full unsampled pool so whale positions
    # aren't limited to the random subset chosen for Haiku.
    whale_signals: list[dict] = []
    if CFG["ENABLE_WHALE_COPY"]:
        log.info("Checking whale wallets...")
        whale_markets = fetch_markets(randomize=False)  # full ranked pool, no random sampling
        whale_signals = get_whale_signals(whale_markets if whale_markets else markets)

    if mode == "shadow":
        # ── Shadow mode: log hypothetical trades for both strategies ──────────
        if CFG["ENABLE_HAIKU"]:
            _, s, t = _run_haiku_slot(con, budget, markets, traded_this_scan, shadow=True)
            signals_found += s; trades_placed += t

        if CFG["ENABLE_WHALE_COPY"]:
            whale_cap = CFG["MAX_WHALE_TRADES_PER_SCAN"]
            for sig in whale_signals[:whale_cap]:
                _, s, t = _run_whale_slot(con, budget, sig, traded_this_scan, shadow=True)
                signals_found += s; trades_placed += t

    else:
        # ── All real-trade modes (compete / parallel / live) ──────────────────
        # Whale signals run first and are independent of Haiku.
        # Both strategies share MAX_OPEN_TRADES as the hard cap.
        if CFG["ENABLE_WHALE_COPY"]:
            whale_cap = CFG["MAX_WHALE_TRADES_PER_SCAN"]
            for sig in whale_signals[:whale_cap]:
                budget, s, t = _run_whale_slot(con, budget, sig, traded_this_scan)
                signals_found += s; trades_placed += t

        if CFG["ENABLE_HAIKU"]:
            budget, s, t = _run_haiku_slot(con, budget, markets, traded_this_scan)
            signals_found += s; trades_placed += t

    con.execute(
        "INSERT INTO scan_log (ts, markets_checked, signals_found, trades_placed) VALUES (?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), len(markets), signals_found, trades_placed),
    )
    con.commit()
    log.info(f"Scan complete: {len(markets)} markets checked, {signals_found} signals, {trades_placed} trades placed")


# ── Entry point ────────────────────────────────────────────────────────────────
def _load_mode_budget() -> float:
    """Load the correct budget for the current strategy mode."""
    mode = CFG["STRATEGY_MODE"]
    if mode == "shadow":
        return load_shadow_budget()
    if mode == "live":
        return load_live_budget()
    return load_budget()


def main():
    mode = CFG["STRATEGY_MODE"]
    if mode == "live":
        log.info("━" * 55)
        log.info("  Polymarket Trading Bot  — LIVE MODE (REAL MONEY)")
        log.info("━" * 55)
        if CFG["LIVE_DRY_RUN"]:
            log.info("  Dry-run enabled — orders logged but NOT placed")
    else:
        log.info("━" * 55)
        log.info("  Polymarket Paper Trading Bot  (paper money only)")
        log.info("━" * 55)

    if not CFG["ANTHROPIC_API_KEY"] and CFG["ENABLE_HAIKU"]:
        log.error("ANTHROPIC_API_KEY not set. Run: export ANTHROPIC_API_KEY='sk-ant-...'")
        sys.exit(1)

    # Validate config bounds
    errors = []
    if not (0 < CFG["TRADE_SIZE_PCT"] < 1):
        errors.append(f"TRADE_SIZE_PCT must be between 0 and 1, got {CFG['TRADE_SIZE_PCT']}")
    if not (0 < CFG["MAX_TRADE_PRICE"] < 1):
        errors.append(f"MAX_TRADE_PRICE must be between 0 and 1, got {CFG['MAX_TRADE_PRICE']}")
    if CFG["STARTING_BUDGET"] <= 0:
        errors.append(f"STARTING_BUDGET must be positive, got {CFG['STARTING_BUDGET']}")
    if CFG["SCAN_INTERVAL"] < 60:
        errors.append(f"SCAN_INTERVAL must be >= 60 seconds, got {CFG['SCAN_INTERVAL']}")
    if CFG["STRATEGY_MODE"] not in ("compete", "parallel", "shadow", "live"):
        errors.append(f"STRATEGY_MODE must be compete|parallel|shadow|live, got '{CFG['STRATEGY_MODE']}'")
    if errors:
        for e in errors:
            log.error(f"Config error: {e}")
        sys.exit(1)

    # Live trading pre-flight
    if mode == "live":
        try:
            validate_live_trading_setup()
        except RuntimeError as e:
            log.error(f"Live trading setup failed: {e}")
            sys.exit(1)

    con = init_db()
    try:
        budget = _load_mode_budget()
        log.info(f"Starting budget: ${budget:.2f} USDC")
        log.info(f"Strategies: haiku={CFG['ENABLE_HAIKU']}, whale_copy={CFG['ENABLE_WHALE_COPY']}, mode={mode}")
        max_open = CFG["LIVE_MAX_OPEN_TRADES"] if mode == "live" else CFG["MAX_OPEN_TRADES"]
        interval_display = CFG["SHADOW_SCAN_INTERVAL"] if mode == "shadow" else CFG["SCAN_INTERVAL"]
        log.info(f"Scan interval: {interval_display}s  |  Max open trades: {max_open}")
        log.info(f"Trade size: {CFG['TRADE_SIZE_PCT']*100:.0f}% of budget per trade (~${budget*CFG['TRADE_SIZE_PCT']:.2f})")
        if mode == "live":
            log.info(f"Live hard cap: ${CFG['MAX_LIVE_TRADE_USDC']:.2f} per trade  |  Dry run: {CFG['LIVE_DRY_RUN']}")

        scan_count = 0
        while True:
            try:
                # Check if budget is too low to place trades
                current_budget = _load_mode_budget()
                trade_amount = current_budget * CFG["TRADE_SIZE_PCT"]

                if trade_amount < CFG["MIN_TRADE_USDC"]:
                    cooldown = CFG["LOW_BUDGET_COOLDOWN"]
                    interval = CFG["SHADOW_SCAN_INTERVAL"] if mode == "shadow" else CFG["SCAN_INTERVAL"]
                    log.info(
                        f"Budget too low for new trades (${current_budget:.2f}, "
                        f"trade size ${trade_amount:.2f} < ${CFG['MIN_TRADE_USDC']:.2f}) "
                        f"— cooldown for {cooldown}s, resolving every {interval}s"
                    )
                    elapsed = 0
                    while elapsed < cooldown:
                        resolve_settled_trades(con)
                        resolve_shadow_trades(con)
                        current_budget = _load_mode_budget()
                        if current_budget * CFG["TRADE_SIZE_PCT"] >= CFG["MIN_TRADE_USDC"]:
                            log.info(f"Budget recovered to ${current_budget:.2f} — resuming scans")
                            break
                        time.sleep(interval)
                        elapsed += interval
                    continue

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

            interval = CFG["SHADOW_SCAN_INTERVAL"] if mode == "shadow" else CFG["SCAN_INTERVAL"]
            log.info(f"Sleeping {interval}s until next scan...")
            time.sleep(interval)
    finally:
        con.close()


if __name__ == "__main__":
    main()
