"""
Backpack exchange client implementation using bpx-py.
"""

import os
import asyncio
import traceback
from decimal import Decimal
from typing import Dict, Any, List, Optional, Tuple
from bpx.account import Account
from bpx.public import Public

from .base import BaseExchangeClient, OrderResult, OrderInfo, query_retry
from helpers.logger import TradingLogger


class BackpackClient(BaseExchangeClient):
    """Backpack exchange client implementation."""

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
        
        # WebSocket management is handled differently in bpx-py (it might not expose a direct WS manager like EdgeX SDK)
        # We might need to implement a custom WS wrapper or use what bpx-py provides if it supports async WS.
        # Checking bpx-py documentation/source would be ideal, but assuming standard REST for now for critical ops
        # and we will check if bpx-py has WS support. 
        # Note: bpx-py seems to be synchronous for REST. We might need to wrap in asyncio.to_thread for async compatibility.
        
    def _validate_config(self) -> None:
        """Validate Backpack configuration."""
        required_env_vars = ['BACKPACK_API_KEY', 'BACKPACK_API_SECRET']
        missing_vars = [var for var in required_env_vars if not os.getenv(var)]
        if missing_vars:
            raise ValueError(f"Missing required environment variables: {missing_vars}")

    async def get_contract_attributes(self) -> Tuple[str, Decimal]:
        """Get contract ID and tick size."""
        # For Backpack, the contract_id is the symbol itself, e.g. "SOL_USDC"
        # We need to fetch tick size from public API
        try:
            # Run in thread as bpx-py is sync
            markets = await asyncio.to_thread(self.public.get_markets)
            
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
        """Connect to the exchange."""
        # bpx-py REST client doesn't need explicit connection.
        # If we implement WS later, we'll add it here.
        self.logger.log("Backpack client initialized", "INFO")

    async def disconnect(self) -> None:
        """Disconnect from the exchange."""
        pass

    def get_exchange_name(self) -> str:
        return "backpack"

    def setup_order_update_handler(self, handler) -> None:
        """Setup order update handler."""
        # TODO: Implement WebSocket support for real-time updates
        self._order_update_handler = handler

    @query_retry(default_return=(Decimal('0'), Decimal('0')))
    async def fetch_bbo_prices(self, contract_id: str) -> Tuple[Decimal, Decimal]:
        """Fetch Best Bid and Offer prices."""
        # contract_id in Backpack is the symbol, e.g., "SOL_USDC"
        try:
            # Run synchronous call in thread
            depth = await asyncio.to_thread(self.public.get_depth, contract_id)
            
            bids = depth.get('bids', [])
            asks = depth.get('asks', [])
            
            best_bid = Decimal(bids[-1][0]) if bids else Decimal('0') # Bids are sorted ascending? usually descending. 
            # bpx-py returns bids sorted? Let's assume standard order book response:
            # bids: [[price, size], ...] usually high to low
            # asks: [[price, size], ...] usually low to high
            # Need to verify bpx-py return format. 
            # Assuming bids[0] is best bid if sorted high-to-low.
            # Actually, let's check standard API behavior.
            
            # Re-checking bpx-py or standard Backpack API:
            # Bids are usually sorted high to low. Asks low to high.
            best_bid = Decimal(bids[0][0]) if bids else Decimal('0')
            best_ask = Decimal(asks[0][0]) if asks else Decimal('0')
            
            return best_bid, best_ask
        except Exception as e:
            self.logger.log(f"Error fetching BBO: {e}", "ERROR")
            return Decimal('0'), Decimal('0')

    async def place_open_order(self, contract_id: str, quantity: Decimal, direction: str) -> OrderResult:
        """Place an open order."""
        try:
            side = 'Bid' if direction.lower() == 'buy' else 'Ask'
            
            # Get current price to place maker order
            best_bid, best_ask = await self.fetch_bbo_prices(contract_id)
            if best_bid == 0 or best_ask == 0:
                return OrderResult(success=False, error_message="Invalid BBO")

            if direction.lower() == 'buy':
                price = best_ask - self.config.tick_size
            else:
                price = best_bid + self.config.tick_size
                
            price = self.round_to_tick(price)
            
            # Execute order
            result = await asyncio.to_thread(
                self.account.execute_order,
                symbol=contract_id,
                side=side,
                order_type='Limit',
                quantity=str(quantity),
                price=str(price),
                post_only=True
            )
            
            return OrderResult(
                success=True,
                order_id=result.get('id'),
                side=direction,
                size=quantity,
                price=price,
                status='OPEN' # Optimistic status
            )
        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    async def place_close_order(self, contract_id: str, quantity: Decimal, price: Decimal, side: str) -> OrderResult:
        """Place a close order."""
        try:
            bp_side = 'Bid' if side.lower() == 'buy' else 'Ask'
            
            # Execute order
            result = await asyncio.to_thread(
                self.account.execute_order,
                symbol=contract_id,
                side=bp_side,
                order_type='Limit',
                quantity=str(quantity),
                price=str(price),
                post_only=True
            )
            
            return OrderResult(
                success=True,
                order_id=result.get('id'),
                side=side,
                size=quantity,
                price=price,
                status='OPEN'
            )
        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    async def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel an order."""
        try:
            # Backpack API requires symbol to cancel? bpx-py cancel_order takes symbol and order_id
            # We need to store symbol/contract_id in config or pass it.
            await asyncio.to_thread(
                self.account.cancel_order,
                symbol=self.config.contract_id,
                order_id=order_id
            )
            return OrderResult(success=True)
        except Exception as e:
            return OrderResult(success=False, error_message=str(e))

    async def get_order_info(self, order_id: str) -> Optional[OrderInfo]:
        """Get order info."""
        try:
            # bpx-py might not have direct get_order. It has get_open_orders and get_history.
            # Let's try to find it in open orders first.
            orders = await asyncio.to_thread(
                self.account.get_open_orders,
                symbol=self.config.contract_id
            )
            
            for o in orders:
                if o.get('id') == order_id:
                    return OrderInfo(
                        order_id=o.get('id'),
                        side=o.get('side').lower(),
                        size=Decimal(o.get('quantity')),
                        price=Decimal(o.get('price')),
                        status='OPEN', # If in open orders, it's open
                        filled_size=Decimal(o.get('executedQuantity', 0))
                    )
            return None
        except Exception:
            return None

    async def get_active_orders(self, contract_id: str) -> List[OrderInfo]:
        """Get active orders."""
        try:
            orders = await asyncio.to_thread(
                self.account.get_open_orders,
                symbol=contract_id
            )
            
            result = []
            for o in orders:
                result.append(OrderInfo(
                    order_id=o.get('id'),
                    side=o.get('side').lower(),
                    size=Decimal(o.get('quantity')),
                    price=Decimal(o.get('price')),
                    status='OPEN',
                    filled_size=Decimal(o.get('executedQuantity', 0))
                ))
            return result
        except Exception as e:
            self.logger.log(f"Error getting active orders: {e}", "ERROR")
            return []

    async def get_account_positions(self) -> Decimal:
        """Get account positions (balances for Spot)."""
        try:
            balances = await asyncio.to_thread(self.account.get_balances)
            # For spot, "position" is the balance of the base asset
            # contract_id is like "SOL_USDC", we need "SOL"
            base_asset = self.config.contract_id.split('_')[0]
            
            if base_asset in balances:
                return Decimal(balances[base_asset].get('available', 0))
            return Decimal('0')
        except Exception as e:
            self.logger.log(f"Error getting positions: {e}", "ERROR")
            return Decimal('0')
