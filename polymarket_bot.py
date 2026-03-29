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
    "STARTING_BUDGET":    float(os.getenv("STARTING_BUDGET",  "500")),
    "TRADE_SIZE_PCT":     float(os.getenv("TRADE_SIZE_PCT",   "0.03")),  # 3% of budget per trade
    "MIN_TRADE_USDC":     float(os.getenv("MIN_TRADE_USDC",   "5")),

    # Market filters
    "MIN_LIQUIDITY":      float(os.getenv("MIN_LIQUIDITY",    "2000")),  # skip thin markets
    "MAX_TRADE_PRICE":    float(os.getenv("MAX_TRADE_PRICE",  "0.85")),  # skip near-certain outcomes; forces yes/no into [0.15, 0.85]
    "MAX_RESOLVE_HOURS":  float(os.getenv("MAX_RESOLVE_HOURS", "0")),    # 0 = no limit; set e.g. 24 for same-day markets only
    "MARKET_POOL_SIZE":   int(os.getenv("MARKET_POOL_SIZE",   "100")),   # fetch this many, then rank and pick best
    "MARKETS_PER_SCAN":   int(os.getenv("MARKETS_PER_SCAN",   "20")),    # top N to send to Haiku after ranking

    # Strategies
    "ENABLE_HAIKU":           os.getenv("ENABLE_HAIKU",       "true").lower() == "true",
    "ENABLE_WHALE_COPY":      os.getenv("ENABLE_WHALE_COPY",  "true").lower() == "true",
    "HAIKU_MIN_CONF":         float(os.getenv("HAIKU_MIN_CONF",        "0.65")),  # confidence threshold for real trades
    "SHADOW_HAIKU_MIN_CONF":  float(os.getenv("SHADOW_HAIKU_MIN_CONF", "0.35")),  # lower threshold for shadow data collection
    "HAIKU_SKIP_SPORTS":      os.getenv("HAIKU_SKIP_SPORTS", "true").lower() == "true",  # block game matchups/spreads
    "WHALE_MIN_SIZE":         float(os.getenv("WHALE_MIN_SIZE",   "500")),   # min USD for whale trade

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
    con.execute("""
        CREATE TABLE IF NOT EXISTS shadow_trades (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          TEXT    NOT NULL,
            strategy    TEXT    NOT NULL,   -- haiku-analyse | whale-copy | whale+haiku
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
            close_ts    TEXT
        )
    """)
    # Migrate existing shadow_trades table if columns are missing
    for col, definition in [("confidence", "REAL"), ("notes", "TEXT"), ("fee", "REAL")]:
        try:
            con.execute(f"ALTER TABLE shadow_trades ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # column already exists
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

def open_trade_count(con) -> int:
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
            token_id = tokens[0] if tokens else m.get("conditionId")
            volume = float(m.get("volume24hr") or m.get("volume") or 0)

            pool.append({
                "id":         m.get("id") or m.get("conditionId", ""),
                "name":       m.get("question") or m.get("title", "Unknown"),
                "yes_price":  yes_price,
                "no_price":   round(1 - yes_price, 4),
                "liquidity":  liquidity,
                "volume_24h": volume,
                "token_id":   token_id,
                "end_date":   m.get("endDate") or m.get("endDateIso"),
                "slug":       m.get("slug", ""),
                "tags":       m.get("tags") or [],
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
        if not yes_token:
            return None
        yes_price = float(yes_token.get("price") or 0)
        if yes_price <= 0:
            return None
        token_id = yes_token.get("token_id")
        return {
            "id":         condition_id,
            "name":       data.get("question", "Unknown"),
            "yes_price":  yes_price,
            "no_price":   round(1 - yes_price, 4),
            "liquidity":  0.0,   # not available from CLOB endpoint
            "volume_24h": 0.0,
            "token_id":   token_id,
            "end_date":   data.get("end_date_iso"),
            "slug":       data.get("market_slug", ""),
            "tags":       data.get("tags") or [],
        }
    except (TypeError, ValueError, KeyError):
        return None


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
_leaderboard_cache: tuple[float, set[str]] = (0.0, set())
LEADERBOARD_CACHE_TTL = 3600  # seconds

def fetch_profitable_wallet_set(top_n: int = 30) -> set[str]:
    """Return a set of historically profitable wallet addresses, cached for 1 hour."""
    global _leaderboard_cache
    now = time.time()
    cached_at, cached_set = _leaderboard_cache
    if now - cached_at < LEADERBOARD_CACHE_TTL and cached_set:
        return cached_set

    data = get(f"{DATA_URL}/v1/leaderboard", params={
        "limit":      top_n,
        "category":   "OVERALL",
        "timePeriod": "WEEK",
        "orderBy":    "PNL",
    })
    if not data:
        return cached_set  # return stale cache on failure rather than empty

    entries = data if isinstance(data, list) else data.get("data", [])
    wallets = set()
    for e in entries:
        addr = e.get("proxyWallet") or e.get("address") or e.get("user", "")
        if addr and _WALLET_RE.match(addr):
            wallets.add(addr)

    _leaderboard_cache = (now, wallets)
    log.info(f"Leaderboard refreshed: {len(wallets)} profitable wallets cached")
    return wallets


def get_whale_signal(markets: list[dict]) -> Optional[dict]:
    """
    Find a signal by checking current open positions of profitable wallets.
    Uses /positions endpoint — more reliable than scanning recent trades.
    Path A: manually configured WHALE_WALLETS (override).
    Path B: top weekly profitable wallets from leaderboard (auto-discover).
    """
    market_ids = {m["id"]: m for m in markets}

    # ── Path A: configured wallets (manual override) ──────────────────────────
    if CFG["WHALE_WALLETS"]:
        for wallet in CFG["WHALE_WALLETS"][:15]:
            for pos in fetch_whale_positions(wallet, CFG["WHALE_MIN_SIZE"]):
                if pos["market_id"] in market_ids:
                    m = market_ids[pos["market_id"]]
                    log.info(
                        f"🐋 Whale position (manual): {wallet[:10]}… "
                        f"{pos['direction'].upper()} on '{m['name'][:50]}' holding ${pos['size']:.0f}"
                    )
                    return {
                        "market":     m,
                        "direction":  pos["direction"],
                        "price":      m["yes_price"] if pos["direction"] == "yes" else m["no_price"],
                        "signal":     "whale-copy",
                        "whale":      wallet,
                        "whale_size": pos["size"],
                        "notes":      f"Whale {wallet[:10]}… holds ${pos['size']:.0f}",
                    }
            time.sleep(0.15)
        return None

    # ── Path B: positions-based whale discovery ───────────────────────────────
    # Fetch top weekly profitable wallets, then check what each currently holds.
    # Positions are current open bets — more reliable than scanning recent trades.
    profitable = fetch_profitable_wallet_set(top_n=30)
    if not profitable:
        log.info("Leaderboard unavailable — skipping whale strategy this scan")
        return None

    log.info(f"Scanning positions for {len(profitable)} top weekly wallets (min size=${CFG['WHALE_MIN_SIZE']:.0f})...")
    wallets = list(profitable)[:15]  # cap API calls at 15 wallets per scan
    total_positions = 0
    for wallet in wallets:
        positions = fetch_whale_positions(wallet, CFG["WHALE_MIN_SIZE"])
        total_positions += len(positions)
        for pos in positions:
            # Try pre-filtered list first; if not found, look up the market directly
            m = market_ids.get(pos["market_id"])
            if m is None:
                m = fetch_market_by_id(pos["market_id"])
                if m is None:
                    continue
            signal_price = m["yes_price"] if pos["direction"] == "yes" else m["no_price"]
            if signal_price > CFG["MAX_TRADE_PRICE"]:
                log.debug(
                    f"🐋 Skipping whale position (price {signal_price:.3f} > MAX_TRADE_PRICE): "
                    f"{m['name'][:50]}"
                )
                continue
            log.info(
                f"🐋 Whale position: {wallet[:10]}… "
                f"{pos['direction'].upper()} on '{m['name'][:50]}' @ {signal_price:.3f} (holding ${pos['size']:.0f})"
            )
            return {
                "market":     m,
                "direction":  pos["direction"],
                "price":      signal_price,
                "signal":     "whale-copy",
                "whale":      wallet,
                "whale_size": pos["size"],
                "notes":      f"Whale {wallet[:10]}… holds ${pos['size']:.0f}",
            }
        time.sleep(0.15)
    log.info(f"Whale scan: {total_positions} open positions checked across {len(wallets)} wallets — no signal found")
    return None

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
    new_budget = budget - amount - fee

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
        for token in data.get("tokens") or []:
            if token.get("winner"):
                return str(token.get("outcome", "")).lower()
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
                for token in clob_data.get("tokens") or []:
                    if token.get("winner"):
                        return str(token.get("outcome", "")).lower()
        return None

# ── Auto-resolver: checks if open trades have settled ─────────────────────────
def resolve_settled_trades(con) -> float:
    """
    Check open trades against current market data.
    If a market is resolved, mark the trade and adjust budget.
    Returns total P&L from newly resolved trades.
    """
    rows = con.execute(
        "SELECT id, market_id, direction, price, amount, fee, ts FROM trades WHERE resolved=0"
    ).fetchall()
    if not rows:
        return 0.0

    total_pnl = 0.0
    budget = load_budget()
    now = datetime.now(timezone.utc)

    for row_id, market_id, direction, price, amount, fee, ts in rows:
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

        budget += amount + fee + pnl
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

# ── Shadow trade engine ───────────────────────────────────────────────────────
def log_shadow_trade(con, strategy: str, market: dict, direction: str, price: float,
                     confidence: Optional[float] = None, notes: Optional[str] = None):
    """Record a hypothetical trade with no budget impact."""
    amount = round(CFG["STARTING_BUDGET"] * CFG["TRADE_SIZE_PCT"], 2)
    fee = calc_fee(amount, price)
    con.execute("""
        INSERT INTO shadow_trades
            (ts, strategy, market_id, market_name, direction, price, amount, fee, confidence, notes)
        VALUES (?,?,?,?,?,?,?,?,?,?)
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
    ))
    con.commit()
    conf_str = f" conf={confidence:.2f}" if confidence is not None else ""
    log.info(
        f"👻 Shadow [{strategy}]{conf_str}: {direction.upper()} on '{market['name'][:55]}' @ {price:.3f}"
        + (f" | {notes[:80]}" if notes else "")
    )

def resolve_shadow_trades(con):
    """Check open shadow trades against market outcomes and record hypothetical P&L."""
    rows = con.execute(
        "SELECT id, market_id, direction, price, amount, fee, ts FROM shadow_trades WHERE resolved=0"
    ).fetchall()
    if not rows:
        return

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
        log.info(
            f"👻 Shadow resolved #{row_id}: {'WON' if won else 'LOST'} "
            f"hypothetical P&L: {pnl:+.2f} USDC"
        )
        time.sleep(0.2)

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
        if not shadow and open_trade_count(con) >= CFG["MAX_OPEN_TRADES"]:
            break
        analysis = haiku_analyse(m, shadow=shadow)
        time.sleep(0.5)
        if not analysis:
            continue
        conf      = float(analysis.get("confidence") or 0)
        min_conf  = CFG["SHADOW_HAIKU_MIN_CONF"] if shadow else CFG["HAIKU_MIN_CONF"]
        # In shadow mode bypass the edge gate — confidence threshold alone decides.
        # In real mode both edge=true AND confidence threshold must pass.
        if shadow:
            if conf < min_conf:
                continue
        else:
            if not analysis.get("edge") or conf < min_conf:
                continue
        signals += 1
        dir_  = analysis.get("direction", "yes")
        price = m["yes_price"] if dir_ == "yes" else m["no_price"]
        if shadow:
            log_shadow_trade(con, "haiku-analyse", m, dir_, price,
                             confidence=conf, notes=analysis.get("reasoning", ""))
        else:
            otype = analysis.get("order_type", "maker")
            new_budget = place_paper_trade(
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
                    confirm_with_haiku: bool, shadow: bool = False) -> tuple[float, int, int]:
    """
    Run a whale-based strategy slot.
    confirm_with_haiku=False → pure whale-copy
    confirm_with_haiku=True  → whale+haiku
    Returns (new_budget, signals, trades).
    """
    if not whale_signal or whale_signal["market"]["id"] in traded:
        return budget, 0, 0

    m     = whale_signal["market"]
    dir_  = whale_signal["direction"]
    price = whale_signal["price"]
    otype = "taker"
    tags  = ["whale-copy"]
    notes = whale_signal["notes"]

    if confirm_with_haiku:
        log.info("[whale+haiku] Confirming whale signal with Haiku...")
        analysis = haiku_analyse(m)
        if not analysis:
            return budget, 1, 0
        if not analysis.get("edge") or analysis.get("confidence", 0) < CFG["HAIKU_MIN_CONF"]:
            log.info("[whale+haiku] Haiku rejected signal — skipping")
            return budget, 1, 0
        tags  = ["whale+haiku"]
        otype = analysis.get("order_type", "taker")
        notes += f" | Haiku conf={analysis['confidence']:.2f}: {analysis.get('reasoning','')}"
    else:
        log.info("[whale-copy] Copying whale signal directly...")

    if shadow:
        conf = float(analysis.get("confidence") or 0) if confirm_with_haiku and analysis else None
        log_shadow_trade(con, tags[0], m, dir_, price, confidence=conf, notes=notes)
        traded.add(m["id"])
        return budget, 1, 1

    new_budget = place_paper_trade(con, budget, m, dir_, price, otype, tags, notes)
    if new_budget is not None:
        traded.add(m["id"])
        return new_budget, 1, 1
    return budget, 1, 0


# ── Main scan loop ─────────────────────────────────────────────────────────────
def run_scan(con):
    budget = load_budget()
    mode   = CFG["STRATEGY_MODE"]
    log.info(f"\n{'═'*55}\n  🔍 Starting scan  |  Budget: ${budget:.2f}  |  mode={mode}\n{'═'*55}")

    resolve_settled_trades(con)
    resolve_shadow_trades(con)

    if mode != "shadow" and open_trade_count(con) >= CFG["MAX_OPEN_TRADES"]:
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

    # Fetch whale signal once — shared by both whale slots.
    # Use the full unsampled pool so whale positions aren't limited to the
    # random 20 markets chosen for Haiku.
    whale_signal = None
    if CFG["ENABLE_WHALE_COPY"]:
        log.info("Checking whale wallets...")
        whale_markets = fetch_markets(randomize=False)  # full ranked pool, no random sampling
        whale_signal = get_whale_signal(whale_markets if whale_markets else markets)

    if mode == "shadow":
        # ── Shadow mode: evaluate all 3 strategies, log hypothetical trades ──
        # No budget is spent. Use results to compare strategies over time.
        # traded_this_scan is seeded with already-open shadow trades so we don't
        # double-enter the same position across scans.
        # whale+haiku uses the pre-scan snapshot so it can still compare against
        # the same market whale-copy just traded this scan (useful A/B data).
        pre_scan_traded = set(traded_this_scan)

        if CFG["ENABLE_HAIKU"]:
            _, s, t = _run_haiku_slot(con, budget, markets, traded_this_scan, shadow=True)
            signals_found += s; trades_placed += t

        if CFG["ENABLE_WHALE_COPY"]:
            _, s, t = _run_whale_slot(con, budget, whale_signal, traded_this_scan,
                                      confirm_with_haiku=False, shadow=True)
            signals_found += s; trades_placed += t

        if CFG["ENABLE_WHALE_COPY"] and CFG["ENABLE_HAIKU"]:
            _, s, t = _run_whale_slot(con, budget, whale_signal, pre_scan_traded,
                                      confirm_with_haiku=True, shadow=True)
            signals_found += s; trades_placed += t

    elif mode == "parallel":
        # ── Parallel mode: each strategy places real trades independently ─────
        if CFG["ENABLE_HAIKU"]:
            budget, s, t = _run_haiku_slot(con, budget, markets, traded_this_scan)
            signals_found += s; trades_placed += t

        if CFG["ENABLE_WHALE_COPY"]:
            budget, s, t = _run_whale_slot(con, budget, whale_signal, traded_this_scan,
                                           confirm_with_haiku=False)
            signals_found += s; trades_placed += t

        if CFG["ENABLE_WHALE_COPY"] and CFG["ENABLE_HAIKU"]:
            budget, s, t = _run_whale_slot(con, budget, whale_signal, set(),
                                           confirm_with_haiku=True)
            signals_found += s; trades_placed += t

    else:
        # ── Compete mode (default): strategies share 3 trade slots ────────────
        if CFG["ENABLE_WHALE_COPY"] and whale_signal:
            budget, s, t = _run_whale_slot(
                con, budget, whale_signal, traded_this_scan,
                confirm_with_haiku=CFG["ENABLE_HAIKU"],
            )
            signals_found += s; trades_placed += t

        if CFG["ENABLE_HAIKU"] and trades_placed < 3:
            budget, s, t = _run_haiku_slot(con, budget, markets, traded_this_scan)
            signals_found += s; trades_placed += t

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
    if CFG["STRATEGY_MODE"] not in ("compete", "parallel", "shadow"):
        errors.append(f"STRATEGY_MODE must be compete|parallel|shadow, got '{CFG['STRATEGY_MODE']}'")
    if errors:
        for e in errors:
            log.error(f"Config error: {e}")
        sys.exit(1)

    con = init_db()
    try:
        budget = load_budget()
        log.info(f"Starting budget: ${budget:.2f} USDC")
        log.info(f"Strategies: haiku={CFG['ENABLE_HAIKU']}, whale_copy={CFG['ENABLE_WHALE_COPY']}, mode={CFG['STRATEGY_MODE']}")
        interval_display = CFG["SHADOW_SCAN_INTERVAL"] if CFG["STRATEGY_MODE"] == "shadow" else CFG["SCAN_INTERVAL"]
        log.info(f"Scan interval: {interval_display}s  |  Max open trades: {CFG['MAX_OPEN_TRADES']}")
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

            interval = CFG["SHADOW_SCAN_INTERVAL"] if CFG["STRATEGY_MODE"] == "shadow" else CFG["SCAN_INTERVAL"]
            log.info(f"Sleeping {interval}s until next scan...")
            time.sleep(interval)
    finally:
        con.close()


if __name__ == "__main__":
    main()
