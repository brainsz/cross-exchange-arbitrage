import os
import dotenv
from bpx.account import Account
from bpx.public import Public

dotenv.load_dotenv()

api_key = os.getenv('BACKPACK_API_KEY')
api_secret = os.getenv('BACKPACK_API_SECRET')

if not api_key or not api_secret:
    print("Error: API keys not found")
    exit(1)

account = Account(api_key, api_secret)
public = Public()

print("--- Account Methods ---")
for method in dir(account):
    if not method.startswith('_'):
        print(method)

print("\n--- Public Methods ---")
for method in dir(public):
    if not method.startswith('_'):
        print(method)
