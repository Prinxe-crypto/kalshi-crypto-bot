import os
import time
import base64
import json
import requests
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

    def get_markets(self):
        return self.get("/trade-api/v2/markets", params={"status": "open", "limit": 10})

    def place_order_v2(self, ticker, count, price_dollar_str, exchange_index):
        path = "/trade-api/v2/portfolio/events/orders"
        client_order_id = f"demo-bot-v2-{int(time.time() * 1000)}"
        
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
        
        print(f"Submitting V2 Order: BUY {count}x {ticker} at ${price_dollar_str} on Shard {exchange_index}")
        return self.post(path, payload)

if __name__ == "__main__":
    print("Initializing Kalshi Demo Bot...")
    bot = KalshiDemoBot(HOST, API_KEY_ID, PRIVATE_KEY_PEM)
    
    # 1. Check Balance and automatically find which shard actually holds funds
    balance_response = bot.get_balance()
    breakdown = balance_response.get("balance_breakdown", [])
    
    active_shard = 2  # Default fallback
    for item in breakdown:
        if float(item.get("balance", "0")) > 0:
            active_shard = item.get("exchange_index", 2)
            break
            
    print(f"Using funded Exchange Shard Index: {active_shard}")
    
    # 2. Fetch markets and force the order to use our active funded shard index
    markets_response = bot.get_markets()
    markets = markets_response.get("markets", [])
    
    if markets:
        target_ticker = markets[0].get("ticker")
        print(f"Targeting ticker: {target_ticker}")
        
        # 3. Place order matching the active funded shard
        order_response = bot.place_order_v2(
            ticker=target_ticker,
            count=1,
            price_dollar_str="0.50",
            exchange_index=active_shard
        )
        print("V2 Order API Response:", json.dumps(order_response, indent=2))
    else:
        print("No open markets available right now.")
