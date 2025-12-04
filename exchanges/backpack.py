"""
Backpack exchange client implementation with WebSocket support.
"""

import os
import asyncio
import json
import time
import base64
import traceback
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from cryptography.hazmat.primitives.asymmetric import ed25519
import websockets
from bpx.account import Account
from bpx.public import Public

from .base import BaseExchangeClient, OrderResult, OrderInfo, query_retry
from helpers.logger import TradingLogger

# Monkeypatch requests to ensure timeout
import requests.adapters
def _timeout_request_patch(func):
    def wrapper(*args, **kwargs):
        if 'timeout' not in kwargs or kwargs['timeout'] is None:
            kwargs['timeout'] = 10
        return func(*args, **kwargs)
    return wrapper

requests.adapters.HTTPAdapter.send = _timeout_request_patch(requests.adapters.HTTPAdapter.send)


class BackpackWebSocketManager:
    """WebSocket manager for Backpack order updates."""

    def __init__(self, api_key: str, api_secret: str, symbol: str, order_update_callback):
        self.api_key = api_key
        self.api_secret = api_secret
        self.symbol = symbol
        self.order_update_callback = order_update_callback
        self.websocket = None
        self.running = False
        self.ws_url = "wss://ws.backpack.exchange"
        self.logger = None

        # Initialize ED25519 private key from base64 decoded secret
        try:
            self.private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(api_secret)
            )
        except Exception as e:
            raise ValueError(f"Failed to decode API secret as ED25519 key: {e}")

    def _generate_signature(self, instruction: str, timestamp: int, window: int = 5000) -> str:
        """Generate ED25519 signature for WebSocket authentication."""
        # Create the message string in the same format as BPX package
        message = f"instruction={instruction}&timestamp={timestamp}&window={window}"

        # Sign the message using ED25519 private key
        signature_bytes = self.private_key.sign(message.encode())

        # Return base64 encoded signature
        return base64.b64encode(signature_bytes).decode()

    async def connect(self):
        """Connect to Backpack WebSocket."""
        while self.running:
            try:
                if self.logger:
                    self.logger.log("Connecting to Backpack WebSocket", "INFO")
                self.websocket = await websockets.connect(self.ws_url)

                # Subscribe to order updates for the specific symbol
                timestamp = int(time.time() * 1000)
                signature = self._generate_signature("subscribe", timestamp)

                subscribe_message = {
                    "method": "SUBSCRIBE",
                    "params": [f"account.orderUpdate.{self.symbol}"],
                    "signature": [
                        self.api_key,
                        signature,
                        str(timestamp),
                        "5000"
                    ]
                }

                await self.websocket.send(json.dumps(subscribe_message))
                if self.logger:
                    self.logger.log(f"✅ Subscribed to Backpack order updates for {self.symbol}", "INFO")

                # Start listening for messages
                await self._listen()

            except websockets.exceptions.ConnectionClosed:
                if self.logger:
                    self.logger.log("Backpack WebSocket connection closed, reconnecting...", "WARNING")
                await asyncio.sleep(2)
            except Exception as e:
                if self.logger:
                    self.logger.log(f"Backpack WebSocket error: {e}", "ERROR")
                await asyncio.sleep(2)

    async def _listen(self):
        """Listen for WebSocket messages."""
        try:
            async for message in self.websocket:
                if not self.running:
                    break

                try:
                    data = json.loads(message)
                    await self._handle_message(data)
                except json.JSONDecodeError as e:
                    if self.logger:
                        self.logger.log(f"Failed to parse WebSocket message: {e}", "ERROR")
                except Exception as e:
                    if self.logger:
                        self.logger.log(f"Error handling WebSocket message: {e}", "ERROR")
                        self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")

        except websockets.exceptions.ConnectionClosed:
            if self.logger:
                self.logger.log("Backpack WebSocket connection closed", "WARNING")
        except Exception as e:
            if self.logger:
                self.logger.log(f"Backpack WebSocket listen error: {e}", "ERROR")

    async def _handle_message(self, data: Dict[str, Any]):
        """Handle incoming WebSocket messages."""
        try:
            stream = data.get('stream', '')
            payload = data.get('data', {})

            if 'orderUpdate' in stream:
                await self._handle_order_update(payload)
            elif data.get('method') == 'SUBSCRIBE':
                # Subscription confirmation
                if self.logger:
                    self.logger.log(f"Subscription confirmed: {data}", "DEBUG")
            else:
                if self.logger:
                    self.logger.log(f"Unknown WebSocket message: {data}", "DEBUG")

        except Exception as e:
            if self.logger:
                self.logger.log(f"Error handling WebSocket message: {e}", "ERROR")

    async def _handle_order_update(self, order_data: Dict[str, Any]):
        """Handle order update messages."""
        try:
            # Call the order update callback if it exists
            if self.order_update_callback:
                await self.order_update_callback(order_data)
        except Exception as e:
            if self.logger:
                self.logger.log(f"Error in order update callback: {e}", "ERROR")
                self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")

    async def start(self):
        """Start the WebSocket connection."""
        self.running = True
        asyncio.create_task(self.connect())

    async def disconnect(self):
        """Disconnect from WebSocket."""
        self.running = False
        if self.websocket:
            await self.websocket.close()
            if self.logger:
                self.logger.log("Backpack WebSocket disconnected", "INFO")

    def set_logger(self, logger):
        """Set the logger instance."""
        self.logger = logger


class BackpackClient(BaseExchangeClient):
    """Backpack exchange client implementation with WebSocket support."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize Backpack client."""
        super().__init__(config)

        # Backpack credentials from environment
        self.api_key = os.getenv('BACKPACK_API_KEY')
        self.api_secret = os.getenv('BACKPACK_API_SECRET')
        
        if not self.api_key or not self.api_secret:
            raise ValueError("BACKPACK_API_KEY and BACKPACK_API_SECRET must be set in environment variables")

        # Initialize bpx-py clients
        self.account = Account(self.api_key, self.api_secret)
        self.public = Public()

        # Initialize logger
        self.logger = TradingLogger(exchange="backpack", ticker=self.config.ticker, log_to_console=False)

        self._order_update_handler = None
        self.ws_manager = None
        
        # Track processed filled orders to prevent duplicates
        self._processed_fills = set()
        
    def _validate_config(self) -> None:
        """Validate Backpack configuration."""
        required_env_vars = ['BACKPACK_API_KEY', 'BACKPACK_API_SECRET']
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {missing_vars}")

    async def get_contract_attributes(self) -> Tuple[str, Decimal]:
        """Get contract ID and tick size."""
        try:
            # Run in thread as bpx-py is sync
            markets = await asyncio.wait_for(
                asyncio.to_thread(self.public.get_markets),
                timeout=10
            )
            
            if isinstance(markets, dict) and 'code' in markets:
                raise Exception(f"Backpack API Error: {markets.get('code')} - {markets.get('message')}")
            
            if not isinstance(markets, list):
                raise Exception(f"Unexpected response from Backpack API: {markets}")

            for m in markets:
                if m['symbol'] == self.config.ticker:
                    tick_size = Decimal(m['filters']['price']['tickSize'])
                    min_quantity = Decimal(m['filters']['quantity']['minQuantity'])
                    
                    if self.config.quantity < min_quantity:
                        raise ValueError(f"Order quantity {self.config.quantity} < min quantity {min_quantity}")
                    
                    self.config.contract_id = self.config.ticker
                    self.config.tick_size = tick_size
                    return self.config.ticker, tick_size
            raise ValueError(f"Market {self.config.ticker} not found on Backpack")
        except Exception as e:
            self.logger.log(f"Error getting Backpack info: {e}", "ERROR")
            raise

    async def connect(self) -> None:
        """Connect to Backpack WebSocket."""
        # Initialize WebSocket manager
        self.ws_manager = BackpackWebSocketManager(
            api_key=self.api_key,
            api_secret=self.api_secret,
            symbol=self.config.contract_id,
            order_update_callback=self._handle_websocket_order_update
        )
        self.ws_manager.set_logger(self.logger)

        try:
            # Start WebSocket connection
            await self.ws_manager.start()
            # Wait a moment for connection to establish
            await asyncio.sleep(2)
        except Exception as e:
            self.logger.log(f"Error connecting to Backpack WebSocket: {e}", "ERROR")
            raise

    async def disconnect(self) -> None:
        """Disconnect from Backpack."""
        try:
            if self.ws_manager:
                await self.ws_manager.disconnect()
        except Exception as e:
            self.logger.log(f"Error during Backpack disconnect: {e}", "ERROR")

    def setup_order_update_handler(self, handler) -> None:
        """Setup order update handler for WebSocket."""
        self._order_update_handler = handler

    def get_exchange_name(self) -> str:
        """Get the exchange name."""
        return "backpack"

    async def _handle_websocket_order_update(self, order_data: Dict[str, Any]):
        """Handle order updates from WebSocket."""
        try:
            # Backpack WebSocket order update format
            event_type = order_data.get('e', '')
            order_id = order_data.get('i', '')
            symbol = order_data.get('s', '')
            side = order_data.get('S', '')
            quantity = order_data.get('q', '0')
            price = order_data.get('p', '0')
            fill_quantity = order_data.get('z', '0')
            status = order_data.get('X', '')

            # Only process orders for our symbol
            if symbol != self.config.contract_id:
                return

            # Determine order side
            if side.upper() == 'BID':
                order_side = 'buy'
            elif side.upper() == 'ASK':
                order_side = 'sell'
            else:
                self.logger.log(f"Unexpected order side: {side}", "WARNING")
                return

            # Map Backpack status to our status
            mapped_status = 'UNKNOWN'
            if status == 'FILLED' or (event_type == 'orderFill' and quantity == fill_quantity):
                mapped_status = 'FILLED'
            elif status == 'NEW' or event_type == 'orderAccepted':
                mapped_status = 'OPEN'
            elif status == 'PARTIALLY_FILLED' or event_type == 'orderFill':
                mapped_status = 'PARTIALLY_FILLED'
            elif status == 'CANCELED' or event_type in ['orderCancelled', 'orderExpired']:
                mapped_status = 'CANCELED'

            # Call the order update handler
            # Deduplicate FILLED events (Backpack may send multiple)
            if mapped_status == 'FILLED':
                if order_id in self._processed_fills:
                    self.logger.log(f"Ignoring duplicate FILLED event for order {order_id}", "DEBUG")
                    return
                self._processed_fills.add(order_id)
                # Limit set size to prevent memory growth
                if len(self._processed_fills) > 1000:
                    # Remove oldest half
                    self._processed_fills = set(list(self._processed_fills)[-500:])
            
            if self._order_update_handler and mapped_status in ['FILLED', 'PARTIALLY_FILLED']:
                # Check if handler is async
                import asyncio
                import inspect
                
                order_update = {
                    'order_id': order_id,
                    'side': order_side,
                    'status': mapped_status,
                    'size': Decimal(quantity),
                    'price': Decimal(price),
                    'contract_id': symbol,
                    'filled_size': Decimal(fill_quantity)
                }
                
                if inspect.iscoroutinefunction(self._order_update_handler):
                    await self._order_update_handler(order_update)
                else:
                    self._order_update_handler(order_update)

        except Exception as e:
            self.logger.log(f"Error handling WebSocket order update: {e}", "ERROR")
            self.logger.log(f"Order data: {order_data}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")

    @query_retry(default_return=(Decimal('0'), Decimal('0')))
    async def fetch_bbo_prices(self, contract_id: str) -> Tuple[Decimal, Decimal]:
        """Fetch best bid/offer prices."""
        try:
            # Run in thread as bpx-py is sync
            depth = await asyncio.wait_for(
                asyncio.to_thread(self.public.get_depth, contract_id),
                timeout=5
            )
            
            bids = depth.get('bids', [])
            asks = depth.get('asks', [])
            
            best_bid = Decimal(bids[0][0]) if bids else Decimal('0')
            best_ask = Decimal(asks[0][0]) if asks else Decimal('0')
            
            return best_bid, best_ask
        except Exception as e:
            self.logger.log(f"Error fetching BBO: {e}", "ERROR")
            return Decimal('0'), Decimal('0')

    async def place_open_order(self, contract_id: str, quantity: Decimal, direction: str) -> OrderResult:
        """Place an open order (post-only limit order)."""
        try:
            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)
            
            if best_bid <= 0 or best_ask <= 0:
                return OrderResult(success=False, error_message='Invalid bid/ask prices')

            # Use mid-price strategy for POST-ONLY orders
            # This keeps price close to market while still being passive
            mid_price = (best_bid + best_ask) / Decimal('2')
            
            if direction == 'buy':
                # Buy order: slightly below mid-price
                order_price = mid_price - self.config.tick_size
                side = 'Bid'
            else:
                # Sell order: slightly above mid-price
                order_price = mid_price + self.config.tick_size
                side = 'Ask'

            order_price = self.round_to_tick(order_price)

            # Place order using bpx-py
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.account.execute_order,
                    symbol=contract_id,
                    side=side,
                    order_type='Limit',
                    quantity=str(quantity),
                    price=str(order_price),
                    post_only=True,
                    time_in_force='GTC'
                ),
                timeout=10
            )

            self.logger.log(f"Place order result: {result}", "INFO")

            if isinstance(result, dict) and 'id' in result:
                order_id = str(result['id'])
                self.logger.log(f"Extracted order_id: {order_id}", "DEBUG")
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    side=direction,
                    size=quantity,
                    price=order_price,
                    status='NEW'
                )
            else:
                return OrderResult(success=False, error_message=f'Unexpected response: {result}')

        except Exception as e:
            self.logger.log(f"Error placing order: {e}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            return OrderResult(success=False, error_message=str(e))

    async def place_taker_order(self, contract_id: str, quantity: Decimal, direction: str) -> OrderResult:
        """Place a taker order (immediate execution).
        
        Uses aggressive pricing for immediate execution.
        Taker fee: 0.024%
        """
        try:
            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)
            
            if best_bid <= 0 or best_ask <= 0:
                return OrderResult(success=False, error_message='Invalid bid/ask prices')

            # Aggressive pricing for immediate execution
            if direction == 'buy':
                order_price = best_ask  # Buy at ask (taker)
                side = 'Bid'
            else:
                order_price = best_bid  # Sell at bid (taker)
                side = 'Ask'

            order_price = self.round_to_tick(order_price)

            # Place order without post_only (allows taker)
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.account.execute_order,
                    symbol=contract_id,
                    side=side,
                    order_type='Limit',
                    quantity=str(quantity),
                    price=str(order_price),
                    post_only=False,
                    time_in_force='IOC'
                ),
                timeout=10
            )

            self.logger.log(f"Place taker order result: {result}", "INFO")

            if isinstance(result, dict) and 'id' in result:
                order_id = str(result['id'])
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    side=direction,
                    size=quantity,
                    price=order_price,
                    status='NEW'
                )
            else:
                return OrderResult(success=False, error_message=f'Unexpected response: {result}')

        except Exception as e:
            self.logger.log(f"Error placing taker order: {e}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            return OrderResult(success=False, error_message=str(e))


    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an order."""
        try:
            self.logger.log(f"Attempting to cancel order {order_id}", "INFO")
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.account.cancel_order,
                    symbol=self.config.contract_id,
                    order_id=order_id
                ),
                timeout=10
            )

            if result and 'id' in result:
                self.logger.log(f"Successfully canceled order {order_id}", "INFO")
                return OrderResult(success=True)
            else:
                return OrderResult(success=False, error_message=f'Cancel failed: {result}')

        except Exception as e:
            self.logger.log(f"Error canceling order: {e}", "ERROR")
            return OrderResult(success=False, error_message=str(e))

    @query_retry()
    async def get_order_info(self, order_id: str) -> Optional[OrderInfo]:
        """Get order information. Returns None if order not found (to allow retries)."""
        try:
            # First check open orders
            open_order = await asyncio.wait_for(
                asyncio.to_thread(
                    self.account.get_open_order,
                    symbol=self.config.contract_id,
                    order_id=order_id
                ),
                timeout=5
            )

            if open_order and isinstance(open_order, dict) and 'id' in open_order:
                status = open_order.get('status', 'UNKNOWN')
                if status == 'New':
                    status = 'OPEN'
                elif status == 'Filled':
                    status = 'FILLED'
                elif status == 'PartiallyFilled':
                    status = 'PARTIALLY_FILLED'
                elif status in ['Canceled', 'Expired']:
                    status = 'CANCELED'

                return OrderInfo(
                    order_id=str(open_order.get('id')),
                    side=open_order.get('side', '').lower(),
                    size=Decimal(open_order.get('quantity', 0)),
                    price=Decimal(open_order.get('price', 0)),
                    status=status,
                    filled_size=Decimal(open_order.get('executedQuantity', 0))
                )

            # If not in open orders, check order history
            try:
                orders = await asyncio.wait_for(
                    asyncio.to_thread(
                        self.account.get_order_history,
                        symbol=self.config.contract_id,
                        limit=100
                    ),
                    timeout=5
                )
            except AttributeError:
                # Fallback to get_fill_history if order history is also missing or different
                self.logger.log(f"AttributeError: Account object missing get_order_history. Available: {dir(self.account)}", "ERROR")
                # Try get_fill_history as fallback
                try:
                     fills = await asyncio.wait_for(
                        asyncio.to_thread(
                            self.account.get_fill_history,
                            symbol=self.config.contract_id,
                            limit=100
                        ),
                        timeout=5
                    )
                     # Construct order info from fills
                     filled_amount = Decimal('0')
                     found_fill = False
                     last_price = Decimal('0')
                     side = ''
                     for fill in fills:
                        fill_order_id = fill.get('orderId') or fill.get('order_id')
                        if str(fill_order_id) == str(order_id):
                            found_fill = True
                            filled_amount += Decimal(fill.get('quantity', 0))
                            last_price = Decimal(fill.get('price', 0))
                            side = fill.get('side', '').lower()
                     
                     if found_fill:
                        return OrderInfo(
                            order_id=order_id,
                            side=side,
                            size=filled_amount,
                            price=last_price,
                            status='FILLED',
                            filled_size=filled_amount
                        )
                     return None

                except Exception as e:
                     self.logger.log(f"Error in fallback get_fill_history: {e}", "ERROR")
                     raise

            for o in orders:
                # Ensure we compare strings to avoid int/str mismatch
                history_order_id = str(o.get('id'))
                target_order_id = str(order_id)
                
                if history_order_id == target_order_id:
                    status = o.get('status', 'UNKNOWN')
                    # Map Backpack status to our status
                    if status == 'Filled':
                        status = 'FILLED'
                    elif status == 'New':
                        status = 'OPEN'
                    elif status == 'PartiallyFilled':
                        status = 'PARTIALLY_FILLED'
                    elif status == 'Canceled':
                        status = 'CANCELED'
                    elif status == 'Expired':
                        status = 'CANCELED'
                        
                    return OrderInfo(
                        order_id=history_order_id,
                        side=o.get('side').lower(),
                        size=Decimal(o.get('quantity')),
                        price=Decimal(o.get('price')),
                        status=status,
                        filled_size=Decimal(o.get('executedQuantity', 0))
                    )
            
            # Debug logging if not found
            self.logger.log(f"Order {order_id} not found in history. History IDs: {[o.get('id') for o in orders[:5]]}...", "WARNING")
            
            # If not found in history either, return None to indicate "unknown/pending" 
            # so the OrderManager keeps polling until timeout.
            return None

            return None
        except Exception as e:
            self.logger.log(f"Error getting order info: {e}", "ERROR")
            raise

    async def place_close_order(self, contract_id: str, quantity: Decimal, price: Decimal, side: str, post_only: bool = True) -> OrderResult:
        """Place a close order (limit order to close position)."""
        try:
            # Determine Backpack side
            if side.lower() == 'buy':
                backpack_side = 'Bid'
            elif side.lower() == 'sell':
                backpack_side = 'Ask'
            else:
                return OrderResult(success=False, error_message=f'Invalid side: {side}')

            order_price = self.round_to_tick(price)

            # Place order using bpx-py
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    self.account.execute_order,
                    symbol=contract_id,
                    side=backpack_side,
                    order_type='Limit',
                    quantity=str(quantity),
                    price=str(order_price),
                    post_only=post_only,
                    time_in_force='GTC'
                ),
                timeout=10
            )

            if isinstance(result, dict) and 'id' in result:
                order_id = str(result['id'])
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    side=side,
                    size=quantity,
                    price=order_price,
                    status='NEW'
                )
            else:
                return OrderResult(success=False, error_message=f'Unexpected response: {result}')

        except Exception as e:
            self.logger.log(f"Error placing close order: {e}", "ERROR")
            return OrderResult(success=False, error_message=str(e))

    @query_retry(default_return=[])
    async def get_active_orders(self, contract_id: str) -> List[OrderInfo]:
        """Get active orders for a contract."""
        try:
            orders = await asyncio.wait_for(
                asyncio.to_thread(
                    self.account.get_open_orders,
                    symbol=contract_id
                ),
                timeout=10
            )

            if not orders:
                return []

            result = []
            for order in orders:
                if isinstance(order, dict):
                    side = 'buy' if order.get('side') == 'Bid' else 'sell'
                    status = order.get('status', 'UNKNOWN')
                    if status == 'New':
                        status = 'OPEN'
                    elif status == 'Filled':
                        status = 'FILLED'
                    elif status == 'PartiallyFilled':
                        status = 'PARTIALLY_FILLED'

                    result.append(OrderInfo(
                        order_id=str(order.get('id')),
                        side=side,
                        size=Decimal(order.get('quantity', 0)),
                        price=Decimal(order.get('price', 0)),
                        status=status,
                        filled_size=Decimal(order.get('executedQuantity', 0))
                    ))

            return result

        except Exception as e:
            self.logger.log(f"Error getting active orders: {e}", "ERROR")
            return []

    @query_retry(default_return=Decimal('0'))
    async def get_account_positions(self) -> Decimal:
        """Get account positions for futures contracts.
        
        Returns the position size as Decimal (matching EdgeX/Lighter interface).
        """
        try:
            import requests
            import time
            import base64
            from urllib.parse import urlencode
            
            # Backpack positions endpoint
            url = "https://api.backpack.exchange/api/v1/positions"
            
            # Generate signature for authentication
            timestamp = int(time.time() * 1000)
            window = 5000
            instruction = "positionQuery"
            
            # Try with symbol parameter first
            params = {'symbol': self.config.contract_id}
            query_string = urlencode(params)
            message = f"instruction={instruction}&{query_string}&timestamp={timestamp}&window={window}"
            
            # Sign with ED25519
            private_key = ed25519.Ed25519PrivateKey.from_private_bytes(
                base64.b64decode(self.api_secret)
            )
            signature_bytes = private_key.sign(message.encode())
            signature = base64.b64encode(signature_bytes).decode()
            
            # Set headers
            headers = {
                'X-API-Key': self.api_key,
                'X-Signature': signature,
                'X-Timestamp': str(timestamp),
                'X-Window': str(window),
                'Content-Type': 'application/json'
            }
            
            # Make request
            response = await asyncio.wait_for(
                asyncio.to_thread(
                    requests.get,
                    url,
                    headers=headers,
                    params=params,
                    timeout=10
                ),
                timeout=15
            )
            
            # Handle 404 - no positions found
            if response.status_code == 404:
                self.logger.log("No open positions found (404)", "DEBUG")
                return Decimal('0')
            
            if response.status_code == 200:
                positions = response.json()
                
                # Extract position size
                if isinstance(positions, list):
                    if len(positions) > 0:
                        # Find position for our symbol
                        for pos in positions:
                            if pos.get('symbol') == self.config.contract_id:
                                # Return the net quantity (signed position size)
                                net_qty = pos.get('netQuantity', '0')
                                return Decimal(net_qty) if net_qty else Decimal('0')
                    # No position found for this symbol
                    return Decimal('0')
                else:
                    # Empty or unexpected response
                    return Decimal('0')
            else:
                self.logger.log(f"Error getting positions: HTTP {response.status_code} - {response.text}", "WARNING")
                return Decimal('0')
                
        except Exception as e:
            self.logger.log(f"Error getting positions: {e}", "WARNING")
            import traceback
            self.logger.log(f"Traceback: {traceback.format_exc()}", "DEBUG")
            return Decimal('0')
