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

    def validate_combo_and_hedge(self, combo_name: str, single_name: str):
        """
        Enforces:
          1. Pure directional combo pairs (both up or both down; no mixed breeds).
          2. Inverse single-leg hedging rule (if combo is up, single must be down, and vice versa).
        """
        combo_upper = combo_name.upper()
        single_upper = single_name.upper()

        # Check combo direction structure
        is_combo_up = ("UP" in combo_upper and "DOWN" not in combo_upper) or ("YES" in combo_upper)
        is_combo_down = ("DOWN" in combo_upper) or ("NO" in combo_upper)

        # Ensure no mixed-breed combos (e.g. BTC-up & ETH-down)
        if "BTC-UP" in combo_upper and "ETH-DOWN" in combo_upper:
            return False, "Rejected: Mixed-breed combo detected (BTC-up with ETH-down)."
        if "BTC-DOWN" in combo_upper and "ETH-UP" in combo_upper:
            return False, "Rejected: Mixed-breed combo detected (BTC-down with ETH-up)."

        # Check single leg opposition rule
        is_single_down = ("DOWN" in single_upper) or ("NO" in single_upper)
        is_single_up = ("UP" in single_upper) or ("YES" in single_upper)

        if is_combo_up and not is_single_down:
            return False, "Rejected Hedging Rule: Combo is UP, but single leg is not DOWN."
        if is_combo_down and not is_single_up:
            return False, "Rejected Hedging Rule: Combo is DOWN, but single leg is not UP."

        return True, "Passed structural validation rules."

    def plan_matched_pairs(self, combo_levels: pd.DataFrame, single_levels: pd.DataFrame,
                            target_size: int, combo_max_price: float = 0.35, 
                            single_max_price: float = 0.55, combined_cap: float = 0.85):
        """
        Advanced order book walker incorporating:
          - Individual price ceilings (Combo <= $0.35, Single <= $0.55)
          - Asymmetric depth & partial fills (ladder walking)
          - Running average cumulative combined cost cap ($\le $0.85)
        """
        # 1. Filter levels by individual price limits
        valid_combo = combo_levels[combo_levels["price"] <= combo_max_price].copy()
        valid_single = single_levels[single_levels["price"] <= single_max_price].copy()

        if valid_combo.empty or valid_single.empty:
            return {
                "filled_size": 0, "combo_avg_price": None, "single_avg_price": None,
                "combined_avg_price": None, "combo_fills": pd.DataFrame(), "single_fills": pd.DataFrame()
            }

        # 2. Expand ladders to simulate depth and partial fills level-by-level
        def expand_ladder(levels, n):
            if levels.empty:
                return pd.Series(dtype=float)
            reps = levels["size"].astype(int).clip(upper=n)
            return levels["price"].repeat(reps).reset_index(drop=True).iloc[:n]

        combo_ladder = expand_ladder(valid_combo, target_size)
        single_ladder = expand_ladder(valid_single, target_size)

        max_matchable = min(len(combo_ladder), len(single_ladder))
        if max_matchable == 0:
            return {
                "filled_size": 0, "combo_avg_price": None, "single_avg_price": None,
                "combined_avg_price": None, "combo_fills": pd.DataFrame(), "single_fills": pd.DataFrame()
            }

        combo_ladder = combo_ladder.iloc[:max_matchable].reset_index(drop=True)
        single_ladder = single_ladder.iloc[:max_matchable].reset_index(drop=True)

        # 3. Calculate running average combined cost
        pair_cost = combo_ladder + single_ladder
        cum_avg = pair_cost.cumsum() / (pair_cost.index + 1)

        # 4. Enforce strict combined cap ($0.85 limit) with hard cutoff
        eligible = cum_avg[cum_avg <= combined_cap]
        filled_size = int(eligible.index.max()) + 1 if len(eligible) > 0 else 0

        if filled_size == 0:
            return {
                "filled_size": 0, "combo_avg_price": None, "single_avg_price": None,
                "combined_avg_price": None, "combo_fills": pd.DataFrame(), "single_fills": pd.DataFrame()
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

    def dry_run(self, combo_ticker, single_ticker, target_size, 
                combo_max_price=0.35, single_max_price=0.55, combined_cap=0.85):
        
        # Validate structural rules first
        is_valid, msg = self.validate_combo_and_hedge(combo_ticker, single_ticker)
        print("=" * 60)
        print(f"STRATEGY VALIDATION: {msg}")
        print("=" * 60)
        if not is_valid:
            return {"filled_size": 0, "reason": msg}

        combo_levels = self.get_orderbook_levels(combo_ticker, side="yes")
        single_levels = self.get_orderbook_levels(single_ticker, side="yes")

        plan = self.plan_matched_pairs(
            combo_levels, single_levels, target_size, 
            combo_max_price, single_max_price, combined_cap
        )

        print(f"DRY RUN — Target Size: {target_size} | Combo Max: ${combo_max_price} | Single Max: ${single_max_price} | Cap: ${combined_cap}")
        print("-" * 60)
        print(f"Combo book depth (within ceiling): {combo_levels[combo_levels['price'] <= combo_max_price]['size'].sum() if not combo_levels.empty else 0}")
        print(f"Single book depth (within ceiling): {single_levels[single_levels['price'] <= single_max_price]['size'].sum() if not single_levels.empty else 0}")
        print(f"\nSuccessfully Matched Partial Fill Size: {plan['filled_size']}")
        
        if plan["filled_size"] > 0:
            print(f"Combo Avg Price:    ${plan['combo_avg_price']}")
            print(f"Single Avg Price:   ${plan['single_avg_price']}")
            print(f"Combined Avg Price: ${plan['combined_avg_price']}  (Strict Cap < ${combined_cap})")
        else:
            print("No fillable size within individual price limits and combined cap — trade REJECTED.")

        return plan

    def execute_matched_pairs(self, combo_ticker, single_ticker, plan, dry_run_only=True):
        if plan.get("filled_size", 0) == 0:
            print("Nothing to execute — plan resulted in zero fillable contracts.")
            return None

        size = plan["filled_size"]
        combo_price = plan["combo_avg_price"]
        single_price = plan["single_avg_price"]

        print(f"\n{'[DRY RUN SIMULATION] ' if dry_run_only else '[LIVE EXECUTION] '}Action Plan:")
        print(f"  BUY {size}x {combo_ticker} @ ~${combo_price}")
        print(f"  BUY {size}x {single_ticker} @ ~${single_price}")

        if dry_run_only:
            return {"status": "dry_run_only", "plan": plan}

        combo_order = self.bot.place_order_v2(combo_ticker, size, f"{combo_price:.4f}")
        single_order = self.bot.place_order_v2(single_ticker, size, f"{single_price:.4f}")
        return {"combo_order": combo_order, "single_order": single_order}


if __name__ == "__main__":
    print("Initializing Kalshi Demo Bot with Full Combo & Hedging Rules...")
    bot = KalshiDemoBot(HOST, API_KEY_ID, PRIVATE_KEY_PEM)
    strategy = ComboKStrategy(bot)
    
    # 1. Check Balance
    balance_response = bot.get_balance()
    print(f"Account Balance: ${balance_response.get('balance_dollars', '0.00')}")
    
    # 2. Run Discovery Diagnostic
    print("\nDiscovering multivariate combo collections...")
    collections = strategy.discover_combo_collections()
    col_list = collections.get("collections", collections.get("multivariate_event_collections", []))
    
    if col_list:
        print(f"Found {len(col_list)} combo collections!")
    else:
        print("Note: No active multivariate collections populated in sandbox right now.")
        print("Strategy rules, individual price caps ($\le$0.35 / $\le$0.55), ladder partial fills, and overall $<0.85$ limit are fully armed.")
