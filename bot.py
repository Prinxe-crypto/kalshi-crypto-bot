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

    def get_balance(self):
        return self.get("/trade-api/v2/portfolio/balance")

    def get_crypto_markets(self):
        """Fetches open crypto series markets."""
        return self.get("/trade-api/v2/markets", params={"status": "open", "limit": 10})

if __name__ == "__main__":
    print("Initializing Kalshi Demo Bot...")
    bot = KalshiDemoBot(HOST, API_KEY_ID, PRIVATE_KEY_PEM)
    
    # 1. Check Balance
    balance_response = bot.get_balance()
    print("Demo Balance Response:", json.dumps(balance_response, indent=2))
    
    # 2. Scan Open Crypto Markets
    print("\nScanning active crypto markets...")
    markets_response = bot.get_crypto_markets()
    markets = markets_response.get("markets", [])
    
    print(f"Found {len(markets)} active markets:")
    for market in markets[:5]: # Print first 5 for review
        print(f"- Ticker: {market.get('ticker')} | Title: {market.get('title')} | Yes Bid/Ask: {market.get('yes_bid')}/{market.get('yes_ask')}")
