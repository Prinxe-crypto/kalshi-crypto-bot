import os
import time
import base64
import json
import random
import requests
import pandas as pd
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# --- CONFIGURATION ---
PROD_HOST = "https://external-api.kalshi.com"
SANDBOX_HOST = "https://external-api.demo.kalshi.co"
API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY")
PAPER_DRY_RUN = True  

class KalshiHybridBot:
    def __init__(self, prod_host, sandbox_host, api_key_id, private_key_pem):
        self.prod_host = prod_host
        self.sandbox_host = sandbox_host
        self.api_key_id = api_key_id
        
        if not private_key_pem:
            self.private_key = None
        else:
            cleaned_pem = private_key_pem.strip()
            self.private_key = serialization.load_pem_private_key(
                cleaned_pem.encode("utf-8"), password=None
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
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256()
        )
        
        return {
            "Content-Type": "application/json",
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8")
        }

    def get(self, path, params=None):
        url = self.prod_host + path
        try:
            response = requests.get(url, params=params)
            return response.json()
        except Exception as e:
            print(f"Error fetching data: {e}")
            return {}

    def post(self, path, payload, use_sandbox=True):
        if PAPER_DRY_RUN and use_sandbox:
            return {"status": "success", "order_id": f"paper-sim-{int(time.time())}"}
            
        host = self.sandbox_host if use_sandbox else self.prod_host
        url = host + path
        headers = self._get_signed_headers("POST", path)
        response = requests.post(url, headers=headers, data=json.dumps(payload))
        return response.json()

    def create_custom_combo_market(self, collection_ticker, selected_markets_payload):
        """Programmatically creates/instantiates the custom combo market on Kalshi."""
        path = f"/trade-api/v2/multivariate_event_collections/{collection_ticker}"
        payload = {
            "selected_markets": selected_markets_payload,
            "with_market_payload": True
        }
        # Market creation requires production write access or signed headers
        response = self.post(path, payload, use_sandbox=False)
        return response


class ComboKStrategy:
    def __init__(self, bot):
        self.bot = bot

    def get_orderbook(self, ticker):
        try:
            data = self.bot.get(f"/trade-api/v2/markets/{ticker}/orderbook")
            ob = data.get("orderbook", {})
            levels = ob.get("yes_dollars", ob.get("yes", [])) or []

            if not levels:
                return pd.DataFrame(columns=["price", "size"])

            df = pd.DataFrame(levels, columns=["opp_price", "size"])
            df["price"] = (1.0 - df["opp_price"].astype(float)).round(4)
            return df.sort_values("price").reset_index(drop=True)[["price", "size"]]
        except Exception:
            return pd.DataFrame(columns=["price", "size"])

    def plan_matched_pairs(self, combo_book, single_book, target_size=1):
        # EXACT LIMITS LOCKED IN
        combo_max = 0.35
        single_max = 0.55
        combined_cap = 0.90

        valid_combo = combo_book[combo_book["price"] <= combo_max].copy()
        valid_single = single_book[single_book["price"] <= single_max].copy()

        if valid_combo.empty or valid_single.empty:
            return {"filled_size": 0, "reason": "Prices exceed $0.35 (combo) or $0.55 (single), or book is empty."}

        combo_price = valid_combo.iloc[0]["price"]
        single_price = valid_single.iloc[0]["price"]
        total_cost = round(combo_price + single_price, 4)

        if total_cost > combined_cap:
            return {"filled_size": 0, "reason": f"Total cost ${total_cost} is over the $0.90 cap."}

        return {
            "filled_size": target_size,
            "combo_price": combo_price,
            "single_price": single_price,
            "total_cost": total_cost,
            "profit_buffer": round(combined_cap - total_cost, 4) 
        }


if __name__ == "__main__":
    print("Starting bot with Programmatic Custom Combo Creation...")
    bot = KalshiHybridBot(PROD_HOST, SANDBOX_HOST, API_KEY_ID, PRIVATE_KEY_PEM)
    strategy = ComboKStrategy(bot)
    
    # Example structure for your custom manual combo creation payload
    # (Replace collection_ticker and selected_markets with your target legs)
    collection_ticker = "KXMVECROSSCATEGORY-SHARD1" 
    selected_markets = [] # Add your specific child market tickers here when ready
    
    print(f"Instantiating custom combo under collection: {collection_ticker}")
    creation_res = bot.create_custom_combo_market(collection_ticker, selected_markets)
    print(f"Creation Response: {creation_res}")
    
    # Extract the resulting custom market ticker if successfully created
    custom_combo_ticker = creation_res.get("market_ticker", "")
    single_leg_ticker = "KXBTC15M-26..." # Your target single leg ticker
    
    if not custom_combo_ticker:
        print("Waiting for valid manual market creation payload parameters.")
    else:
        combo_book = strategy.get_orderbook(custom_combo_ticker)
        single_book = strategy.get_orderbook(single_leg_ticker)
        
        plan = strategy.plan_matched_pairs(combo_book, single_book, target_size=random.randint(1, 5))
        
        print("\n--- RESULTS ---")
        if plan.get("filled_size", 0) > 0:
            print(f"🟢 SUCCESS! Found prices under your limits.")
            print(f"Combo cost: ${plan['combo_price']} (Limit: $0.35)")
            print(f"Single cost: ${plan['single_price']} (Limit: $0.55)")
            print(f"Total spent: ${plan['total_cost']} (Cap: $0.90)")
        else:
            print(f"🔴 REJECTED: {plan['reason']}")
