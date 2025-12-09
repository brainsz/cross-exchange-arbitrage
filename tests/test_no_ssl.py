"""
Test script with disabled SSL verification (for debugging only).
"""
import asyncio
import json
import os
import ssl
import traceback
from dotenv import load_dotenv
import aiohttp

load_dotenv()

async def test_orderbook_ws():
    """Test orderbook WebSocket connection with SSL verification disabled."""
    ticker = "ETH"
    url = f"wss://stream.starknet.extended.exchange/orderbooks/{ticker}-USD?depth=1"
    
    print(f"Connecting to: {url}")
    print("⚠️  SSL verification disabled for debugging")
    
    # Create SSL context that doesn't verify certificates
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    try:
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.ws_connect(url, heartbeat=20, ssl=ssl_context) as ws:
                print("✅ Connected to orderbook WebSocket!")
                
                count = 0
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        data = msg.data
                        
                        if data == "ping":
                            await ws.send_str("pong")
                            print("Received ping, sent pong")
                            continue
                        
                        try:
                            parsed = json.loads(data)
                        except Exception as e:
                            print(f"Failed to parse: {e}")
                            continue
                        
                        if parsed.get("type") == "PING":
                            await ws.send_str(json.dumps({"type": "PONG"}))
                            print("Received PING, sent PONG")
                            continue
                        
                        print(f"Received message type: {parsed.get('type')}")
                        print(f"Message preview: {json.dumps(parsed, indent=2)[:400]}")
                        
                        count += 1
                        if count >= 3:
                            print("\n✅ Successfully received 3 messages!")
                            break
                            
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        print(f"❌ WebSocket error: {ws.exception()}")
                        break
                        
    except Exception as e:
        print(f"❌ Connection error: {e}")
        traceback.print_exc()

async def main():
    print("=" * 50)
    print("Testing with SSL verification disabled")
    print("=" * 50)
    
    await test_orderbook_ws()

if __name__ == "__main__":
    asyncio.run(main())
