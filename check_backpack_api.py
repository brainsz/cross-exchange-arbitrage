#!/usr/bin/env python3
"""Check Backpack API methods for positions."""
import os
from bpx.account import Account

# Load credentials
api_key = os.getenv('BACKPACK_API_KEY')
api_secret = os.getenv('BACKPACK_API_SECRET')

if not api_key or not api_secret:
    print("Error: BACKPACK_API_KEY and BACKPACK_API_SECRET must be set")
    exit(1)

# Initialize account
account = Account(api_key, api_secret)

# List all methods
print("=== Available Account methods ===")
methods = [m for m in dir(account) if not m.startswith('_')]
for method in methods:
    print(f"  - {method}")

print("\n=== Methods containing 'position' ===")
position_methods = [m for m in methods if 'position' in m.lower()]
for method in position_methods:
    print(f"  - {method}")

print("\n=== Methods containing 'balance' ===")
balance_methods = [m for m in methods if 'balance' in m.lower()]
for method in balance_methods:
    print(f"  - {method}")

# Try to get balances
print("\n=== Testing get_balances() ===")
try:
    balances = account.get_balances()
    print(f"Type: {type(balances)}")
    print(f"Content: {balances}")
except Exception as e:
    print(f"Error: {e}")
