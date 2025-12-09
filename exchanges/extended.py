"""
Extended exchange client implementation for arbitrage.
Uses IOC (Immediate or Cancel) orders for quick execution in low-liquidity environments.
"""

from typing import Dict, Any, List, Optional, Tuple
from decimal import Decimal, ROUND_HALF_UP

from .base import BaseExchangeClient, OrderResult, OrderInfo
from helpers.logger import TradingLogger

from x10.perpetual.trading_client import PerpetualTradingClient
from x10.perpetual.configuration import MAINNET_CONFIG
from x10.perpetual.accounts import StarkPerpetualAccount
from x10.perpetual.orders import TimeInForce, OrderSide

import websockets
import time
import json
import traceback
import asyncio
import aiohttp
import os
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

load_dotenv()

import ssl


async def _stream_worker(
    url: str,
    handler,
    stop_event: asyncio.Event,
    extra_headers: dict | list[tuple[str, str]] | None = None,
):
    """WebSocket stream worker with auto-reconnect."""
    # Create SSL context that doesn't verify certificates (for proxy environments)
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    
    while not stop_event.is_set():
        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                ping_timeout=20,
                extra_headers=extra_headers,
                ssl=ssl_context
            ) as ws:
                print(f"✅ Connected to {url}")
                async for raw in ws:
                    if raw == "ping":
                        await ws.send("pong")
                        continue
                    try:
                        msg = json.loads(raw)
                    except Exception:
                        continue

                    if msg.get("type") == "PING":
                        await ws.send(json.dumps({"type": "PONG"}))
                        continue

                    await handler(msg)

        except Exception as e:
            print(f"❌ {url} error: {e}")
            await asyncio.sleep(3)


def utc_now():
    return datetime.now(tz=timezone.utc)


class ExtendedClient(BaseExchangeClient):
    """Extended exchange client implementation with IOC order support."""

    def __init__(self, config: Dict[str, Any]):
        """Initialize the exchange client with configuration."""
        super().__init__(config)

        vault = os.getenv('EXTENDED_VAULT')
        private_key = os.getenv('EXTENDED_STARK_KEY_PRIVATE')
        public_key = os.getenv('EXTENDED_STARK_KEY_PUBLIC')
        api_key = os.getenv('EXTENDED_API_KEY')
        self.api_key = api_key

        self.stark_account = StarkPerpetualAccount(
            vault=vault,
            private_key=private_key,
            public_key=public_key,
            api_key=api_key
        )
        self.stark_config = MAINNET_CONFIG
        self.perpetual_trading_client = PerpetualTradingClient(
            self.stark_config, self.stark_account
        )

        # Initialize logger
        self.logger = TradingLogger(
            exchange="extended",
            ticker=self.config.ticker,
            log_to_console=True
        )
        self._order_update_handler = None

        self.orderbook = None

        # For websocket
        self._stop_event = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

        # Maintain open order dict
        self.open_orders = {}
        self.min_order_size = Decimal('0.0001')

    def _validate_config(self) -> None:
        """Validate the exchange-specific configuration."""
        required_env_vars = [
            'EXTENDED_VAULT',
            'EXTENDED_STARK_KEY_PRIVATE',
            'EXTENDED_STARK_KEY_PUBLIC',
            'EXTENDED_API_KEY'
        ]
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {missing_vars}")

    async def connect(self) -> None:
        """Connect to the exchange (WebSocket, etc.)."""
        self._stop_event.clear()

        host = MAINNET_CONFIG.stream_url
        self._tasks = [
            # Account update stream (for order updates)
            asyncio.create_task(_stream_worker(
                host + "/account",
                self.handle_account,
                self._stop_event,
                extra_headers=[("X-API-Key", self.api_key)]
            )),
            # Orderbook update stream
            asyncio.create_task(_stream_worker(
                host + "/orderbooks/" + self.config.ticker + "-USD" + "?depth=1",
                self.handle_orderbook,
                self._stop_event
            )),
        ]
        self.logger.log("Streams started", "INFO")

    async def disconnect(self) -> None:
        """Disconnect from the exchange gracefully."""
        try:
            self.logger.log("Starting graceful disconnect from Extended exchange", "INFO")

            # Stop WebSocket streams
            self._stop_event.set()
            for t in self._tasks:
                t.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self.logger.log("Streams stopped", "INFO")

            # Close the main client connection
            if hasattr(self, 'perpetual_trading_client') and self.perpetual_trading_client:
                try:
                    await self.perpetual_trading_client.close()
                    self.logger.log("Main client connection closed", "INFO")
                except Exception as e:
                    self.logger.log(f"Error closing main client: {e}", "WARNING")

            # Reset internal state
            self.orderbook = None
            self._order_update_handler = None

            self.logger.log("Extended exchange disconnected successfully", "INFO")

        except Exception as e:
            self.logger.log(f"Error during Extended disconnect: {e}", "ERROR")
            self.logger.log(f"Traceback: {traceback.format_exc()}", "ERROR")
            raise

    async def fetch_bbo_prices(self, contract_id: str) -> tuple[Decimal, Decimal]:
        """Fetch best bid and offer prices from orderbook."""
        try:
            orderbook = self.orderbook

            if orderbook is None:
                self.logger.log(
                    f"Error fetching BBO prices for {contract_id}: orderbook is None",
                    level="ERROR"
                )
                return Decimal('0'), Decimal('0')

            # Get best bid (highest bid price)
            best_bid = Decimal('0')
            if orderbook["bid"] and len(orderbook["bid"]) > 0:
                best_bid = Decimal(orderbook["bid"][0]["p"])

            # Get best ask (lowest ask price)
            best_ask = Decimal('0')
            if orderbook["ask"] and len(orderbook["ask"]) > 0:
                best_ask = Decimal(orderbook["ask"][0]["p"])

            return best_bid, best_ask

        except Exception as e:
            self.logger.log(
                f"Error fetching BBO prices for {contract_id}: {str(e)}",
                level="ERROR"
            )
            return Decimal('0'), Decimal('0')

    async def place_open_order(
        self,
        contract_id: str,
        quantity: Decimal,
        direction: str
    ) -> OrderResult:
        """
        Place an open order with Extended using IOC (Immediate or Cancel).
        This is optimized for arbitrage - if there's no immediate match, cancel.
        """
        max_retries = 5
        retry_count = 0

        while retry_count < max_retries:
            try:
                if self.orderbook is None:
                    await asyncio.sleep(0.5)
                    self.logger.log("Orderbook not ready, waiting...", level="INFO")
                    retry_count += 1
                    continue

                best_bid, best_ask = await self.fetch_bbo_prices(contract_id)

                if best_bid <= 0 or best_ask <= 0:
                    return OrderResult(success=False, error_message='Invalid bid/ask prices')

                if direction == 'buy':
                    # For buy orders: place at best ask price to match immediately
                    order_price = best_ask
                    side = OrderSide.BUY
                else:
                    # For sell orders: place at best bid price to match immediately
                    order_price = best_bid
                    side = OrderSide.SELL

                # Round price to appropriate precision
                rounded_price = self.round_to_tick(order_price)

                # Place IOC order - will fill immediately or be cancelled
                order_result = await self.perpetual_trading_client.place_order(
                    market_name=contract_id,
                    amount_of_synthetic=quantity,
                    price=rounded_price,
                    side=side,
                    time_in_force=TimeInForce.IOC,  # IOC for immediate execution
                    post_only=False,  # Not post-only, we want to take
                    expire_time=utc_now() + timedelta(minutes=1),
                )

                if not order_result or not order_result.data or order_result.status != 'OK':
                    return OrderResult(success=False, error_message='Failed to place order')

                order_id = order_result.data.id
                if not order_id:
                    return OrderResult(success=False, error_message='No order ID in response')

                # Check order status
                await asyncio.sleep(0.1)
                order_info = await self.get_order_info(order_id)

                if order_info:
                    if order_info.status == 'FILLED':
                        return OrderResult(
                            success=True,
                            order_id=order_id,
                            side=side.value,
                            size=quantity,
                            price=rounded_price,
                            status='FILLED',
                            filled_size=order_info.filled_size
                        )
                    elif order_info.status in ['CANCELED', 'REJECTED']:
                        # IOC order didn't match - this is expected sometimes
                        self.logger.log(
                            f"IOC order not filled (status: {order_info.status}), no liquidity at {rounded_price}",
                            level="INFO"
                        )
                        return OrderResult(
                            success=False,
                            error_message=f'IOC order not filled - no liquidity at {rounded_price}'
                        )
                    elif order_info.status == 'PARTIALLY_FILLED':
                        # Partial fill is still a success
                        return OrderResult(
                            success=True,
                            order_id=order_id,
                            side=side.value,
                            size=quantity,
                            price=rounded_price,
                            status='PARTIALLY_FILLED',
                            filled_size=order_info.filled_size
                        )
                    else:
                        return OrderResult(
                            success=False,
                            error_message=f'Unexpected order status: {order_info.status}'
                        )
                else:
                    # Can't verify, assume success
                    self.logger.log(
                        f"Could not verify order {order_id}, assuming filled",
                        level="WARNING"
                    )
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        side=side.value,
                        size=quantity,
                        price=rounded_price
                    )

            except Exception as e:
                if retry_count < max_retries - 1:
                    retry_count += 1
                    await asyncio.sleep(0.1)
                    continue
                else:
                    return OrderResult(success=False, error_message=str(e))

        return OrderResult(success=False, error_message='Max retries exceeded')

    async def place_close_order(
        self,
        contract_id: str,
        quantity: Decimal,
        price: Decimal,
        side: str
    ) -> OrderResult:
        """Place a close order with Extended using GTT (Good Till Time)."""
        try:
            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)

            if best_bid <= 0 or best_ask <= 0:
                return OrderResult(success=False, error_message='Invalid bid/ask prices')

            order_side = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL

            # Adjust price to be maker
            adjusted_price = price
            if side.lower() == 'sell':
                if price <= best_bid:
                    adjusted_price = best_bid + self.config.tick_size
            elif side.lower() == 'buy':
                if price >= best_ask:
                    adjusted_price = best_ask - self.config.tick_size

            rounded_price = self.round_to_tick(adjusted_price)
            quantity = quantity.quantize(self.min_order_size, rounding=ROUND_HALF_UP)

            order_result = await self.perpetual_trading_client.place_order(
                market_name=contract_id,
                amount_of_synthetic=quantity,
                price=rounded_price,
                side=order_side,
                time_in_force=TimeInForce.GTT,
                post_only=True,
                expire_time=utc_now() + timedelta(days=90),
            )

            if not order_result or not order_result.data or order_result.status != 'OK':
                return OrderResult(success=False, error_message='Failed to place order')

            order_id = order_result.data.id
            return OrderResult(
                success=True,
                order_id=order_id,
                side=side,
                size=quantity,
                price=rounded_price,
                status='OPEN'
            )

        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an order."""
        try:
            cancel_result = await self.perpetual_trading_client.orders.cancel_order(order_id)

            if not cancel_result or not cancel_result.data:
                self.logger.log(f"Failed to cancel order {order_id}", level="ERROR")
                return OrderResult(success=False, error_message='Failed to cancel order')

            await asyncio.sleep(0.1)
            order_info = await self.get_order_info(order_id)
            filled_size = Decimal('0')
            if order_info:
                filled_size = order_info.filled_size

            return OrderResult(success=True, filled_size=filled_size)

        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    async def get_order_info(self, order_id: str) -> Optional[OrderInfo]:
        """Get order information using REST API."""
        url = f"https://api.starknet.extended.exchange/api/v1/user/orders/{order_id}"
        headers = {
            "X-Api-Key": self.api_key,
            "User-Agent": "ArbitrageBot"
        }

        for attempt in range(10):
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()

                            if data.get("status") != "OK" or not data.get("data"):
                                return None

                            order_data = data["data"]

                            # Convert status
                            status = order_data.get("status", "")
                            if status == "NEW":
                                status = "OPEN"
                            elif status == "CANCELLED":
                                status = "CANCELED"

                            return OrderInfo(
                                order_id=str(order_data.get("id", "")),
                                side=order_data.get("side", "").lower(),
                                size=Decimal(order_data.get("qty", "0")) - Decimal(order_data.get("filledQty", "0")),
                                price=Decimal(order_data.get("price", "0")),
                                status=status,
                                filled_size=Decimal(order_data.get("filledQty", "0")),
                                remaining_size=Decimal(order_data.get("qty", "0")) - Decimal(order_data.get("filledQty", "0"))
                            )

                        elif response.status == 404:
                            return None

            except Exception as e:
                self.logger.log(f"Error getting order info: {str(e)}", "ERROR")

            await asyncio.sleep(0.3)

        return None

    async def get_active_orders(self, contract_id: str) -> List[OrderInfo]:
        """Get active orders for a contract."""
        try:
            active_orders = await self.perpetual_trading_client.account.get_open_orders(
                market_names=[contract_id]
            )

            if not active_orders or not hasattr(active_orders, 'data'):
                return []

            contract_orders = []
            for order in active_orders.data:
                if order.market == contract_id:
                    order_status = 'OPEN' if order.status == 'NEW' else order.status

                    contract_orders.append(OrderInfo(
                        order_id=order.id,
                        side=order.side.lower(),
                        size=Decimal(order.qty) - Decimal(order.filled_qty),
                        price=Decimal(order.price),
                        status=order_status,
                        filled_size=Decimal(order.filled_qty),
                        remaining_size=Decimal(order.qty) - Decimal(order.filled_qty)
                    ))

            return contract_orders

        except Exception as e:
            self.logger.log(f"Error getting active orders: {e}", "ERROR")
            return []

    async def get_account_positions(self) -> Decimal:
        """Get account positions."""
        try:
            positions_data = await self.perpetual_trading_client.account.get_positions(
                market_names=[self.config.ticker + "-USD"]
            )

            if not positions_data or not hasattr(positions_data, 'data'):
                return Decimal('0')

            positions = positions_data.data
            if positions:
                for p in positions:
                    if p.market == self.config.contract_id:
                        return Decimal(p.size)

            return Decimal('0')

        except Exception as e:
            self.logger.log(f"Error getting positions: {e}", "ERROR")
            return Decimal('0')

    async def handle_account(self, message):
        """Handle order updates from WebSocket."""
        try:
            if isinstance(message, str):
                message = json.loads(message)

            event = message.get("type", "")
            if event == "ORDER":
                data = message.get('data', {})
                orders = data.get('orders', [])

                for order in orders:
                    if order.get('market') != self.config.contract_id:
                        continue

                    order_id = order.get('id')
                    status = order.get('status')
                    side = order.get('side', '').lower()
                    filled_size = order.get('filledQty')

                    if side == self.config.close_order_side:
                        order_type = "CLOSE"
                    else:
                        order_type = "OPEN"

                    # Normalize status
                    if status == "NEW":
                        status = "OPEN"
                    elif status == "CANCELLED":
                        status = "CANCELED"

                    # Maintain open orders dict
                    if status in ["OPEN", "PARTIALLY_FILLED"]:
                        self.open_orders[order_id] = order
                    elif status in ["CANCELED", "FILLED"]:
                        self.open_orders.pop(order_id, None)

                    if status in ['OPEN', 'PARTIALLY_FILLED', 'FILLED', 'CANCELED']:
                        if self._order_update_handler:
                            self._order_update_handler({
                                'order_id': order_id,
                                'side': side,
                                'order_type': order_type,
                                'status': status,
                                'size': order.get('qty'),
                                'price': order.get('price'),
                                'contract_id': order.get('market'),
                                'filled_size': filled_size
                            })

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.log(f"Error handling order update: {e}", "ERROR")

    def setup_order_update_handler(self, handler) -> None:
        """Setup order update handler for WebSocket."""
        self._order_update_handler = handler

    async def handle_orderbook(self, message):
        """Handle orderbook updates from WebSocket."""
        try:
            if isinstance(message, str):
                message = json.loads(message)

            event = message.get("type", "")
            if event == "SNAPSHOT":
                data = message.get('data', {})
                market = data.get('m', '')
                bids = data.get('b', [])
                asks = data.get('a', [])

                self.orderbook = {
                    'market': market,
                    'bid': bids,
                    'ask': asks
                }

                self.logger.log(
                    f"Orderbook updated for {market}: "
                    f"bid={bids[0] if bids else 'N/A'}, ask={asks[0] if asks else 'N/A'}",
                    "DEBUG"
                )

        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.log(f"Error handling orderbook update: {e}", "ERROR")

    def get_exchange_name(self) -> str:
        """Get the exchange name."""
        return "extended"

    async def get_contract_attributes(self) -> Tuple[str, Decimal]:
        """Get contract ID and tick size for the configured ticker."""
        ticker = self.config.ticker
        if len(ticker) == 0:
            self.logger.log("Ticker is empty", "ERROR")
            raise ValueError("Ticker is empty")

        # Create the market name
        self.config.contract_id = ticker + "-USD"

        # Fetch market information
        market_information = await self.perpetual_trading_client.markets_info.get_markets(
            market_names=[self.config.contract_id]
        )

        if not market_information or not hasattr(market_information, 'data') or len(market_information.data) == 0:
            self.logger.log(f"Failed to get market information for {self.config.contract_id}", "ERROR")
            raise ValueError(f"Failed to get market information for {self.config.contract_id}")

        # Get min order size and tick size
        min_quantity = Decimal(str(market_information.data[0].trading_config.min_order_size))
        self.min_order_size = min_quantity

        if self.config.quantity < min_quantity:
            self.logger.log(
                f"Order quantity is less than min quantity: {self.config.quantity} < {min_quantity}",
                "ERROR"
            )
            raise ValueError(f"Order quantity is less than min quantity: {self.config.quantity} < {min_quantity}")

        self.config.tick_size = Decimal(str(market_information.data[0].trading_config.min_price_change))

        return self.config.contract_id, self.config.tick_size

    async def get_order_price(self, direction: str) -> Decimal:
        """Get the price for an order based on direction."""
        best_bid, best_ask = await self.fetch_bbo_prices(self.config.contract_id)
        if best_bid <= 0 or best_ask <= 0:
            self.logger.log("Invalid bid/ask prices", "ERROR")
            raise ValueError("Invalid bid/ask prices")

        if direction == 'buy':
            # For buy IOC orders, price at best ask for immediate match
            order_price = best_ask
        else:
            # For sell IOC orders, price at best bid for immediate match
            order_price = best_bid

        return self.round_to_tick(order_price)
