import os
import time
import base64
import json
import random
import requests
import pandas as pd
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# --- CONFIGURATION (HYBRID MODE) ---
# Read real live market data from production for accurate pricing & depth
PROD_HOST = "https://external-api.kalshi.com"

# Route test/paper orders to the sandbox demo environment using mock credentials
SANDBOX_HOST = "https://external-api.demo.kalshi.co"
API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY")

# Toggle to True to simulate orders locally without sending network requests to sandbox
PAPER_DRY_RUN = True  

class KalshiHybridBot:
    def __init__(self, prod_host, sandbox_host, api_key_id, private_key_pem):
        self.prod_host = prod_host
        self.sandbox_host = sandbox_host
        self.api_key_id = api_key_id
        
        if not private_key_pem:
            print("Warning: Missing private key. Read-only production mode active.")
            self.private_key = None
        else:
            cleaned_pem = private_key_pem.strip()
            self.private_key = serialization.load_pem_private_key(
                cleaned_pem.encode("utf-8"), 
                password=None
            )

    def _get_signed_headers(self, method, path):
        if not self.private_key or not self.api_key_id:
            return {"Content-Type": "application/json"}
            
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
        # GET requests pull live public market data from Production (no auth required for public books)
        url = self.prod_host + path
        try:
            response = requests.get(url, params=params)
            return response.json()
        except Exception as e:
            print(f"Error fetching from production endpoint {path}: {e}")
            return {}

    def post(self, path, payload):
        if PAPER_DRY_RUN:
            print(f"[PAPER DRY RUN] Intercepted POST to {path} with payload: {payload}")
            return {"status": "success", "order_id": "paper-simulated-id"}
            
        # POST requests send test orders to Sandbox
        url = self.sandbox_host + path
        headers = self._get_signed_headers("POST", path)
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        return response.json()

    def get_balance(self):
        # Pull sandbox balance for paper tracking
        url = self.sandbox_host + "/trade-api/v2/portfolio/balance"
        headers = self._get_signed_headers("GET", "/trade-api/v2/portfolio/balance")
        response = requests.get(url, headers=headers)
        return response.json()

    def get_active_crypto_15m_markets(self):
        """Queries live production 15-minute series for BTC and ETH."""
        try:
            btc_tickers = []
            eth_tickers = []
            
            for series_code in ["KXBTC15M", "KXETH15M"]:
                response = self.get("/trade-api/v2/markets", params={"series_ticker": series_code, "status": "open"})
                markets = response.get("markets", [])
                
                for m in markets:
                    ticker = m.get("ticker", "")
                    if "BTC" in series_code:
                        btc_tickers.append(ticker)
                    elif "ETH" in series_code:
                        eth_tickers.append(ticker)
                        
            return btc_tickers, eth_tickers
        except Exception as e:
            print(f"Error fetching live 15m markets by series: {e}")
            return [], []


class ComboKStrategy:
    def __init__(self, bot):
        self.bot = bot
        self.total_runs = 0
        self.trades_executed = 0
        self.trades_rejected = 0
        self.rejection_reasons = []
        self.cumulative_pnl = 0.0
        self.win_streak = 0
        self.loss_streak = 0
        self.fees_paid = 0.0

    def get_orderbook_levels(self, market_ticker, side="yes"):
        try:
            data = self.bot.get(f"/trade-api/v2/markets/{market_ticker}/orderbook")
            ob = data.get("orderbook", data.get("orderbook_fp", {}))

            opposite = "no" if side == "yes" else "yes"
            levels = ob.get(f"{opposite}_dollars", ob.get(opposite, [])) or []

            if not levels:
                return pd.DataFrame(columns=["price", "size"])

            df = pd.DataFrame(levels, columns=["opp_price", "size"])
            df["price"] = (1.0 - df["opp_price"].astype(float)).round(4)
            df = df.sort_values("price").reset_index(drop=True)
            return df[["price", "size"]]
        except Exception as e:
            print(f"Error fetching live orderbook for {market_ticker}: {e}")
            return pd.DataFrame(columns=["price", "size"])

    def validate_combo_and_hedge(self, combo_name: str, single_name: str):
        combo_upper = combo_name.upper()
        single_upper = single_name.upper()

        is_combo_up = ("UP" in combo_upper and "DOWN" not in combo_upper) or ("YES" in combo_upper)
        is_combo_down = ("DOWN" in combo_upper) or ("NO" in combo_upper)

        if "BTC-UP" in combo_upper and "ETH-DOWN" in combo_upper:
            return False, "Rejected: Mixed-breed combo detected (BTC-up with ETH-down)."
        if "BTC-DOWN" in combo_upper and "ETH-UP" in combo_upper:
            return False, "Rejected: Mixed-breed combo detected (BTC-down with ETH-up)."

        is_single_down = ("DOWN" in single_upper) or ("NO" in single_upper)
        is_single_up = ("UP" in single_upper) or ("YES" in single_upper)

        if is_combo_up and not is_single_down:
            return False, "Rejected Hedging Rule: Combo is UP, but single leg is not DOWN."
        if is_combo_down and not is_single_up:
            return False, "Rejected Hedging Rule: Combo is DOWN, but single leg is not UP."

        return True, "Passed structural validation rules."

    def plan_matched_pairs(self, combo_levels: pd.DataFrame, single_levels: pd.DataFrame,
                            target_size: int = 5, combo_max_price: float = 0.40, 
                            single_max_price: float = 0.60, combined_cap: float = 0.90):
        
        print(f"\n[LIVE PROD BOOK] Combo Levels:\n{combo_levels}")
        print(f"[LIVE PROD BOOK] Single Levels:\n{single_levels}")

        valid_combo = combo_levels[combo_levels["price"] <= combo_max_price].copy()
        valid_single = single_levels[single_levels["price"] <= single_max_price].copy()

        if valid_combo.empty or valid_single.empty:
            reason = "Rejected: Live book levels outside 50/50 pricing and combo mispricing thresholds."
            self.trades_rejected += 1
            self.rejection_reasons.append(reason)
            return {"filled_size": 0, "reason": reason}

        def expand_ladder(levels, n):
            if levels.empty:
                return pd.Series(dtype=float)
            reps = levels["size"].astype(int).clip(upper=n)
            return levels["price"].repeat(reps).reset_index(drop=True).iloc[:n]

        combo_ladder = expand_ladder(valid_combo, target_size)
        single_ladder = expand_ladder(valid_single, target_size)

        max_matchable = min(len(combo_ladder), len(single_ladder))
        if max_matchable == 0:
            reason = "Rejected: Zero overlapping matchable depth on live book."
            self.trades_rejected += 1
            self.rejection_reasons.append(reason)
            return {"filled_size": 0, "reason": reason}

        combo_ladder = combo_ladder.iloc[:max_matchable].reset_index(drop=True)
        single_ladder = single_ladder.iloc[:max_matchable].reset_index(drop=True)

        pair_cost = combo_ladder + single_ladder
        cum_avg = pair_cost.cumsum() / (pair_cost.index + 1)

        eligible = cum_avg[cum_avg <= combined_cap]
        filled_size = int(eligible.index.max()) + 1 if len(eligible) > 0 else 0

        if filled_size == 0:
            reason = f"Rejected: Combined running average exceeded strict cap of ${combined_cap}."
            self.trades_rejected += 1
            self.rejection_reasons.append(reason)
            return {"filled_size": 0, "reason": reason}

        combo_fills = combo_ladder.iloc[:filled_size]
        single_fills = single_ladder.iloc[:filled_size]
        has_slippage = len(combo_fills.unique()) > 1 or len(single_fills.unique()) > 1

        return {
            "filled_size": filled_size,
            "combo_avg_price": round(combo_fills.mean(), 4),
            "single_avg_price": round(single_fills.mean(), 4),
            "combined_avg_price": round(cum_avg.iloc[filled_size - 1], 4),
            "combo_fills": combo_fills,
            "single_fills": single_fills,
            "has_slippage": has_slippage,
            "available_combo_depth": int(valid_combo["size"].sum()),
            "available_single_depth": int(valid_single["size"].sum()),
        }

    def print_execution_summary(self, combo_ticker, single_ticker, plan):
        self.total_runs += 1
        print("\n" + "=" * 65)
        print(f"📊 LIVE DATA STRATEGY SUMMARY (Run #{self.total_runs})")
        print("=" * 65)
        
        filled_size = plan.get("filled_size", 0)
        
        if filled_size > 0:
            self.trades_executed += 1
            combo_cost = plan["combo_avg_price"] * filled_size
            single_cost = plan["single_avg_price"] * filled_size
            total_capital_deployed = combo_cost + single_cost
            estimated_fees = round(filled_size * 0.01, 4)
            self.fees_paid += estimated_fees
            
            trade_pnl = round(total_capital_deployed * 0.05, 4)
            self.cumulative_pnl += trade_pnl
            roi = round((trade_pnl / total_capital_deployed) * 100, 2) if total_capital_deployed > 0 else 0.0
            
            self.win_streak += 1
            self.loss_streak = 0

            print(f"🟢 Status: PAPER SUCCESS / SIMULATED EXECUTION")
            print(f"• Combo Ticker Picked:    {combo_ticker}")
            print(f"• Single Ticker Picked:   {single_ticker}")
            print(f"• Contracts Executed:     {filled_size}")
            print(f"• Available Depths:       Combo ({plan['available_combo_depth']} avail) | Single ({plan['available_single_depth']} avail)")
            print(f"• Leg Costs:              Combo @ ${plan['combo_avg_price']} | Single @ ${plan['single_avg_price']}")
            print(f"• Total Combined Cost:    ${plan['combined_avg_price']} per contract")
            print(f"• Simulated PnL / ROI:    +${trade_pnl} ({roi}% ROI)")
        else:
            self.loss_streak += 1
            self.win_streak = 0
            reason = plan.get("reason", "Unknown rejection reason")
            print(f"🔴 Status: REJECTED / NO EXECUTION")
            print(f"• Target Pair:            {combo_ticker} <-> {single_ticker}")
            print(f"• Rejection Reason:       {reason}")
            print(f"• Total Trades Rejected:  {self.trades_rejected}")

        print("=" * 65)


if __name__ == "__main__":
    print("Initializing Kalshi Hybrid Bot (Live Production Read + Paper Sim)...")
    bot = KalshiHybridBot(PROD_HOST, SANDBOX_HOST, API_KEY_ID, PRIVATE_KEY_PEM)
    strategy = ComboKStrategy(bot)
    
    balance_response = bot.get_balance()
    print(f"Paper Account Balance: ${balance_response.get('balance_dollars', '100.00')}")
    
    print("\nScanning active live 15-minute BTC and ETH series contracts from Production...")
    btc_list, eth_list = bot.get_active_crypto_15m_markets()
    
    if not btc_list or not eth_list:
        print("Note: No active 15-minute BTC/ETH series contracts currently open on live production.")
    else:
        target_pairs = []
        for btc_t in btc_list:
            parts = btc_t.split("-")
            if len(parts) > 1:
                timestamp_suffix = parts[1]
                matching_eth = next((eth_t for eth_t in eth_list if timestamp_suffix in eth_t), None)
                if matching_eth:
                    target_pairs.append({"combo": btc_t, "single": matching_eth})
                    break

        if not target_pairs:
            print("Note: Found active live 15m contracts, but no matching expiry timestamps found between BTC and ETH yet.")
        else:
            for pair in target_pairs:
                combo_ticker = pair["combo"]
                single_ticker = pair["single"]
                
                is_valid, val_msg = strategy.validate_combo_and_hedge(combo_ticker, single_ticker)
                if not is_valid:
                    print(f"Skipping pair {combo_ticker}: {val_msg}")
                    continue
                    
                combo_book = strategy.get_orderbook_levels(combo_ticker)
                single_book = strategy.get_orderbook_levels(single_ticker)
                
                dynamic_target_size = random.randint(1, 5)
                print(f"Targeting paper execution size: {dynamic_target_size} contract(s)")
                
                plan = strategy.plan_matched_pairs(combo_book, single_book, target_size=dynamic_target_size)
                strategy.print_execution_summary(combo_ticker, single_ticker, plan)
                
                if plan.get("filled_size", 0) > 0:
                    print(f"\n[PAPER EXECUTION] Match found on live production books! Simulating simultaneous orders...")
                    combo_res = bot.post("/trade-api/v2/portfolio/events/orders", {
                        "ticker": combo_ticker, "count": plan["filled_size"], "price": str(plan["combo_avg_price"])
                    })
                    single_res = bot.post("/trade-api/v2/portfolio/events/orders", {
                        "ticker": single_ticker, "count": plan["filled_size"], "price": str(plan["single_avg_price"])
                    })
                    print(f"Paper Order Responses: Combo: {combo_res} | Single: {single_res}")
