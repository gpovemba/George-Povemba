"""
🌙 MOON DEV's 5-MINUTE BUZZER BEATER v1.0
=============================================================================
Automated last-90-second entry bot for BTC 5-minute UP/DOWN markets on Polymarket.

STRATEGY:
  - Wait until there are <= 90 seconds left in the current 5-min market
  - Look at the UP and DOWN order books
  - If one side's ask has crossed the "favored" threshold (default 0.70)
    the market has basically already picked a winner - pile into that side.
  - Place a limit BUY on the favored side sitting on the ask (aggressive).
  - Re-place every 3 seconds to chase until we fill or the market closes.
  - One entry per market. After we fill, we just hold until expiry.

MULTI-ACCOUNT:
  Runs on the OG account by default. Set ACCOUNT_SUFFIX below (e.g. "_MAY13",
  "_FEB19") to run on another wallet. Empty suffix uses signature_type=1 (OG),
  any other suffix uses signature_type=2 (Gnosis Safe multi-account).

288 buzzer beats per day. Let's steal some winners. 🍊🌿

Built by Moon Dev 🌙
=============================================================================
"""

import sys
import os
import time
import random
import itertools
import csv
from collections import deque
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from termcolor import colored

# =============================================================================
# MOON DEV - PATH / ENV SETUP
# =============================================================================
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(PROJECT_ROOT, '.env'))

# =============================================================================
# MOON DEV - ACCOUNT CONFIGURATION (Multi-Account Ready)
# =============================================================================
# "" = OG account (signature_type=1)
# "_MAY13", "_FEB19", etc. = other wallets (signature_type=2 / Gnosis Safe)
ACCOUNT_SUFFIX = ""

PRIVATE_KEY_ENV_NAME  = f"PRIVATE_KEY{ACCOUNT_SUFFIX}"
PUBLIC_KEY_ENV_NAME   = f"PUBLIC_KEY{ACCOUNT_SUFFIX}"
API_KEY_ENV_NAME      = f"API_KEY{ACCOUNT_SUFFIX}"
API_SECRET_ENV_NAME   = f"API_SECRET{ACCOUNT_SUFFIX}"
PASSPHRASE_ENV_NAME   = f"API_PASSPHRASE{ACCOUNT_SUFFIX}" if ACCOUNT_SUFFIX else "SECRET"

# OG uses signature_type=1, every other account uses 2 (Gnosis Safe)
SIGNATURE_TYPE = 1 if ACCOUNT_SUFFIX == "" else 2

# =============================================================================
# MOON DEV - STRATEGY CONFIGURATION
# =============================================================================
BET_SIZE_USD          = 5.0   # How much to bet per bucket
FAVORED_THRESHOLD     = 0.70  # Only enter if the favored side's ask >= this
ENTRY_WINDOW_SEC      = 90    # Start trying to enter when <= this many seconds left
STOP_THRESHOLD_SEC    = 30    # Stop placing new orders when <= this many seconds left
ORDER_UPDATE_INTERVAL = 3     # Re-place our bid every X seconds (chase allowed)
MAX_ASK_CAP           = 0.97  # Don't chase above this - too rich, not worth the risk

MARKET_DURATION = 300         # 5-minute markets
ET = timezone(timedelta(hours=-5))

# =============================================================================
# MOON DEV - DRY RUN MODE
# Set DRY_RUN = True to simulate without placing real orders.
# All logic runs normally — order placement is skipped and logged instead.
# =============================================================================
DRY_RUN = True

# =============================================================================
# MOON DEV - SESSION STATS
# =============================================================================
SESSION_ENTRIES  = 0
SESSION_SKIPS    = 0
SESSION_ATTEMPTS = 0
SESSION_START    = time.time()

# =============================================================================
# MOON DEV - CSV TRADE LOG
# =============================================================================
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "btc_buzzer_log.csv")
_LOG_HEADERS = ["timestamp", "market_slug", "side", "entry_price", "shares", "outcome", "pnl_usd"]

def _init_log():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow(_LOG_HEADERS)

def log_trade(market_slug, side, entry_price, shares, outcome=None):
    """Write a trade row. Call with outcome=None on entry, update later with outcome."""
    _init_log()
    pnl = None
    if outcome is not None and entry_price:
        cost = entry_price * shares
        pnl  = round(shares - cost, 4) if outcome.upper() == side.upper() else round(-cost, 4)
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S"),
            market_slug, side, entry_price, shares,
            outcome if outcome else "pending", pnl if pnl is not None else "",
        ])

def update_trade_outcome(market_slug, side, entry_price, outcome):
    """Re-open the CSV and fill in outcome + pnl for the matching pending row."""
    if not os.path.exists(LOG_FILE):
        return
    rows = []
    updated = False
    with open(LOG_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (not updated and row["market_slug"] == market_slug
                    and row["side"] == side and row["outcome"] == "pending"):
                shares = float(row["shares"])
                cost   = float(row["entry_price"]) * shares
                pnl    = round(shares - cost, 4) if outcome == side else round(-cost, 4)
                row["outcome"] = outcome
                row["pnl_usd"] = pnl
                updated = True
            rows.append(row)
    with open(LOG_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=_LOG_HEADERS)
        w.writeheader()
        w.writerows(rows)

def _parse_outcome_from_data(data):
    import json as _json
    prices   = data.get("outcomePrices")
    outcomes = data.get("outcomes")
    if not prices or not outcomes:
        return None
    prices   = _json.loads(prices)   if isinstance(prices,   str) else prices
    outcomes = _json.loads(outcomes) if isinstance(outcomes, str) else outcomes
    for outcome, price in zip(outcomes, prices):
        if float(price) == 1.0:
            return outcome
    return None

def resolve_market_outcome(market_id):
    """Fetch resolved outcome by market ID. Returns 'Up','Down', or None."""
    try:
        resp = requests.get(f"https://gamma-api.polymarket.com/markets/{market_id}", timeout=10)
        if resp.status_code != 200:
            return None
        return _parse_outcome_from_data(resp.json())
    except Exception:
        return None

def resolve_market_outcome_by_slug(slug):
    """Fetch resolved outcome by market slug. Returns 'Up','Down', or None."""
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug, "closed": "true"},
            timeout=10,
        )
        if resp.status_code != 200 or not resp.json():
            return None
        return _parse_outcome_from_data(resp.json()[0])
    except Exception:
        return None

# =============================================================================
# MOON DEV - FUN ZONE: HYPE MESSAGES, EVENT LOG, SPINNERS
# =============================================================================
HYPE_MESSAGES = [
    "LFG Moon Dev! 🚀🚀🚀",
    "Buzzer beater incoming 🏀",
    "Stealing the last 90 seconds ✖️",
    "Moon Dev on the hunt 🌙",
    "288 chances per day, let's eat 🍽️",
    "Favored side = free money 💸",
    "Patience. Conviction. Entry. 🎯",
    "Trust the buzzer, Moon Dev 🌙",
    "Let the suckers fight at 50c, we strike at 70c ✖️",
    "Moon stays winning 🏆",
    "Vibes: immaculate ✨",
    "Every 5 min is a new shot 📸",
    "Built different 🌙",
    "Cook mode: ENGAGED 🔥",
    "We chase. We fill. We ride. 🚀",
    "The last 90 belong to us 🏆",
    "Moon Dev = the closer 🎯",
    "Ice in the veins 🧊",
    "Buy the fear in the final minute 😱",
    "One bullet per market, make it count 🎯",
]

SPINNER_FRAMES = itertools.cycle(["e", "o", "0", "o"])
ROCKET_FRAMES  = itertools.cycle(["🌙 ", "  ", "🚀 ", "  ", "🚀 ", "  "])

EVENT_LOG = deque(maxlen=6)


def log_event(msg, color="white"):
    """Push a timestamped event onto the rolling log."""
    ts = datetime.now().strftime("%H:%M:%S")
    EVENT_LOG.append((ts, msg, color))


# =============================================================================
# MOON DEV - INLINE HELPERS (replaces nice_funcs dependency)
# =============================================================================

def calculate_shares(bet_size_usd, price):
    """Integer shares for a given USD amount at a given price."""
    if not price or price <= 0:
        return 0
    return int(bet_size_usd / price)


def get_token_id(market_id):
    """Returns [market_id, up_token_id, down_token_id] or [] on failure."""
    try:
        resp = requests.get(
            f"https://gamma-api.polymarket.com/markets/{market_id}",
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        # API returns clobTokenIds as a JSON string e.g. '["id1","id2"]'
        import json as _json
        raw = data.get('clobTokenIds')
        if raw:
            tokens = _json.loads(raw) if isinstance(raw, str) else raw
            if len(tokens) >= 2:
                return [market_id, tokens[0], tokens[1]]
        # fallback: old 'tokens' array shape
        tokens = data.get('tokens', [])
        if len(tokens) < 2:
            return []
        return [market_id, tokens[0]['token_id'], tokens[1]['token_id']]
    except Exception:
        return []


# =============================================================================
# MOON DEV - POLYMARKET CLIENT HELPERS (account-aware)
# =============================================================================
_CLIENT_CACHE = None


def _build_client():
    """Build (and cache) a ClobClient for the configured account."""
    global _CLIENT_CACHE
    if _CLIENT_CACHE is not None:
        return _CLIENT_CACHE

    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds
    from py_clob_client.constants import POLYGON
    from web3 import Web3

    key             = os.getenv(PRIVATE_KEY_ENV_NAME)
    browser_address = os.getenv(PUBLIC_KEY_ENV_NAME)

    if not key or not browser_address:
        print(colored(
            f"❌ Moon Dev - Missing {PRIVATE_KEY_ENV_NAME} or {PUBLIC_KEY_ENV_NAME} in .env!",
            "red",
        ))
        sys.exit(1)

    try:
        browser_wallet = Web3.toChecksumAddress(browser_address)
    except AttributeError:
        browser_wallet = Web3.to_checksum_address(browser_address)

    client = ClobClient(
        host="https://clob.polymarket.com",
        key=key,
        chain_id=POLYGON,
        funder=browser_wallet,
        signature_type=SIGNATURE_TYPE,
    )

    api_key    = os.getenv(API_KEY_ENV_NAME)
    api_secret = os.getenv(API_SECRET_ENV_NAME)
    passphrase = os.getenv(PASSPHRASE_ENV_NAME)

    if api_key and api_secret and passphrase:
        creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)
        client.set_api_creds(creds=creds)
    else:
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds=creds)

    _CLIENT_CACHE = client
    return client


def get_order_status_for_id(order_id):
    """
    Authoritative order status check by orderID.
    Returns dict on success, None if order not found/invalid,
    or "ERROR" if the API is down (caller should refuse to place).
    """
    if not order_id or order_id == 'timeout-assumed-live':
        return None
    if str(order_id).startswith('dry-run-'):
        return {'status': 'LIVE', 'size_matched': 0.0, 'original_size': 5.0, 'price': 0.0, 'is_filled': False}

    client = _build_client()
    for attempt in range(3):
        try:
            order = client.get_order(order_id)
            if not order:
                return {
                    'status': 'NOT_FOUND',
                    'size_matched': 0.0,
                    'original_size': 0.0,
                    'price': 0.0,
                    'is_filled': False,
                }
            if isinstance(order, dict):
                size_matched  = float(order.get('size_matched', 0) or 0)
                original_size = float(order.get('original_size', 0) or 0)
                status        = str(order.get('status', 'unknown'))
                price         = float(order.get('price', 0) or 0)
            else:
                size_matched  = float(getattr(order, 'size_matched', 0) or 0)
                original_size = float(getattr(order, 'original_size', 0) or 0)
                status        = str(getattr(order, 'status', 'unknown'))
                price         = float(getattr(order, 'price', 0) or 0)
            return {
                'status':        status.upper(),
                'size_matched':  size_matched,
                'original_size': original_size,
                'price':         price,
                'is_filled':     size_matched > 0,
            }
        except Exception as e:
            err = str(e)
            if any(code in err for code in ['500', '502', '503', '504', 'timeout', 'Timeout']):
                time.sleep(0.5 * (attempt + 1))
                continue
            log_event(f"⚠️ get_order failed: {type(e).__name__}", "yellow")
            return "ERROR"
    return "ERROR"


def place_limit_order(token_id, side, price, size, neg_risk=False):
    """Place a limit order for the configured account."""
    if DRY_RUN:
        log_event(f"[DRY RUN] Would place {side} @ ${price:.4f} x {size} shares", "magenta")
        return {'orderID': f'dry-run-{int(time.time())}', 'status': 'LIVE'}

    from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions

    client = _build_client()
    order_args = OrderArgs(
        token_id=str(token_id),
        price=price,
        size=size,
        side=side.upper(),
        fee_rate_bps=1000,
    )

    log_event(f"⬛ Placing {side} @ ${price:.4f} x {size} shares", "cyan")

    if neg_risk:
        signed_order = client.create_order(order_args, options=PartialCreateOrderOptions(neg_risk=True))
    else:
        signed_order = client.create_order(order_args)

    try:
        response = client.post_order(signed_order)
        if response and response.get('orderID'):
            log_event(f"✅ Order LIVE - id {response['orderID'][:10]}...", "green")
            return response
        log_event(f"❌ Order rejected: {response}", "red")
        return response if response else {}
    except Exception as e:
        err = str(e)
        if 'Request exception' in err or 'Duplicated' in err or 'ReadTimeout' in err:
            log_event("⚠️ post_order timed out - assuming it landed", "yellow")
            return {'orderID': 'timeout-assumed-live', 'status': 'LIVE'}
        log_event(f"❌ post_order failed: {type(e).__name__}: {e}", "red")
        return {}


def cancel_token_orders_acct(token_id):
    """Cancel all orders for a token on the configured account."""
    if DRY_RUN:
        log_event(f"[DRY RUN] Would cancel orders on {str(token_id)[:10]}...", "magenta")
        return True
    client = _build_client()
    try:
        client.cancel_market_orders(asset_id=str(token_id))
        log_event(f"🟡 Cancelled orders on {str(token_id)[:10]}...", "yellow")
        return True
    except Exception as e:
        log_event(f"⚠️ Cancel failed: {type(e).__name__}: {e}", "yellow")
        return False


# =============================================================================
# MOON DEV - MARKET DATA HELPERS
# =============================================================================

def get_current_market_timestamp():
    now = int(time.time())
    return (now // MARKET_DURATION) * MARKET_DURATION


def get_time_remaining(market_ts):
    return MARKET_DURATION - (int(time.time()) - market_ts)


def get_order_book(token_id):
    """Best bid/ask from Polymarket CLOB."""
    try:
        response = requests.get(
            "https://clob.polymarket.com/book",
            params={'token_id': token_id},
            timeout=10,
        )
        if response.status_code != 200:
            return None
        data = response.json()
        bids = data.get('bids', [])
        asks = data.get('asks', [])
        if not bids or not asks:
            return None
        # Polymarket returns bids/asks sorted worst->best.
        # bids[-1] = highest bid = best bid, asks[-1] = lowest ask = best ask.
        return {
            'best_bid': float(bids[-1]['price']),
            'best_ask': float(asks[-1]['price']),
        }
    except Exception:
        return None


def get_market_info(market_ts):
    """Resolve the btc-updown-5m market for this timestamp."""
    market_slug = f"btc-updown-5m-{market_ts}"
    try:
        response = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={'slug': market_slug, 'closed': 'false', 'active': 'true'},
            timeout=10,
        )
        if response.status_code != 200:
            return None
        markets = response.json()
        if not markets:
            return None

        market     = markets[0]
        token_data = get_token_id(market['id'])
        if len(token_data) != 3:
            return None

        return {
            'market_id':    market['id'],
            'up_token_id':  token_data[1],
            'down_token_id':token_data[2],
            'question':     market['question'],
            'slug':         market_slug,
            'neg_risk':     market.get('negRisk', False),
        }
    except Exception:
        return None


def position_shares_for_token(token_id):
    """How many shares do we hold of token_id on THIS account."""
    user_address = os.getenv(PUBLIC_KEY_ENV_NAME)
    if not user_address:
        return 0.0
    try:
        resp = requests.get(
            "https://data-api.polymarket.com/positions",
            params={'user': user_address, 'limit': 500, 'sortBy': 'CURRENT', 'sortDirection': 'DESC'},
            timeout=8,
        )
        if resp.status_code != 200:
            return 0.0
        for pos in resp.json():
            if str(pos.get('asset')) == str(token_id):
                return float(pos.get('size', 0) or 0)
        return 0.0
    except Exception as e:
        log_event(f"⚠️ positions fetch failed: {e}", "yellow")
        return 0.0


# =============================================================================
# MOON DEV - CORE BUZZER BEATER LOGIC
# =============================================================================

def pick_favored_side(up_book, down_book):
    """
    Pick the side the market has already decided.
    Returns ("UP"|"DOWN", book) or (None, None) if nothing is clearly favored.
    We go with whichever side's best_ask >= FAVORED_THRESHOLD (default 0.70).
    If somehow both qualify, go with the higher ask (stronger conviction).
    """
    up_ask   = up_book['best_ask']   if up_book   else None
    down_ask = down_book['best_ask'] if down_book else None

    up_qualifies   = up_ask   is not None and up_ask   >= FAVORED_THRESHOLD
    down_qualifies = down_ask is not None and down_ask >= FAVORED_THRESHOLD

    if up_qualifies and down_qualifies:
        return ("UP", up_book) if up_ask >= down_ask else ("DOWN", down_book)
    if up_qualifies:
        return "UP", up_book
    if down_qualifies:
        return "DOWN", down_book
    return None, None


def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')


BANNER_COLORS = itertools.cycle(["cyan", "magenta", "yellow", "green", "blue", "red"])


def draw_banner(hype_msg):
    color = next(BANNER_COLORS)
    print(colored("""
 ███╗   ███╗ ██████╗  ██████╗ ███╗   ██╗    ██████╗ ███████╗██╗   ██╗
 ████╗ ████║██╔═══██╗██╔═══██╗████╗  ██║    ██╔══██╗██╔════╝██║   ██║
 ██╔████╔██║██║   ██║██║   ██║██╔██╗ ██║    ██║  ██║█████╗  ██║   ██║
 ██║╚██╔╝██║██║   ██║██║   ██║██║╚██╗██║    ██║  ██║██╔══╝  ╚██╗ ██╔╝
 ██║ ╚═╝ ██║╚██████╔╝╚██████╔╝██║ ╚████║    ██████╔╝███████╗ ╚████╔╝
 ╚═╝     ╚═╝ ╚═════╝  ╚═════╝ ╚═╝  ╚═══╝    ╚═════╝ ╚══════╝  ╚═══╝
""", color, attrs=['bold']))
    print(colored("  🌙 5-MIN BUZZER BEATER 🏀 — Last-90s Favored-Side Entry Bot", "white", attrs=['bold']))
    print(colored(f"  🎯 {hype_msg}", "white", attrs=['bold']))
    print()


def draw_scoreboard():
    uptime_min = (time.time() - SESSION_START) / 60
    print(colored("  ⏱ SESSION SCOREBOARD 🏆 ————————————————————", "yellow", attrs=['bold']))
    print(
        colored(f"  🎯 Entries: {SESSION_ENTRIES:<4}", "green", attrs=['bold'])
        + colored(f"  🔄 Attempts: {SESSION_ATTEMPTS:<4}", "cyan")
        + colored(f"  😊 Skips: {SESSION_SKIPS:<4}", "white")
        + colored(f"  ⏰ Up: {uptime_min:5.1f}m", "magenta")
    )
    print(colored("  |", "yellow"))

    acct_label = "OG" if ACCOUNT_SUFFIX == "" else ACCOUNT_SUFFIX.lstrip("_")
    print(
        colored("  | ", "yellow")
        + colored(f"👤 Account: {acct_label:<6}", "cyan", attrs=['bold'])
        + colored(f"  💰 Bet: ${BET_SIZE_USD:<5.2f}", "green")
        + colored(f"  🎯 Thresh: {FAVORED_THRESHOLD:.2f}", "white")
        + colored(f"  🧢 Cap: {MAX_ASK_CAP:.2f}", "white")
    )
    print(colored("  |", "yellow"))
    print(colored("  └————————————————————————————————————————————————", "yellow", attrs=['bold']))


def draw_timer(time_left):
    """Timer bar + buzzer callout when we enter the window."""
    mins = max(0, time_left) // 60
    secs = max(0, time_left) % 60
    pct  = max(0.0, min(1.0, time_left / MARKET_DURATION))
    bar_width = 50
    filled = int(bar_width * pct)
    bar = "█" * filled + "░" * (bar_width - filled)

    if time_left > ENTRY_WINDOW_SEC:
        color = "green"
        label = "CHILLIN'"
    elif time_left > 30:
        color = "yellow"
        label = "🚨 BUZZER WINDOW OPEN 🚨"
    elif time_left > STOP_THRESHOLD_SEC:
        color = "red"
        label = "🔥🔥 FINAL STRETCH 🔥🔥"
    else:
        color = "red"
        label = "🛑 TOO LATE - STANDING DOWN"

    print()
    print(colored(f"  ⏰  TIME LEFT: {mins}:{secs:02d}  {label}", color, attrs=['bold']))
    print(colored(f"  [{bar}]", color))


def draw_books(market_info, up_book, down_book, favored_side):
    if not market_info:
        return
    print()
    print(colored(f"  📋 {market_info['question']}", "white", attrs=['bold']))
    print()

    def row(label, book, is_favored, base_color):
        if not book:
            print(colored(f"  {label} | No order book available", base_color))
            return
        tag = ""
        if is_favored:
            tag = colored(" → 🔥 FAVORED 🔥", "yellow", attrs=['bold', 'blink'])
        elif book['best_ask'] >= FAVORED_THRESHOLD:
            tag = colored(" → qualifying", "yellow")
        line = (
            colored(f"  {label} | ", base_color, attrs=['bold'])
            + colored(f"Bid: ${book['best_bid']:.4f}", base_color)
            + colored(" | ", "white")
            + colored(f"Ask: ${book['best_ask']:.4f}", base_color, attrs=['bold'])
        )
        print(line + tag)

    print(colored("  ——————————————————————————————————————————————————", "white"))
    row("🟢 UP  ", up_book,   favored_side == "UP",   "green")
    row("🔴 DOWN", down_book, favored_side == "DOWN", "red")
    print(colored("  ——————————————————————————————————————————————————", "white"))


def draw_status(state, time_left):
    print()
    if state['entered']:
        side   = state['favored_side']
        price  = state['entry_fill_price'] or 0.0
        rocket = next(ROCKET_FRAMES)
        c = "green"
        print(colored("  ┌————————————————————————————————————————————————┐", c, attrs=['bold']))
        print(colored(f"  | ▶▶▶ FILLED - IN ON {side:<5} ▶▶▶              |", c, attrs=['bold']))
        print(colored(f"  | Entry = ${price:.4f}   Holding to expiry       |", "white", attrs=['bold']))
        print(colored(f"  | {rocket} Ride or die, Moon Dev {rocket}         |", c, attrs=['bold']))
        print(colored("  └————————————————————————————————————————————————┘", c, attrs=['bold']))
        return

    spin = next(SPINNER_FRAMES)

    if time_left > ENTRY_WINDOW_SEC:
        wait = time_left - ENTRY_WINDOW_SEC
        print(colored(f"  {spin} CHILLIN' - {wait}s until the buzzer window opens...", "white"))
        return

    if state['favored_side'] is None:
        print(colored(f"  {spin} HUNTING for a favored side (need ask ≥ {FAVORED_THRESHOLD:.2f})...", "yellow"))
        return

    side       = state['favored_side']
    side_color = "green" if side == "UP" else "red"
    print(colored("  🔒 LOCKED ON: ", "magenta", attrs=['bold']) + colored(f"{side}", side_color, attrs=['bold']))

    if state['open_order_price'] is not None:
        print(colored(f"  🟠 CHASING @ ${state['open_order_price']:.4f}   {spin}", "cyan", attrs=['bold']))
    else:
        print(colored(f"  {spin} Preparing next order...", "cyan"))


def draw_event_log():
    print()
    print(colored("  ——————————————— 📋 RECENT EVENTS 📋 ———————————————", "white", attrs=['bold']))
    if not EVENT_LOG:
        print(colored("  |  (nothing yet - Moon Dev is warming up 🌙)        |", "white"))
    else:
        for ts, msg, color in EVENT_LOG:
            line = f"  | {ts}  {msg}"
            line = line[:54] if len(line) > 54 else line + " " * (54 - len(line))
            print(colored(line + "|", color))
    print(colored("  ——————————————————————————————————————————————————————", "white", attrs=['bold']))


def draw_footer():
    print(colored("  🌙 Built by Moon Dev — Ctrl+C to bail — LFG 🚀", "magenta", attrs=['bold']))


def draw_dashboard(state, time_left, up_book, down_book, hype_msg):
    clear_screen()
    draw_banner(hype_msg)
    draw_scoreboard()
    draw_timer(time_left)
    draw_books(state['market_info'], up_book, down_book, state['favored_side'])
    draw_status(state, time_left)
    draw_event_log()
    draw_footer()


# =============================================================================
# MOON DEV - MAIN LOOP
# =============================================================================

def main():
    global SESSION_ENTRIES, SESSION_SKIPS, SESSION_ATTEMPTS

    if DRY_RUN:
        log_event("🟣 DRY RUN MODE — no real orders will be placed", "magenta")
    elif not os.getenv(PRIVATE_KEY_ENV_NAME) or not os.getenv(PUBLIC_KEY_ENV_NAME):
        print(colored(
            f"❌ Moon Dev - Missing {PRIVATE_KEY_ENV_NAME} / {PUBLIC_KEY_ENV_NAME} in .env!",
            "red",
        ))
        sys.exit(1)

    state = {
        'market_ts':         None,
        'market_info':       None,
        'favored_side':      None,   # "UP" | "DOWN" | None (locked on first qualifying look)
        'favored_token_id':  None,
        'neg_risk':          False,
        'open_order_price':  None,   # price shown on dashboard
        'entered':           False,  # True once we have shares (or assume we do)
        'entry_fill_price':  None,
        'last_order_update': 0.0,
        # ORDER ID TRACKING - anti-dupe backbone.
        # Never place a second order unless the previous is proven non-live.
        'last_order_id':     None,   # exact orderID from post_order, or None
        'last_order_price':  None,   # price of last placed order
    }

    up_book          = None
    down_book        = None
    hype_msg         = random.choice(HYPE_MESSAGES)
    last_hype_change = time.time()
    last_draw        = 0.0

    log_event("🌙 Buzzer Beater booting up...", "cyan")
    log_event(f"👤 Account: {'OG' if ACCOUNT_SUFFIX == '' else ACCOUNT_SUFFIX}", "cyan")
    log_event(f"💰 Bet size: ${BET_SIZE_USD:.2f}", "green")

    # Resolve any pending outcomes from previous sessions
    _init_log()
    try:
        import csv as _csv
        _pending_slugs = set()
        with open(LOG_FILE, "r", newline="") as _f:
            for _row in _csv.DictReader(_f):
                if _row["outcome"] == "pending":
                    _pending_slugs.add(_row["market_slug"])
        for _slug in _pending_slugs:
            _outcome = resolve_market_outcome_by_slug(_slug)
            if _outcome:
                _rows2 = []
                with open(LOG_FILE, "r", newline="") as _f:
                    _reader = _csv.DictReader(_f)
                    for _r in _reader:
                        if _r["market_slug"] == _slug and _r["outcome"] == "pending":
                            _shares = float(_r["shares"])
                            _cost   = float(_r["entry_price"]) * _shares
                            _won    = _outcome.upper() == _r["side"].upper()
                            _r["outcome"] = _outcome
                            _r["pnl_usd"] = round(_shares - _cost, 4) if _won else round(-_cost, 4)
                        _rows2.append(_r)
                with open(LOG_FILE, "w", newline="") as _f:
                    _w = _csv.DictWriter(_f, fieldnames=_LOG_HEADERS)
                    _w.writeheader()
                    _w.writerows(_rows2)
                log_event(f"📊 Resolved old pending: {_slug[-10:]} → {_outcome}", "cyan")
    except Exception:
        pass

    def reset_market_state():
        """Called when the market rolls over to the next 5-min window."""
        state['market_ts']         = None
        state['market_info']       = None
        state['favored_side']      = None
        state['favored_token_id']  = None
        state['neg_risk']          = False
        state['open_order_price']  = None
        state['entered']           = False
        state['entry_fill_price']  = None
        state['last_order_update'] = 0.0
        state['last_order_id']     = None
        state['last_order_price']  = None

    while True:
        now       = time.time()
        market_ts = get_current_market_timestamp()
        time_left = get_time_remaining(market_ts)

        # Rotate hype message every 12 seconds
        if now - last_hype_change > 12:
            hype_msg         = random.choice(HYPE_MESSAGES)
            last_hype_change = now

        # — Market rollover ————————————————————————————————————————————
        if market_ts != state['market_ts']:
            if state['last_order_id'] is not None and state['favored_token_id'] and not state['entered']:
                cancel_token_orders_acct(state['favored_token_id'])
            # Resolve outcome for the market that just closed
            if state['market_info'] and state['favored_side'] and state['last_order_id']:
                outcome = resolve_market_outcome(state['market_info']['market_id'])
                if outcome:
                    update_trade_outcome(
                        state['market_info']['slug'],
                        state['favored_side'],
                        state['last_order_price'] or state['open_order_price'],
                        outcome,
                    )
                    log_event(f"📊 Resolved {state['market_info']['slug'][-10:]}: {outcome}", "cyan")
            reset_market_state()
            state['market_ts'] = market_ts
            up_book   = None
            down_book = None

            for attempt in range(8):
                info = get_market_info(market_ts)
                if info:
                    state['market_info'] = info
                    state['neg_risk']    = info.get('neg_risk', False)
                    break
                time.sleep(2)

        # — Only act inside the entry window ——————————————————————————
        if time_left <= ENTRY_WINDOW_SEC and state['market_info']:

            # Refresh order books every tick
            up_book   = get_order_book(state['market_info']['up_token_id'])
            down_book = get_order_book(state['market_info']['down_token_id'])

            # Lock onto favored side (only once per market)
            if state['favored_side'] is None:
                side, book = pick_favored_side(up_book, down_book)
                if side is None:
                    up_str   = f"{up_book['best_ask']:.2f}"   if up_book   else "N/A"
                    down_str = f"{down_book['best_ask']:.2f}" if down_book else "N/A"
                    log_event(
                        f"⚪ No favored side yet ({time_left}s). UP={up_str} DN={down_str}",
                        "white",
                    )
                else:
                    state['favored_side']     = side
                    state['favored_token_id'] = (
                        state['market_info']['up_token_id'] if side == "UP"
                        else state['market_info']['down_token_id']
                    )
                    log_event(
                        f"🔒 LOCKED {side} @ ask {book['best_ask']:.3f} ({time_left}s left)",
                        "magenta",
                    )

            # — Order verification + placement flow ————————————————————
            if state['favored_side'] is not None and not state['entered']:
                token_id = state['favored_token_id']

                # Short-circuit: positions API (slow but definitive when it catches up)
                shares_held = position_shares_for_token(token_id)
                if shares_held > 0:
                    state['entered']          = True
                    state['entry_fill_price'] = state['last_order_price'] or state['open_order_price'] or 0.0
                    state['open_order_price'] = None
                    SESSION_ENTRIES += 1
                    log_event(
                        f"▶ FILLED {state['favored_side']}! {shares_held:.1f}sh @ ~${state['entry_fill_price']:.4f}",
                        "green",
                    )

                elif state['last_order_id'] == 'timeout-assumed-live':
                    # Can't verify by ID — mark entered conservatively, stop placing
                    state['entered']          = True
                    state['entry_fill_price'] = state['last_order_price'] or 0.0

                elif state['last_order_id'] is not None:
                    # We placed a real order — ask the CLOB what happened to it
                    status_info = get_order_status_for_id(state['last_order_id'])

                    if status_info == "ERROR":
                        log_event("🔴 get_order API error - refusing to place", "red")

                    elif status_info is None:
                        log_event("🔴 Null status for tracked order - refusing to place", "red")

                    elif status_info['is_filled']:
                        state['entered']          = True
                        state['entry_fill_price'] = status_info['price'] or state['last_order_price'] or 0.0
                        state['open_order_price'] = None
                        SESSION_ENTRIES += 1
                        log_event(
                            f"▶ FILLED (via get_order)! status={status_info['status']} "
                            f"matched={status_info['size_matched']:.1f} @ ${state['entry_fill_price']:.4f}",
                            "green",
                        )

                    elif status_info['status'] == 'LIVE':
                        # Still resting — decide: keep or chase?
                        existing_price            = status_info['price'] or state['last_order_price'] or 0.0
                        state['open_order_price'] = existing_price

                        favored_book = up_book if state['favored_side'] == "UP" else down_book
                        best_ask     = favored_book['best_ask'] if favored_book else existing_price

                        if best_ask > MAX_ASK_CAP:
                            log_event(f"⚠️ Ask {best_ask:.3f} > cap - not chasing", "yellow")
                        else:
                            target_price = round(best_ask, 4)
                            if abs(existing_price - target_price) <= 0.01:
                                log_event(f"   resting @ ${existing_price:.4f} (no chase)", "white")
                            else:
                                # Chase: cancel → wait → re-verify → place fresh
                                log_event(f"🔄 Chase ${existing_price:.4f} → ${target_price:.4f}", "cyan")
                                cancel_token_orders_acct(token_id)
                                time.sleep(1.5)

                                # Re-query same orderID to catch cancel-race fills
                                post_info = get_order_status_for_id(state['last_order_id'])
                                if post_info == "ERROR" or post_info is None:
                                    log_event("🔴 post-cancel verify failed - refusing to place", "red")
                                elif post_info['is_filled']:
                                    state['entered']          = True
                                    state['entry_fill_price'] = post_info['price'] or existing_price
                                    state['open_order_price'] = None
                                    SESSION_ENTRIES += 1
                                    log_event(f"▶ FILLED during cancel race! @ ${state['entry_fill_price']:.4f}", "green")
                                elif post_info['status'] in ('CANCELED', 'CANCELLED', 'NOT_FOUND'):
                                    # Safe to place fresh — clear tracking
                                    state['last_order_id']    = None
                                    state['last_order_price'] = None
                                    state['open_order_price'] = None

                                    shares = calculate_shares(BET_SIZE_USD, target_price)
                                    if shares > 0:
                                        SESSION_ATTEMPTS += 1
                                        resp = place_limit_order(
                                            token_id, "BUY", target_price, shares,
                                            neg_risk=state['neg_risk'],
                                        )
                                        if resp:
                                            oid = resp.get('orderID')
                                            if oid:
                                                state['last_order_id']    = oid
                                                state['last_order_price'] = target_price
                                                state['open_order_price'] = target_price
                                else:
                                    log_event(
                                        f"   cancel didn't settle (status={post_info['status']}), holding",
                                        "yellow",
                                    )

                    elif status_info['status'] in ('CANCELED', 'CANCELLED', 'NOT_FOUND'):
                        # Previous order dead — clear tracking, fresh placement next tick
                        log_event(f"↩️ Previous order {status_info['status']} - clearing tracking", "white")
                        state['last_order_id']    = None
                        state['last_order_price'] = None
                        state['open_order_price'] = None

                    else:
                        log_event(f"❓ Unknown order status: {status_info['status']} - holding", "yellow")

                else:
                    # No previous order this market → fresh placement
                    favored_book = up_book if state['favored_side'] == "UP" else down_book
                    best_ask     = favored_book['best_ask'] if favored_book else None

                    if best_ask is None:
                        log_event("⚠️ No order book for favored side", "yellow")
                    elif best_ask > MAX_ASK_CAP:
                        log_event(f"⚠️ Ask {best_ask:.3f} > cap {MAX_ASK_CAP:.2f} - skip", "yellow")
                    else:
                        target_price = round(best_ask, 4)
                        shares       = calculate_shares(BET_SIZE_USD, target_price)
                        if shares > 0:
                            SESSION_ATTEMPTS += 1
                            resp = place_limit_order(
                                token_id, "BUY", target_price, shares,
                                neg_risk=state['neg_risk'],
                            )
                            if resp:
                                oid = resp.get('orderID')
                                if oid:
                                    state['last_order_id']    = oid
                                    state['last_order_price'] = target_price
                                    state['open_order_price'] = target_price
                                    log_trade(
                                        state['market_info']['slug'],
                                        state['favored_side'],
                                        target_price, shares,
                                    )

        # — Past the stop threshold — kill any stale order ————————————
        if (
            not state['entered']
            and time_left <= STOP_THRESHOLD_SEC
            and state['last_order_id'] is not None
            and state['favored_token_id']
        ):
            log_event(f"🛑 {time_left}s left - cancel stale order", "yellow")
            cancel_token_orders_acct(state['favored_token_id'])
            state['open_order_price'] = None
            state['last_order_id']    = None
            state['last_order_price'] = None

        # — Redraw dashboard every ~0.5s so spinners / bar feel live ——
        if now - last_draw >= 0.5:
            last_draw = now
            draw_dashboard(state, time_left, up_book, down_book, hype_msg)

        time.sleep(2.0)


if __name__ == "__main__":
    print(colored("🌙 Moon Dev's 5-Minute Buzzer Beater - loading up...", "cyan", attrs=['bold']))
    time.sleep(0.4)
    try:
        main()
    except KeyboardInterrupt:
        uptime = (time.time() - SESSION_START) / 60
        print()
        print(colored("  ┌————————————————————————————————————————————————┐", "yellow", attrs=['bold']))
        print(colored("  |        🌙 MOON DEV - GAME OVER 🌙               |", "yellow", attrs=['bold']))
        print(colored("  └————————————————————————————————————————————————┘", "yellow", attrs=['bold']))
        print(colored(f"  ✅ Entries: {SESSION_ENTRIES}", "green", attrs=['bold']))
        print(colored(f"  🔄 Attempts: {SESSION_ATTEMPTS}", "cyan"))
        print(colored(f"  😊 Skips: {SESSION_SKIPS}", "white"))
        print(colored(f"  ⏰ Uptime: {uptime:.1f}m", "magenta"))
        print(colored("  🌙 LFG Moon Dev - see you on the next buzzer! 🚀", "cyan"))
