import os
import dotenv
from bpx.account import Account

dotenv.load_dotenv()

api_key = os.getenv('BACKPACK_API_KEY')
api_secret = os.getenv('BACKPACK_API_SECRET')

if not api_key or not api_secret:
    print("Error: API keys not found")
    exit(1)

account = Account(api_key, api_secret)

print("\n--- Debugging Account Attributes ---")
print("Dir(account):", [m for m in dir(account) if 'history' in m])

print("\n--- Checking Fill History ---")
try:
    fills = account.get_fill_history_query(symbol="BTC_USDC_PERP", limit=5)
    print("Fills:", fills)
except Exception as e:
    print(f"Error getting fills: {e}")

print("\n--- Checking Positions via Raw URL ---")
if hasattr(account, 'get'):
    base_url = "https://api.backpack.exchange/"
    endpoints = ["api/v1/position", "api/v1/positions", "wapi/v1/position", "wapi/v1/positions"]
    
    for ep in endpoints:
        url = base_url + ep
        print(f"Trying {url}...")
        try:
            res = account.get(url)
            print(f"Result for {ep}: {res}")
        except Exception as e:
            print(f"Error for {ep}: {e}")


