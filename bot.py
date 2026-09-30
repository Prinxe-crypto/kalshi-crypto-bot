"""
bot.py
------
Kalshi 15-min crypto paper bot (simplified single-strategy version).

STRATEGY (S4 structure):
  - Buy BTC "down" single leg (enter only if price <= $0.50, or <= $0.55
    as a looser fallback)
  - Buy BTC-up + ETH-up combo (via Kalshi's real multivariate/RFQ system)
  - Spend $10 total per trade window, split across both legs
  - PAPER ONLY: logs what would happen, places no real orders

Each run does TWO things:
  1. Checks any PENDING trade from a previous run whose window has now
     settled, scores it win/loss, and updates running stats.
  2. Attempts a NEW entry for the current live window, logs it as pending.

Running stats (total PnL, wins, losses, fees) persist in stats.json
between runs, since GitHub Actions starts fresh each time.
"""

import os
import time
import base64
import json
import math
import requests
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# ── CONFIG ─────────────────────────────────────────────────────────────
API_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID", "")
PRIVATE_KEY_PEM = os.environ.get("KALSHI_PRIVATE_KEY_PEM", "")

BTC_SERIES = "KXBTC15M"
ETH_SERIES = "KXETH15M"
COMBO_COLLECTION_TICKER = "KXMVECROSSCATEGORY-SHARD1-R"

TRADE_AMOUNT_USD = 10.0
SINGLE_LEG_MAX_PRICE = 0.50
SINGLE_LEG_FALLBACK_MAX = 0.55
RFQ_CONTRACT_SIZE = 1
RFQ_MAX_WAIT_SECONDS = 20

STATS_FILE = "stats.json"
LOG_FILE = "trade_log.csv"


# ── KALSHI CLIENT ──────────────────────────────────────────────────────
class KalshiClient:
    def __init__(self):
        if not API_KEY_ID or not PRIVATE_KEY_PEM:
            raise RuntimeError("Missing KALSHI_API_KEY_ID or KALSHI_PRIVATE_KEY_PEM env vars.")
        self.private_key = serialization.load_pem_private_key(
            PRIVATE_KEY_PEM.encode("utf-8"), password=None
        )

    def _headers(self, method, path):
        timestamp_ms = str(int(time.time() * 1000))
        full_path = "/trade-api/v2" + path
        message = f"{timestamp_ms}{method}{full_path}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": API_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
            "Content-Type": "application/json",
        }

    def get(self, path, params=None):
        r = requests.get(API_BASE_URL + path, headers=self._headers("GET", path), params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def post(self, path, body):
        r = requests.post(API_BASE_URL + path, headers=self._headers("POST", path), json=body, timeout=10)
        r.raise_for_status()
        return r.json()

    def put(self, path, body=None):
        r = requests.put(API_BASE_URL + path, headers=self._headers("PUT", path), json=body or {}, timeout=10)
        r.raise_for_status()
        return r.json()

    def get_current_market(self, series_ticker):
        data = self.get("/markets", params={"series_ticker": series_ticker, "status": "open", "limit": 5})
        markets = data.get("markets", [])
        if not markets:
            return None
        markets.sort(key=lambda m: m.get("close_time", ""))
        return markets[0]

    def get_market(self, ticker):
        return self.get(f"/markets/{ticker}").get("market", {})

    def get_best_price(self, ticker, side="yes"):
        data = self.get(f"/markets/{ticker}/orderbook")
        book = data.get("orderbook_fp", data.get("orderbook", {}))
        key = f"{side}_dollars" if f"{side}_dollars" in book else side
        levels = book.get(key, [])
        if not levels:
            return None, 0
        price_str, size_str = levels[-1][0], levels[-1][1]
        return float(price_str), float(size_str)

    def resolve_combo_ticker(self, selected_markets):
        try:
            result = self.put(
                f"/multivariate_event_collections/{COMBO_COLLECTION_TICKER}/lookup",
                {"selected_markets": selected_markets},
            )
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                result = self.post(
                    f"/multivariate_event_collections/{COMBO_COLLECTION_TICKER}",
                    {"selected_markets": selected_markets, "with_market_payload": True},
                )
            else:
                raise
        return result["market_ticker"] if "market_ticker" in result else result["market"]["ticker"]

    def get_combo_price_via_rfq(self, combo_ticker, side="yes"):
        rfq = self.post("/communications/rfqs", {
            "market_ticker": combo_ticker, "contracts_fp": RFQ_CONTRACT_SIZE, "rest_remainder": False
        })
        rfq_id = rfq.get("rfq_id") or rfq.get("id")
        waited = 0
        while waited < RFQ_MAX_WAIT_SECONDS:
            quotes = self.get(f"/communications/rfqs/{rfq_id}/quotes").get("quotes", [])
            for q in quotes:
                price_cents = q.get(f"{side}_bid", 0)
                if price_cents and price_cents > 0:
                    return price_cents / 100.0
            time.sleep(1)
            waited += 1
        return None


# ── FEE ESTIMATE (Kalshi's standard taker fee formula) ──────────────────
def estimate_fee(price, contracts):
    # Kalshi fee: ceil(0.07 * contracts * price * (1 - price) * 100) / 100
    fee = math.ceil(0.07 * contracts * price * (1 - price) * 100) / 100
    return round(fee, 4)


# ── STATS PERSISTENCE ────────────────────────────────────────────────────
def load_stats():
    if os.path.isfile(STATS_FILE):
        with open(STATS_FILE) as f:
            return json.load(f)
    return {"total_trades": 0, "wins": 0, "losses": 0, "total_pnl": 0.0, "total_fees": 0.0, "pending": None}


def save_stats(stats):
    with open(STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)


def log_row(row):
    file_exists = os.path.isfile(LOG_FILE)
    import csv
    fields = ["timestamp_utc", "btc_ticker", "eth_ticker", "combo_ticker", "single_price",
              "combo_price", "contracts", "total_cost", "fees", "outcome", "pnl"]
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in fields})


# ── SETTLEMENT CHECK (for the pending trade from the previous run) ──────
def check_pending_settlement(client, stats):
    pending = stats.get("pending")
    if not pending:
        print("No pending trade from a previous run to settle.")
        return stats

    btc_market = client.get_market(pending["btc_ticker"])
    if btc_market.get("status") != "settled" and btc_market.get("result", "") == "":
        print(f"Pending trade on {pending['btc_ticker']} has not settled yet -- leaving it pending.")
        return stats

    btc_result = btc_market.get("result", "")  # "yes" or "no"
    # Single leg = BTC DOWN (bought "no"). Combo = BTC UP + ETH UP.
    single_won = (btc_result == "no")
    combo_won = (btc_result == "yes")  # combo also needs ETH up, but if BTC didn't go up, combo can't have won
    if btc_result == "yes":
        eth_market = client.get_market(pending["eth_ticker"])
        eth_result = eth_market.get("result", "")
        combo_won = (eth_result == "yes")

    payout = 0.0
    if single_won:
        payout = pending["contracts"] * 1.0
    elif combo_won:
        payout = pending["contracts"] * 1.0
    pnl = round(payout - pending["total_cost"], 4)

    won = payout > pending["total_cost"]
    stats["total_trades"] += 1
    stats["wins"] += 1 if won else 0
    stats["losses"] += 0 if won else 1
    stats["total_pnl"] = round(stats["total_pnl"] + pnl, 4)

    log_row({
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "btc_ticker": pending["btc_ticker"], "eth_ticker": pending["eth_ticker"],
        "combo_ticker": pending["combo_ticker"], "single_price": pending["single_price"],
        "combo_price": pending["combo_price"], "contracts": pending["contracts"],
        "total_cost": pending["total_cost"], "fees": pending["fees"],
        "outcome": "win" if won else "loss", "pnl": pnl,
    })

    print(f"SETTLED: {pending['btc_ticker']} -- {'WIN' if won else 'LOSS'} -- PnL: ${pnl}")
    stats["pending"] = None
    return stats


# ── NEW ENTRY ATTEMPT ────────────────────────────────────────────────────
def attempt_new_entry(client, stats):
    btc_market = client.get_current_market(BTC_SERIES)
    eth_market = client.get_current_market(ETH_SERIES)
    if not btc_market or not eth_market:
        print("No open BTC/ETH 15-min market right now.")
        return stats

    single_price, _ = client.get_best_price(btc_market["ticker"], side="no")  # BTC DOWN
    if single_price is None:
        print("Could not get BTC single-leg price.")
        return stats

    max_price = SINGLE_LEG_MAX_PRICE if single_price <= SINGLE_LEG_MAX_PRICE else SINGLE_LEG_FALLBACK_MAX
    if single_price > max_price:
        print(f"SKIPPED: BTC-down price ${single_price} above max ${max_price}")
        return stats

    selected_markets = [
        {"event_ticker": btc_market["event_ticker"], "market_ticker": btc_market["ticker"], "side": "yes"},
        {"event_ticker": eth_market["event_ticker"], "market_ticker": eth_market["ticker"], "side": "yes"},
    ]
    combo_ticker = client.resolve_combo_ticker(selected_markets)
    combo_price = client.get_combo_price_via_rfq(combo_ticker, side="yes")
    if combo_price is None:
        print("SKIPPED: no combo quote received.")
        return stats

    per_contract_cost = single_price + combo_price
    contracts = int(TRADE_AMOUNT_USD // per_contract_cost)
    if contracts < 1:
        print(f"SKIPPED: per-contract cost ${per_contract_cost} too high for ${TRADE_AMOUNT_USD} budget.")
        return stats

    total_cost = round(per_contract_cost * contracts, 4)
    fees = round(estimate_fee(single_price, contracts) + estimate_fee(combo_price, contracts), 4)

    stats["total_fees"] = round(stats.get("total_fees", 0.0) + fees, 4)
    stats["pending"] = {
        "btc_ticker": btc_market["ticker"], "eth_ticker": eth_market["ticker"],
        "combo_ticker": combo_ticker, "single_price": single_price, "combo_price": combo_price,
        "contracts": contracts, "total_cost": total_cost, "fees": fees,
    }

    print(f"ENTERED: BTC-down @ ${single_price} + Combo @ ${combo_price} x{contracts} contracts, "
          f"total cost ${total_cost}, fees ${fees}")
    return stats


# ── SUMMARY PRINT ─────────────────────────────────────────────────────────
def print_summary(prev_stats, stats):
    print("\n" + "=" * 55)
    print("RUN SUMMARY")
    print("=" * 55)
    print(f"Previous total PnL : ${prev_stats.get('total_pnl', 0.0)}")
    print(f"Current total PnL  : ${stats['total_pnl']}")
    print(f"Total fees paid    : ${stats['total_fees']}")
    print(f"Total trades       : {stats['total_trades']}  (Wins: {stats['wins']}, Losses: {stats['losses']})")
    if stats.get("pending"):
        p = stats["pending"]
        print(f"Pending trade      : {p['btc_ticker']} / {p['combo_ticker']} "
              f"({p['contracts']} contracts, cost ${p['total_cost']})")
    else:
        print("Pending trade      : none")
    print("=" * 55 + "\n")


def main():
    client = KalshiClient()
    stats = load_stats()
    prev_stats = json.loads(json.dumps(stats))  # snapshot before this run's changes

    stats = check_pending_settlement(client, stats)
    stats = attempt_new_entry(client, stats)

    save_stats(stats)
    print_summary(prev_stats, stats)


if __name__ == "__main__":
    main()
