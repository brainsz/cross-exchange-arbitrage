"""
Test script to debug Extended exchange WebSocket connection.
"""
import asyncio
import websockets
import json
import os
import traceback
from dotenv import load_dotenv

load_dotenv()

async def test_orderbook_ws():
    """Test orderbook WebSocket connection."""
    ticker = "ETH"
    url = f"wss://stream.starknet.extended.exchange/orderbooks/{ticker}-USD?depth=1"
    
    print(f"Connecting to: {url}")
    
    try:
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20
        ) as ws:
            print("✅ Connected to orderbook WebSocket!")
            
            count = 0
            async for raw in ws:
                if raw == "ping":
                    await ws.send("pong")
                    print("Received ping, sent pong")
                    continue
                    
                try:
                    msg = json.loads(raw)
                except Exception as e:
                    print(f"Failed to parse message: {e}")
                    continue
                
                if msg.get("type") == "PING":
                    await ws.send(json.dumps({"type": "PONG"}))
                    print("Received PING, sent PONG")
                    continue
                
                print(f"Received message type: {msg.get('type')}")
                print(f"Message: {json.dumps(msg, indent=2)[:500]}")
                
                count += 1
                if count >= 5:
                    print("\n✅ Successfully received 5 messages!")
                    break
                    
    except Exception as e:
        print(f"❌ Connection error: {e}")
        traceback.print_exc()

async def test_account_ws():
    """Test account WebSocket connection."""
    api_key = os.getenv('EXTENDED_API_KEY')
    if not api_key:
        print("❌ EXTENDED_API_KEY not set!")
        return
        
    url = "wss://stream.starknet.extended.exchange/account"
    
    print(f"\nConnecting to: {url}")
    
    try:
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            extra_headers=[("X-API-Key", api_key)]
        ) as ws:
            print("✅ Connected to account WebSocket!")
            
            # Wait for a few seconds to see if we get any messages
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                print(f"Received: {msg[:200] if len(msg) > 200 else msg}")
            except asyncio.TimeoutError:
                print("No messages received in 5 seconds (this is normal if no orders)")
                    
    except Exception as e:
        print(f"❌ Connection error: {e}")

async def main():
    print("=" * 50)
    print("Testing Extended Exchange WebSocket Connections")
    print("=" * 50)
    
    await test_orderbook_ws()
    await test_account_ws()

if __name__ == "__main__":
    asyncio.run(main())
