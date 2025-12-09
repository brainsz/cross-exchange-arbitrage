"""
Test using the correct SDK stream URL with SSL verification disabled.
"""
import asyncio
import json
import os
import ssl
import traceback
from dotenv import load_dotenv
import websockets

load_dotenv()

# SDK's MAINNET_CONFIG.stream_url
STREAM_URL = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1"

async def test_orderbook_ws():
    """Test orderbook WebSocket connection using SDK URL with SSL disabled."""
    ticker = "ETH"
    url = f"{STREAM_URL}/orderbooks/{ticker}-USD?depth=1"
    
    print(f"Connecting to: {url}")
    print("⚠️  SSL verification disabled for testing")
    
    # Create SSL context that doesn't verify
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    try:
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            ssl=ssl_context
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
                    print(f"Failed to parse: {e}")
                    continue
                
                if msg.get("type") == "PING":
                    await ws.send(json.dumps({"type": "PONG"}))
                    print("Received PING, sent PONG")
                    continue
                
                print(f"Received message type: {msg.get('type')}")
                print(f"Message preview: {json.dumps(msg, indent=2)[:500]}")
                
                count += 1
                if count >= 3:
                    print("\n✅ Successfully received 3 messages!")
                    break
                    
    except Exception as e:
        print(f"❌ Connection error: {e}")
        traceback.print_exc()

async def main():
    print("=" * 50)
    print("Testing with SDK stream URL (SSL disabled)")
    print("=" * 50)
    
    await test_orderbook_ws()

if __name__ == "__main__":
    asyncio.run(main())
