import os
import time
from dotenv import load_dotenv
from termcolor import colored

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

PRIVATE_KEY_ENV_NAME = "PRIVATE_KEY"
PUBLIC_KEY_ENV_NAME  = "PUBLIC_KEY"
API_KEY_ENV_NAME     = "API_KEY"
API_SECRET_ENV_NAME  = "API_SECRET"
PASSPHRASE_ENV_NAME  = "SECRET"
SIGNATURE_TYPE       = 1

_CLIENT_CACHE = None


def _build_client():
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
        print(colored(f"❌ Missing {PRIVATE_KEY_ENV_NAME} or {PUBLIC_KEY_ENV_NAME} in .env!", "red"))
        raise EnvironmentError("Missing wallet credentials in .env")

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


def place_limit_order(token_id, side, price, size, neg_risk=False):
    """
    Place a limit order on Polymarket.

    Args:
        token_id  : The CLOB token ID for the outcome you want to buy/sell.
        side      : "BUY" or "SELL"
        price     : Limit price between 0.01 and 0.99 (represents cents on the dollar).
        size      : Number of shares.
        neg_risk  : Set True for neg-risk (multi-outcome) markets.

    Returns:
        dict with 'orderID' and 'status' on success, empty dict on failure.
        Returns {'orderID': 'timeout-assumed-live', 'status': 'LIVE'} if the POST
        timed out — the order may or may not have landed, do NOT retry blindly.
    """
    from py_clob_client.clob_types import OrderArgs, PartialCreateOrderOptions

    client = _build_client()
    order_args = OrderArgs(
        token_id=str(token_id),
        price=price,
        size=size,
        side=side.upper(),
        fee_rate_bps=1000,
    )

    print(colored(f"⬛ Placing {side} @ ${price:.4f} x {size} shares", "cyan"))

    if neg_risk:
        signed_order = client.create_order(order_args, options=PartialCreateOrderOptions(neg_risk=True))
    else:
        signed_order = client.create_order(order_args)

    try:
        response = client.post_order(signed_order)
        if response and response.get('orderID'):
            print(colored(f"✅ Order LIVE - id {response['orderID'][:10]}...", "green"))
            return response
        print(colored(f"❌ Order rejected: {response}", "red"))
        return response if response else {}
    except Exception as e:
        err = str(e)
        if 'Request exception' in err or 'Duplicated' in err or 'ReadTimeout' in err:
            print(colored("⚠️ post_order timed out - assuming it landed", "yellow"))
            return {'orderID': 'timeout-assumed-live', 'status': 'LIVE'}
        print(colored(f"❌ post_order failed: {type(e).__name__}: {e}", "red"))
        return {}


# =============================================================================
# Example usage
# =============================================================================
if __name__ == "__main__":
    # Replace these with real values from Polymarket
    TOKEN_ID = "YOUR_TOKEN_ID_HERE"
    SIDE     = "BUY"
    PRICE    = 0.72   # $0.72 per share
    SIZE     = 6      # 6 shares (~$4.32 total)

    result = place_limit_order(TOKEN_ID, SIDE, PRICE, SIZE)
    print(result)
