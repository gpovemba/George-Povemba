"""
POLYMARKET BACKTESTER
=============================================================================
Foundation for backtesting strategies against historical Polymarket data.

WHAT IT DOES:
  1. Fetches closed markets from Polymarket (filterable by slug pattern,
     category, date range, volume, etc.)
  2. Pulls full price history for each market token from the CLOB API
  3. Structures everything into a clean list of MarketData objects
  4. Runs a strategy function over each market and aggregates results

HOW TO USE:
  1. Define a strategy function with the signature:
       def my_strategy(market: MarketData) -> Trade | None
     Return a Trade if you'd enter, or None to skip.

  2. Pass it to run_backtest():
       results = run_backtest(markets, strategy=my_strategy)
       print_results(results)

  3. Use fetch_markets() to pull the data you want to test against.

BUILT-IN EXAMPLE STRATEGY:
  buzzer_beater_strategy — mirrors the live BTC buzzer beater bot logic
  (enter in last 90s if favored side ask >= threshold)

=============================================================================
"""

import os
import time
import json
import csv
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional, Callable

# =============================================================================
# CONFIG
# =============================================================================
GAMMA_API        = "https://gamma-api.polymarket.com"
CLOB_API         = "https://clob.polymarket.com"
REQUEST_TIMEOUT  = 10
RATE_LIMIT_SLEEP = 0.15  # seconds between API calls to avoid throttling
CACHE_DIR        = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_cache")

ET = timezone(timedelta(hours=-5))


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class PricePoint:
    timestamp: int    # unix seconds
    price: float      # 0.0 – 1.0


@dataclass
class TokenHistory:
    token_id: str
    outcome: str      # e.g. "Up", "Down", "Yes", "No"
    history: list     # list of PricePoint
    resolved_price: float = 0.0   # 1.0 if won, 0.0 if lost


@dataclass
class MarketData:
    market_id: str
    slug: str
    question: str
    start_time: int       # unix seconds (market open)
    end_time: int         # unix seconds (market close)
    volume: float
    outcomes: list        # ["Up", "Down"] etc.
    tokens: list          # list of TokenHistory
    neg_risk: bool = False
    resolved_outcome: Optional[str] = None   # winning outcome label


@dataclass
class Trade:
    market_slug: str
    outcome: str          # which side we bet on
    entry_time: int       # unix seconds
    entry_price: float
    shares: float
    pnl: float = 0.0
    won: bool = False


@dataclass
class BacktestResults:
    trades: list = field(default_factory=list)
    markets_seen: int = 0
    markets_entered: int = 0
    markets_skipped: int = 0

    @property
    def wins(self):
        return sum(1 for t in self.trades if t.won)

    @property
    def losses(self):
        return sum(1 for t in self.trades if not t.won)

    @property
    def win_rate(self):
        return self.wins / len(self.trades) if self.trades else 0.0

    @property
    def total_pnl(self):
        return sum(t.pnl for t in self.trades)

    @property
    def total_wagered(self):
        return sum(t.entry_price * t.shares for t in self.trades)

    @property
    def roi(self):
        return self.total_pnl / self.total_wagered if self.total_wagered else 0.0


# =============================================================================
# CACHE HELPERS
# =============================================================================

def _cache_path(slug: str) -> str:
    os.makedirs(CACHE_DIR, exist_ok=True)
    return os.path.join(CACHE_DIR, f"{slug}.json")


def _load_cache(slug: str) -> Optional[dict]:
    path = _cache_path(slug)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None


def _save_cache(slug: str, data: dict):
    with open(_cache_path(slug), "w") as f:
        json.dump(data, f)


# =============================================================================
# API HELPERS
# =============================================================================

def _get(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 502, 503, 504):
                time.sleep(2 ** attempt)
                continue
        except Exception:
            time.sleep(2 ** attempt)
    return None


def fetch_price_history(token_id: str) -> list:
    """Returns list of PricePoint for a token, sorted by timestamp."""
    data = _get(f"{CLOB_API}/prices-history", params={
        "market": token_id,
        "interval": "max",
        "fidelity": 1,
    })
    if not data or "history" not in data:
        return []
    return [PricePoint(p["t"], p["p"]) for p in data["history"]]


def _parse_market_raw(m: dict) -> Optional[MarketData]:
    """Parse a raw Gamma API market dict into a MarketData (no price history)."""
    try:
        outcomes  = json.loads(m["outcomes"])  if isinstance(m["outcomes"],  str) else m["outcomes"]
        token_ids = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds",""), str) else []
        prices    = json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices",""), str) else []

        if not token_ids or len(token_ids) < len(outcomes):
            return None

        resolved = None
        for outcome, price in zip(outcomes, prices):
            if float(price) == 1.0:
                resolved = outcome
                break

        closed_time  = m.get("closedTime") or m.get("endDate") or ""
        created_time = m.get("createdAt", "")
        try:
            end_ts = int(datetime.fromisoformat(closed_time.replace("Z", "+00:00")).timestamp())
        except Exception:
            end_ts = 0
        try:
            start_ts = int(datetime.fromisoformat(created_time.replace("Z", "+00:00")).timestamp())
        except Exception:
            start_ts = 0

        return MarketData(
            market_id=m["id"],
            slug=m["slug"],
            question=m["question"],
            start_time=start_ts,
            end_time=end_ts,
            volume=float(m.get("volumeNum", 0) or 0),
            outcomes=outcomes,
            tokens=[],  # filled later
            neg_risk=m.get("negRisk", False),
            resolved_outcome=resolved,
        ), token_ids, prices
    except Exception:
        return None


def fetch_btc_5m_markets(
    days: int = 30,
    end_date: Optional[str] = None,
    use_cache: bool = True,
) -> list:
    """
    Fetch BTC 5-min markets for the past `days` days by generating expected
    slugs directly (btc-updown-5m-{timestamp}) rather than paginating all markets.
    Caches each market locally so re-runs are instant.

    Args:
        days:       How many days back to fetch (default 30)
        end_date:   End date string "YYYY-MM-DD" (default: today)
        use_cache:  Load from local cache if available (default True)
    """
    MARKET_DURATION = 300  # 5 minutes

    if end_date:
        end_ts = int(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc).timestamp())
    else:
        end_ts = int(time.time())

    start_ts = end_ts - (days * 86400)

    # Align to market grid
    start_ts = (start_ts // MARKET_DURATION) * MARKET_DURATION
    end_ts   = (end_ts   // MARKET_DURATION) * MARKET_DURATION

    total = (end_ts - start_ts) // MARKET_DURATION
    print(f"🔍 Targeting {total} BTC 5-min markets over {days} days")
    print(f"   {datetime.fromtimestamp(start_ts, tz=ET).strftime('%Y-%m-%d')} → "
          f"{datetime.fromtimestamp(end_ts, tz=ET).strftime('%Y-%m-%d')}\n")

    markets = []
    cached_count = 0
    fetched_count = 0
    skipped_count = 0

    timestamps = range(start_ts, end_ts, MARKET_DURATION)
    for i, ts in enumerate(timestamps):
        slug = f"btc-updown-5m-{ts}"

        # Try cache first
        if use_cache:
            cached = _load_cache(slug)
            if cached:
                market = MarketData(
                    market_id=cached["market_id"],
                    slug=cached["slug"],
                    question=cached["question"],
                    start_time=cached["start_time"],
                    end_time=cached["end_time"],
                    volume=cached["volume"],
                    outcomes=cached["outcomes"],
                    neg_risk=cached["neg_risk"],
                    resolved_outcome=cached["resolved_outcome"],
                    tokens=[
                        TokenHistory(
                            token_id=t["token_id"],
                            outcome=t["outcome"],
                            history=[PricePoint(p["t"], p["p"]) for p in t["history"]],
                            resolved_price=t["resolved_price"],
                        )
                        for t in cached["tokens"]
                    ],
                )
                markets.append(market)
                cached_count += 1
                continue

        # Fetch from API
        raw = _get(f"{GAMMA_API}/markets", params={"slug": slug, "closed": "true"})
        if not raw:
            skipped_count += 1
            continue

        parsed = _parse_market_raw(raw[0])
        if not parsed:
            skipped_count += 1
            continue

        market, token_ids, prices = parsed

        # Fetch price history for each token
        tokens = []
        for outcome, token_id, res_price in zip(market.outcomes, token_ids, prices):
            time.sleep(RATE_LIMIT_SLEEP)
            history = fetch_price_history(token_id)
            tokens.append(TokenHistory(
                token_id=token_id,
                outcome=outcome,
                history=history,
                resolved_price=float(res_price),
            ))
        market.tokens = tokens
        fetched_count += 1

        # Save to cache
        if use_cache:
            _save_cache(slug, {
                "market_id": market.market_id,
                "slug": market.slug,
                "question": market.question,
                "start_time": market.start_time,
                "end_time": market.end_time,
                "volume": market.volume,
                "outcomes": market.outcomes,
                "neg_risk": market.neg_risk,
                "resolved_outcome": market.resolved_outcome,
                "tokens": [
                    {
                        "token_id": t.token_id,
                        "outcome": t.outcome,
                        "history": [{"t": p.timestamp, "p": p.price} for p in t.history],
                        "resolved_price": t.resolved_price,
                    }
                    for t in tokens
                ],
            })

        markets.append(market)

        if (i + 1) % 50 == 0:
            print(f"  [{i+1}/{total}] fetched={fetched_count} cached={cached_count} skipped={skipped_count}")
        time.sleep(RATE_LIMIT_SLEEP)

    print(f"\n✅ Loaded {len(markets)} markets  (fetched={fetched_count}, cached={cached_count}, skipped={skipped_count})")
    return markets


def fetch_markets(
    slug_contains: Optional[str] = None,
    closed: bool = True,
    min_volume: float = 0.0,
    limit: int = 100,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    use_cache: bool = True,
) -> list:
    """
    Generic market fetcher. Paginates Gamma API and filters client-side.
    For BTC 5-min markets, prefer fetch_btc_5m_markets() — it's much faster.
    """
    params = {"closed": str(closed).lower(), "limit": 100, "order": "closedTime", "ascending": "false"}
    offset = 0
    all_raw = []

    while len(all_raw) < limit:
        params["offset"] = offset
        batch = _get(f"{GAMMA_API}/markets", params=params)
        if not batch:
            break
        all_raw.extend(batch)
        if len(batch) < 100:
            break
        offset += 100

    markets = []
    for m in all_raw:
        if slug_contains and slug_contains not in m.get("slug", ""):
            continue
        vol = float(m.get("volumeNum", 0) or 0)
        if vol < min_volume:
            continue

        slug = m.get("slug", "")
        if use_cache:
            cached = _load_cache(slug)
            if cached:
                market = MarketData(
                    market_id=cached["market_id"], slug=cached["slug"],
                    question=cached["question"], start_time=cached["start_time"],
                    end_time=cached["end_time"], volume=cached["volume"],
                    outcomes=cached["outcomes"], neg_risk=cached["neg_risk"],
                    resolved_outcome=cached["resolved_outcome"],
                    tokens=[
                        TokenHistory(token_id=t["token_id"], outcome=t["outcome"],
                            history=[PricePoint(p["t"], p["p"]) for p in t["history"]],
                            resolved_price=t["resolved_price"])
                        for t in cached["tokens"]
                    ],
                )
                markets.append(market)
                continue

        parsed = _parse_market_raw(m)
        if not parsed:
            continue
        market, token_ids, prices = parsed

        if start_date:
            cutoff = int(datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc).timestamp())
            if market.end_time < cutoff:
                continue
        if end_date:
            cutoff = int(datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc).timestamp())
            if market.end_time > cutoff:
                continue

        print(f"  📥 {slug[-40:]:<40} vol=${market.volume:,.0f}")
        tokens = []
        for outcome, token_id, res_price in zip(market.outcomes, token_ids, prices):
            time.sleep(RATE_LIMIT_SLEEP)
            history = fetch_price_history(token_id)
            tokens.append(TokenHistory(token_id=token_id, outcome=outcome,
                history=history, resolved_price=float(res_price)))
        market.tokens = tokens

        if use_cache:
            _save_cache(slug, {
                "market_id": market.market_id, "slug": market.slug,
                "question": market.question, "start_time": market.start_time,
                "end_time": market.end_time, "volume": market.volume,
                "outcomes": market.outcomes, "neg_risk": market.neg_risk,
                "resolved_outcome": market.resolved_outcome,
                "tokens": [{"token_id": t.token_id, "outcome": t.outcome,
                    "history": [{"t": p.timestamp, "p": p.price} for p in t.history],
                    "resolved_price": t.resolved_price} for t in tokens],
            })

        markets.append(market)
        if len(markets) >= limit:
            break

    print(f"\n✅ Loaded {len(markets)} markets")
    return markets


# =============================================================================
# BACKTESTER ENGINE
# =============================================================================

def run_backtest(
    markets: list,
    strategy: Callable,
    bet_size_usd: float = 5.0,
) -> BacktestResults:
    """
    Run strategy over each market. Strategy receives a MarketData and returns
    a Trade (without pnl filled in) or None to skip.
    PnL is calculated here based on resolved outcome.
    """
    results = BacktestResults()

    for market in markets:
        results.markets_seen += 1

        trade = strategy(market)
        if trade is None:
            results.markets_skipped += 1
            continue

        # Override shares based on bet_size if not set
        if trade.shares == 0 and trade.entry_price > 0:
            trade.shares = int(bet_size_usd / trade.entry_price)

        # Calculate PnL
        cost = trade.entry_price * trade.shares
        if market.resolved_outcome and trade.outcome.upper() == market.resolved_outcome.upper():
            trade.won = True
            trade.pnl = round(trade.shares - cost, 4)
        else:
            trade.won = False
            trade.pnl = round(-cost, 4)

        results.trades.append(trade)
        results.markets_entered += 1

    return results


# =============================================================================
# RESULTS DISPLAY
# =============================================================================

def print_results(results: BacktestResults, show_trades: bool = True):
    print("\n" + "="*60)
    print("  BACKTEST RESULTS")
    print("="*60)
    print(f"  Markets seen:    {results.markets_seen}")
    print(f"  Markets entered: {results.markets_entered}")
    print(f"  Markets skipped: {results.markets_skipped}")
    print()
    print(f"  Wins:      {results.wins}")
    print(f"  Losses:    {results.losses}")
    print(f"  Win rate:  {results.win_rate*100:.1f}%")
    print()
    print(f"  Total PnL:    ${results.total_pnl:+.2f}")
    print(f"  Total wagered: ${results.total_wagered:.2f}")
    print(f"  ROI:           {results.roi*100:.1f}%")
    print("="*60)

    if show_trades and results.trades:
        print("\n  TRADE LOG:")
        print(f"  {'slug':<30} {'side':<6} {'price':>6} {'shares':>6} {'pnl':>8}")
        print("  " + "-"*60)
        for t in results.trades:
            tag = "WIN " if t.won else "LOSS"
            print(f"  {t.market_slug[-30:]:<30} {t.outcome:<6} {t.entry_price:>6.3f} {t.shares:>6} {t.pnl:>+8.2f}  {tag}")


def save_results_csv(results: BacktestResults, filepath: str):
    with open(filepath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["market_slug","outcome","entry_time","entry_price","shares","won","pnl"])
        w.writeheader()
        for t in results.trades:
            w.writerow({
                "market_slug": t.market_slug,
                "outcome": t.outcome,
                "entry_time": datetime.fromtimestamp(t.entry_time, tz=ET).strftime("%Y-%m-%d %H:%M:%S"),
                "entry_price": t.entry_price,
                "shares": t.shares,
                "won": t.won,
                "pnl": t.pnl,
            })
    print(f"✅ Results saved to {filepath}")


# =============================================================================
# EXAMPLE STRATEGY: Buzzer Beater (mirrors live bot logic)
# =============================================================================

def buzzer_beater_strategy(
    market: MarketData,
    entry_window_sec: int = 90,
    favored_threshold: float = 0.70,
    max_ask_cap: float = 0.97,
    market_duration: int = 300,
) -> Optional[Trade]:
    """
    Enter if, during the last `entry_window_sec` of the market, one side's
    price crosses `favored_threshold`. Takes the first qualifying price point.
    """
    if not market.resolved_outcome:
        return None

    window_start = market.end_time - entry_window_sec

    for token in market.tokens:
        for point in token.history:
            if point.timestamp < window_start:
                continue
            if favored_threshold <= point.price <= max_ask_cap:
                return Trade(
                    market_slug=market.slug,
                    outcome=token.outcome,
                    entry_time=point.timestamp,
                    entry_price=point.price,
                    shares=0,   # filled by run_backtest
                )

    return None


# =============================================================================
# MAIN — run example backtest on BTC 5-min markets
# =============================================================================

if __name__ == "__main__":
    print("🌙 Polymarket Backtester")
    print("="*60)

    markets = fetch_btc_5m_markets(days=30, use_cache=True)

    if not markets:
        print("No markets found.")
    else:
        print("\n🔁 Running buzzer beater strategy...")
        results = run_backtest(markets, strategy=buzzer_beater_strategy, bet_size_usd=5.0)
        print_results(results)
        save_results_csv(results, os.path.join(os.path.dirname(__file__), "backtest_results.csv"))
