import os
import time
import base64
import json
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding
from cryptography.hazmat.primitives import hashes

# --- DEMO CONFIGURATION ---
BASE_URL = "https://external-api.demo.kalshi.co/trade-api/v2"

API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
PRIVATE_KEY_PEM = os.getenv("KALSHI_PRIVATE_KEY")

class KalshiDemoBot:
    def __init__(self, base_url, api_key_id, private_key_pem):
        self.base_url = base_url
        self.api_key_id = api_key_id
        
        if not private_key_pem:
            raise ValueError("Missing KALSHI_PRIVATE_KEY environment variable!")
            
        cleaned_pem = private_key_pem.strip()
        self.private_key = serialization.load_pem_private_key(
            cleaned_pem.encode("utf-8"), 
            password=None
        )

    def _get_signed_headers(self, method, endpoint_path):
        """Generates accurate cryptographic authentication headers for Kalshi Demo API."""
        timestamp = str(int(time.time() * 1000))
        
        # Kalshi signature format: timestamp + METHOD + /trade-api/v2 + path (no query strings)
        clean_path = endpoint_path.split("?")[0]
        if not clean_path.startswith("/trade-api/v2"):
            sign_path = f"/trade-api/v2{clean_path}"
        else:
            sign_path = clean_path
            
        message = (timestamp + method.upper() + sign_path).encode("utf-8")
        
        if isinstance(self.private_key, ed25519.Ed25519PrivateKey):
            signature = self.private_key.sign(message)
        else:
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
        return self.get("/portfolio/balance")

if __name__ == "__main__":
    print("Initializing Kalshi Demo Bot...")
    bot = KalshiDemoBot(BASE_URL, API_KEY_ID, PRIVATE_KEY_PEM)
    
    balance_response = bot.get_balance()
    print("Demo Balance Response:", json.dumps(balance_response, indent=2))
