#!/usr/bin/env python3
"""Check actual Backpack positions via API."""
import asyncio
import os
import sys
import requests
import time
import base64
from urllib.parse import urlencode
from cryptography.hazmat.primitives.asymmetric import ed25519

async def main():
    api_key = os.getenv('BACKPACK_API_KEY')
    api_secret = os.getenv('BACKPACK_API_SECRET')
    
    if not api_key or not api_secret:
        print("Error: BACKPACK_API_KEY and BACKPACK_API_SECRET must be set")
        return
    
    # Backpack positions endpoint
    url = "https://api.backpack.exchange/api/v1/positions"
    
    # Generate signature
    timestamp = int(time.time() * 1000)
    window = 5000
    instruction = "positionQuery"
    
    # Try with symbol parameter
    symbol = 'BTC_USDC_PERP'
    params = {'symbol': symbol}
    query_string = urlencode(params)
    message = f"instruction={instruction}&{query_string}&timestamp={timestamp}&window={window}"
    
    # Sign with ED25519
    private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(api_secret)
    )
    signature_bytes = private_key.sign(message.encode())
    signature = base64.b64encode(signature_bytes).decode()
    
    # Set headers
    headers = {
        'X-API-Key': api_key,
        'X-Signature': signature,
        'X-Timestamp': str(timestamp),
        'X-Window': str(window),
        'Content-Type': 'application/json'
    }
    
    print(f"Querying Backpack positions for {symbol}...")
    print(f"URL: {url}")
    print(f"Params: {params}")
    
    # Make request
    response = requests.get(url, headers=headers, params=params, timeout=10)
    
    print(f"\nStatus Code: {response.status_code}")
    print(f"Response: {response.text}")
    
    if response.status_code == 200:
        positions = response.json()
        print(f"\nParsed positions:")
        if isinstance(positions, list):
            for pos in positions:
                print(f"  Symbol: {pos.get('symbol')}")
                print(f"  Net Quantity: {pos.get('netQuantity')}")
                print(f"  Entry Price: {pos.get('entryPrice')}")
                print(f"  Mark Price: {pos.get('markPrice')}")
                print()
        else:
            print(f"  Unexpected format: {type(positions)}")
    elif response.status_code == 404:
        print("\n✅ No positions found (404) - This is normal if you have no open positions")

if __name__ == '__main__':
    asyncio.run(main())
