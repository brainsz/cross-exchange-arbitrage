"""
Test script to debug Extended exchange WebSocket connection with aiohttp.
"""
import asyncio
import json
import os
import traceback
from dotenv import load_dotenv
import aiohttp

load_dotenv()

async def test_orderbook_ws():
    """Test orderbook WebSocket connection via aiohttp."""
    ticker = "ETH"
    url = f"wss://stream.starknet.extended.exchange/orderbooks/{ticker}-USD?depth=1"
    
    print(f"Connecting to: {url}")
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(url, heartbeat=20) as ws:
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
    print("Testing Extended Exchange WebSocket via aiohttp")
    print("=" * 50)
    
    await test_orderbook_ws()

if __name__ == "__main__":
    asyncio.run(main())
