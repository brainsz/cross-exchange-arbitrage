import asyncio
import os
import dotenv
from bpx.account import Account

dotenv.load_dotenv()

async def main():
    api_key = os.getenv('BACKPACK_API_KEY')
    api_secret = os.getenv('BACKPACK_API_SECRET')
    account = Account(api_key, api_secret)
    
    print("Fetching balances...")
    balances = await asyncio.to_thread(account.get_balances)
    print(f"Balances keys: {list(balances.keys())}")
    
    print("\nFetching positions via raw request...")
    try:
        # Try perps endpoint
        url = 'https://api.backpack.exchange/api/v1/perps/positions'
        positions = await asyncio.to_thread(account.get, url)
        print(f"Positions: {positions}")
    except Exception as e:
        print(f"Error fetching positions: {e}")

if __name__ == "__main__":
    asyncio.run(main())
