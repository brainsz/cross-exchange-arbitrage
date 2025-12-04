#!/usr/bin/env python3
"""Test Backpack position tracking."""
import asyncio
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exchanges.backpack import BackpackClient

class SimpleConfig:
    def __init__(self):
        self.ticker = 'BTC_USDC_PERP'
        self.quantity = 0.0002
        self.contract_id = None
        self.tick_size = None

async def main():
    # Create simple config
    config = SimpleConfig()
    
    # Initialize client
    client = BackpackClient({'ticker': config.ticker, 'quantity': config.quantity})
    client.config = config
    
    # Get contract attributes
    print("Getting contract attributes...")
    await client.get_contract_attributes()
    print(f"Contract ID: {config.contract_id}")
    print(f"Tick size: {config.tick_size}")
    
    # Test position retrieval
    print(f"\nTesting position retrieval for {config.ticker}...")
    positions_data = await client.get_account_positions()
    
    print(f"\nRaw response:")
    print(positions_data)
    
    if positions_data and 'data' in positions_data:
        if positions_data['data']:
            position_list = positions_data['data'].get('positionList', [])
            if position_list:
                for pos in position_list:
                    print(f"\n✅ Position found:")
                    print(f"   Contract: {pos.get('contractId')}")
                    print(f"   Size: {pos.get('openSize')}")
            else:
                print("\n✅ No open positions (empty list)")
        else:
            print("\n⚠️  API returned None data")
    else:
        print("\n❌ Invalid response format")

if __name__ == '__main__':
    asyncio.run(main())
