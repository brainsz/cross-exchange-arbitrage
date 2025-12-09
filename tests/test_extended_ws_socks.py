"""
Test script to debug Extended exchange WebSocket connection with SOCKS5 proxy.
"""
import asyncio
import json
import os
import traceback
import ssl
from dotenv import load_dotenv

load_dotenv()

# Try using websockets with explicit SOCKS5 proxy
async def test_orderbook_ws_socks():
    """Test orderbook WebSocket connection via SOCKS5 proxy."""
    try:
        from python_socks.async_.asyncio.v2 import Proxy
        import websockets
        
        ticker = "ETH"
        url = f"wss://stream.starknet.extended.exchange/orderbooks/{ticker}-USD?depth=1"
        
        print(f"Connecting to: {url}")
        print("Using SOCKS5 proxy: 127.0.0.1:7890")
        
        # Create SOCKS5 proxy
        proxy = Proxy.from_url('socks5://127.0.0.1:7890')
        
        # Get the actual host and port from URL
        host = "stream.starknet.extended.exchange"
        port = 443
        
        # Connect through proxy
        sock = await proxy.connect(dest_host=host, dest_port=port)
        
        # Create SSL context
        ssl_context = ssl.create_default_context()
        
        # Connect websocket over the proxied socket
        async with websockets.connect(
            url,
            sock=sock,
            ssl=ssl_context,
            server_hostname=host,
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
                print(f"Message preview: {json.dumps(msg, indent=2)[:300]}")
                
                count += 1
                if count >= 3:
                    print("\n✅ Successfully received 3 messages!")
                    break
                    
    except Exception as e:
        print(f"❌ SOCKS5 connection error: {e}")
        traceback.print_exc()

async def main():
    print("=" * 50)
    print("Testing Extended Exchange WebSocket via SOCKS5")
    print("=" * 50)
    
    await test_orderbook_ws_socks()

if __name__ == "__main__":
    asyncio.run(main())
