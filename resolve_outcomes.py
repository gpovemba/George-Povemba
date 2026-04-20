"""
Scan btc_buzzer_log.csv for pending entries and fill in outcomes from Polymarket.
Run this anytime to update the log with resolved results.
"""
import csv
import json
import os
import requests

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "btc_buzzer_log.csv")
HEADERS  = ["timestamp", "market_slug", "side", "entry_price", "shares", "outcome", "pnl_usd"]


def fetch_outcome(slug):
    try:
        resp = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={"slug": slug, "closed": "true"},
            timeout=10,
        )
        if resp.status_code != 200 or not resp.json():
            return None
        data = resp.json()[0]
        prices   = data.get("outcomePrices")
        outcomes = data.get("outcomes")
        if not prices or not outcomes:
            return None
        prices   = json.loads(prices)   if isinstance(prices,   str) else prices
        outcomes = json.loads(outcomes) if isinstance(outcomes, str) else outcomes
        for outcome, price in zip(outcomes, prices):
            if float(price) == 1.0:
                return outcome
        return None
    except Exception as e:
        print(f"  ⚠️  Error fetching {slug}: {e}")
        return None


def resolve_all():
    if not os.path.exists(LOG_FILE):
        print("No log file found.")
        return

    rows = []
    with open(LOG_FILE, "r", newline="") as f:
        rows = list(csv.DictReader(f))

    pending = [r for r in rows if r["outcome"] == "pending"]
    print(f"Found {len(pending)} pending entries across {len(set(r['market_slug'] for r in pending))} markets.\n")

    outcome_cache = {}
    updated = 0

    for row in rows:
        if row["outcome"] != "pending":
            continue
        slug = row["market_slug"]
        if slug not in outcome_cache:
            outcome_cache[slug] = fetch_outcome(slug)
            status = outcome_cache[slug] or "not resolved yet"
            print(f"  {slug}  ->  {status}")

        outcome = outcome_cache[slug]
        if outcome:
            shares     = float(row["shares"])
            entry_price = float(row["entry_price"])
            cost       = entry_price * shares
            won        = outcome.upper() == row["side"].upper()
            pnl        = round(shares - cost, 4) if won else round(-cost, 4)
            row["outcome"] = outcome
            row["pnl_usd"] = pnl
            updated += 1

    with open(LOG_FILE, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writeheader()
        w.writerows(rows)

    print(f"\n✅ Updated {updated} entries.")

    # Print summary
    resolved = [r for r in rows if r["outcome"] not in ("pending", "")]
    if resolved:
        wins   = [r for r in resolved if r["outcome"].upper() == r["side"].upper()]
        losses = [r for r in resolved if r["outcome"].upper() != r["side"].upper()]
        total_pnl = sum(float(r["pnl_usd"]) for r in resolved if r["pnl_usd"])
        print(f"\n📊 SESSION SUMMARY")
        print(f"  Attempts : {len(resolved)}")
        print(f"  Wins     : {len(wins)}")
        print(f"  Losses   : {len(losses)}")
        print(f"  Win Rate : {len(wins)/len(resolved)*100:.1f}%")
        print(f"  Total P&L: ${total_pnl:.4f}")


if __name__ == "__main__":
    resolve_all()
