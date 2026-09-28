import os
import time
import base64
import json
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# --- CONFIGURATION ---
BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"

# Read credentials safely from GitHub Environment Variables
API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY")

class KalshiBot:
    def __init__(self, base_url, api_key_id, private_key_pem):
        self.base_url = base_url
        self.api_key_id = api_key_id
        
        if not private_key_pem:
            raise ValueError("Missing KALSHI_PRIVATE_KEY environment variable!")
            
        self.private_key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), 
            password=None
        )

    def _get_signed_headers(self, method, path):
        """Generates the required RSA-PSS cryptographic headers for Kalshi API authentication."""
        timestamp = str(int(time.time() * 1000))
        path_only = path.split("?")[0]
        message = (timestamp + method.upper() + path_only).encode("utf-8")
        
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
        url = self.base_url + path
        headers = self._get_signed_headers("GET", path)
        response = requests.get(url, headers=headers, params=params)
        return response.json()

    def get_balance(self):
        """Fetch demo account balance."""
        return self.get("/portfolio/balance")

    def get_crypto_markets(self):
        """Fetch active crypto markets."""
        return self.get("/markets", params={"status": "open", "series_ticker": "KXBTC"})

if __name__ == "__main__":
    print("Initializing Kalshi Demo Bot...")
    bot = KalshiBot(BASE_URL, API_KEY_ID, PRIVATE_KEY_PEM)
    
    # Test connection by checking balance
    balance = bot.get_balance()
    print("Demo Balance Response:", json.dumps(balance, indent=2))
    
    # Check active markets
    markets = bot.get_crypto_markets()
    print("Active Crypto Markets Fetched Successfully!")
