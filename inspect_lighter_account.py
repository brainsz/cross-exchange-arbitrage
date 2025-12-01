import os
import requests
import dotenv
import json

dotenv.load_dotenv()

lighter_base_url = "https://mainnet.zklighter.elliot.ai"
account_index = os.getenv('LIGHTER_ACCOUNT_INDEX')

if not account_index:
    print("Error: LIGHTER_ACCOUNT_INDEX not set")
    exit(1)

url = f"{lighter_base_url}/api/v1/account"
headers = {"accept": "application/json"}
params = {"by": "index", "value": account_index}

try:
    response = requests.get(url, headers=headers, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    print(json.dumps(data, indent=2))
except Exception as e:
    print(f"Error: {e}")
