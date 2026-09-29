import os
import time
import base64
import json
import requests
import pandas as pd
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# --- CONFIGURATION ---
HOST = "https://external-api.demo.kalshi.co"
API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY")

class KalshiDemoBot:
    def __init__(self, host, api_key_id, private_key_pem):
        self.host = host
        self.api_key_id = api_key_id
        
        if not private_key_pem:
            raise ValueError("Missing KALSHI_PRIVATE_KEY environment variable!")
            
        cleaned_pem = private_key_pem.strip()
        self.private_key = serialization.load_pem_private_key(
            cleaned_pem.encode("utf-8"), 
            password=None
        )

    def _get_signed_headers(self, method, path):
        timestamp = str(int(time.time() * 1000))
        clean_path = path.split("?")[0]
        sign_path = f"/trade-api/v2{clean_path}" if not clean_path.startswith("/trade-api/v2") else clean_path
            
        message = (timestamp + method.upper() + sign_path).encode("utf-8")
        
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8")
        }

    def get(self, path, params=None):
        url = self.host + path
        headers = self._get_signed_headers("GET", path)
        response = requests.get(url, headers=headers, params=params)
        return response.json()

    def post(self, path, payload):
        url = self.host + path
        headers = self._get_signed_headers("POST", path)
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        return response.json()

    def get_balance(self):
        return self.get("/trade-api/v2/portfolio/balance")

    def place_order_v2(self, ticker, count, price_dollar_str, exchange_index=2):
        path = "/trade-api/v2/portfolio/events/orders"
        client_order_id = f"demo-bot-combo-{int(time.time() * 1000)}"
        
        payload = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "type": "limit",
            "action": "buy",
            "side": "bid",
            "count": str(count),
            "price": price_dollar_str,
            "exchange_index": exchange_index,
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross"
        }
        
        print(f"Submitting Order: BUY {count}x {ticker} at ${price_dollar_str} on Shard {exchange_index}")
        return self.post(path, payload)


class ComboKStrategy:
    def __init__(self, bot):
        self.bot = bot

    def discover_combo_collections(self):
        """Attempts to list multivariate (combo) event collections."""
        return self.bot.get("/trade-api/v2/multivariate_event_collections")

    def get_orderbook_levels(self, market_ticker, side="yes"):
        """Fetches order book levels and adjusts for buying yes via opposite bids."""
        book = self.bot.get(f"/trade-api/v2/markets/{market_ticker}/orderbook")
        ob = book.get("orderbook", book.get("orderbook_fp", {}))

        opposite = "no" if side == "yes" else "yes"
        levels = ob.get(f"{opposite}_dollars", ob.get(opposite, [])) or []

        if not levels:
            return pd.DataFrame(columns=["price", "size"])

        df = pd.DataFrame(levels, columns=["opp_price", "size"])
        df["price"] = (1.0 - df["opp_price"].astype(float)).round(4)
        df = df.sort_values("price").reset_index(drop=True)
        return df[["price", "size"]]

    def plan_matched_pairs(self, combo_levels: pd.DataFrame, single_levels: pd.DataFrame,
                            target_size: int, combined_cap: float = 0.85):
        """Vectorized price-walking engine enforcing the combined $0.85 cap."""
        def expand_ladder(levels, n):
            if levels.empty:
                return pd.Series(dtype=float)
            reps = levels["size"].astype(int).clip(upper=n)
            ladder = levels["price"].repeat(reps).reset_index(drop=True)
            return ladder.iloc[:n]

        combo_ladder = expand_ladder(combo_levels, target_size)
        single_ladder = expand_ladder(single_levels, target_size)

        max_matchable = min(len(combo_ladder), len(single_ladder))
        if max_matchable == 0:
            return {
                "filled_size": 0, "combo_avg_price": None, "single_avg_price": None,
                "combined_avg_price": None, "combo_fills": pd.DataFrame(), "single_fills": pd.DataFrame(),
            }

        combo_ladder = combo_ladder.iloc[:max_matchable].reset_index(drop=True)
        single_ladder = single_ladder.iloc[:max_matchable].reset_index(drop=True)

        pair_cost = combo_ladder + single_ladder
        cum_avg = pair_cost.cumsum() / (pair_cost.index + 1)

        eligible = cum_avg[cum_avg <= combined_cap]
        filled_size = int(eligible.index.max()) + 1 if len(eligible) > 0 else 0

        if filled_size == 0:
            return {
                "filled_size": 0, "combo_avg_price": None, "single_avg_price": None,
                "combined_avg_price": None, "combo_fills": pd.DataFrame(), "single_fills": pd.DataFrame(),
            }

        combo_fills = combo_ladder.iloc[:filled_size]
        single_fills = single_ladder.iloc[:filled_size]

        return {
            "filled_size": filled_size,
            "combo_avg_price": round(combo_fills.mean(), 4),
            "single_avg_price": round(single_fills.mean(), 4),
            "combined_avg_price": round(cum_avg.iloc[filled_size - 1], 4),
            "combo_fills": combo_fills,
            "single_fills": single_fills,
        }

    def dry_run(self, combo_ticker, single_ticker, target_size, combined_cap=0.85):
        combo_levels = self.get_orderbook_levels(combo_ticker, side="yes")
        single_levels = self.get_orderbook_levels(single_ticker, side="yes")

        plan = self.plan_matched_pairs(combo_levels, single_levels, target_size, combined_cap)

        print("=" * 60)
        print(f"DRY RUN — target size {target_size}, combined cap ${combined_cap}")
        print("=" * 60)
        print(f"Combo book depth available: {combo_levels['size'].sum() if not combo_levels.empty else 0}")
        print(f"Single book depth available: {single_levels['size'].sum() if not single_levels.empty else 0}")
        print(f"\nFilled matched-pair size: {plan['filled_size']}")
        if plan["filled_size"] > 0:
            print(f"Combo avg price:    ${plan['combo_avg_price']}")
            print(f"Single avg price:   ${plan['single_avg_price']}")
            print(f"Combined avg price: ${plan['combined_avg_price']}  (cap ${combined_cap})")
        else:
            print("No fillable size within cap — trade would be REJECTED, no orders placed.")

        return plan

    def execute_matched_pairs(self, combo_ticker, single_ticker, plan, dry_run_only=True):
        if plan["filled_size"] == 0:
            print("Nothing to execute — plan had zero fillable size.")
            return None

        size = plan["filled_size"]
        combo_price = plan["combo_avg_price"]
        single_price = plan["single_avg_price"]

        print(f"{'[DRY RUN] ' if dry_run_only else ''}Would place:")
        print(f"  BUY {size}x {combo_ticker} @ ~${combo_price} (combo leg)")
        print(f"  BUY {size}x {single_ticker} @ ~${single_price} (single leg)")

        if dry_run_only:
            return {"status": "dry_run_only", "plan": plan}

        combo_order = self.bot.place_order_v2(combo_ticker, size, f"{combo_price:.4f}")
        single_order = self.bot.place_order_v2(single_ticker, size, f"{single_price:.4f}")
        return {"combo_order": combo_order, "single_order": single_order}


if __name__ == "__main__":
    print("Initializing Kalshi Demo Bot with Combo Strategy...")
    bot = KalshiDemoBot(HOST, API_KEY_ID, PRIVATE_KEY_PEM)
    strategy = ComboKStrategy(bot)
    
    # 1. Check Balance
    balance_response = bot.get_balance()
    print(f"Account Balance: ${balance_response.get('balance_dollars', '0.00')}")
    
    # 2. Discover Combo Collections with Fallback Protection
    print("\nDiscovering multivariate combo collections...")
    collections = strategy.discover_combo_collections()
    
    # Check if collections were returned or if sandbox list is currently empty
    col_list = collections.get("collections", collections.get("multivariate_event_collections", []))
    if col_list:
        print(f"Found {len(col_list)} combo collections!")
        print(json.dumps(collections, indent=2))
    else:
        print("Note: No active multivariate collections currently populated in the sandbox environment.")
        print("Discovery response:", json.dumps(collections, indent=2))
        print("The strategy engine, order book walking, and $0.85 cap rules are fully armed and ready for when live combo markets open.")
