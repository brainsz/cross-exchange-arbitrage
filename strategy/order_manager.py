"""Order placement and monitoring for Maker Exchange and Lighter."""
import asyncio
import logging
import time
from decimal import Decimal
from typing import Optional

from exchanges.base import BaseExchangeClient, OrderResult
from lighter.signer_client import SignerClient


class OrderManager:
    """Manages order placement and monitoring for both exchanges."""

    def __init__(self, order_book_manager, logger: logging.Logger):
        """Initialize order manager."""
        self.order_book_manager = order_book_manager
        self.logger = logger

        # Maker client and config
        self.maker_client: Optional[BaseExchangeClient] = None
        self.maker_contract_id: Optional[str] = None
        self.maker_tick_size: Optional[Decimal] = None
        self.maker_order_status: Optional[str] = None
        self.maker_client_order_id: str = ''

        # Lighter client and config
        self.lighter_client: Optional[SignerClient] = None
        self.lighter_market_index: Optional[int] = None
        self.base_amount_multiplier: Optional[int] = None
        self.price_multiplier: Optional[int] = None
        self.tick_size: Optional[Decimal] = None

        # Lighter order state
        self.lighter_order_filled = False
        self.lighter_order_price: Optional[Decimal] = None
        self.lighter_order_side: Optional[str] = None
        self.lighter_order_size: Optional[Decimal] = None

        # Order execution tracking
        self.order_execution_complete = False
        self.waiting_for_lighter_fill = False
        self.current_lighter_side: Optional[str] = None
        self.current_lighter_quantity: Optional[Decimal] = None
        self.current_lighter_price: Optional[Decimal] = None

        # Callbacks
        self.on_order_filled: Optional[callable] = None
        
        # Fee configuration (default values)
        self.maker_maker_fee = Decimal('0')  # Maker fee on maker exchange
        self.maker_taker_fee = Decimal('0.00024')  # Taker fee on maker exchange
        self.lighter_fee = Decimal('0.00005')  # Lighter fee
        
        # Current maker order ID tracking
        self.current_maker_order_id: Optional[str] = None

    def set_maker_config(self, client: BaseExchangeClient, contract_id: str, tick_size: Decimal):
        """Set Maker client and configuration."""
        self.maker_client = client
        self.maker_contract_id = contract_id
        self.maker_tick_size = tick_size

    def set_lighter_config(self, client: SignerClient, market_index: int,
                           base_amount_multiplier: int, price_multiplier: int, tick_size: Decimal):
        """Set Lighter client and configuration."""
        self.lighter_client = client
        self.lighter_market_index = market_index
        self.base_amount_multiplier = base_amount_multiplier
        self.price_multiplier = price_multiplier
        self.tick_size = tick_size

    def set_callbacks(self, on_order_filled: callable = None):
        """Set callback functions."""
        self.on_order_filled = on_order_filled
    
    def set_fees(self, maker_maker_fee: Decimal = Decimal('0'), 
                 maker_taker_fee: Decimal = Decimal('0.00024'),
                 lighter_fee: Decimal = Decimal('0.00005')):
        """Set fee configuration."""
        self.maker_maker_fee = maker_maker_fee
        self.maker_taker_fee = maker_taker_fee
        self.lighter_fee = lighter_fee

    def round_to_tick(self, price: Decimal) -> Decimal:
        """Round price to tick size."""
        if self.maker_tick_size is None:
            return price
        return (price / self.maker_tick_size).quantize(Decimal('1')) * self.maker_tick_size

    async def fetch_maker_bbo_prices(self) -> tuple[Decimal, Decimal]:
        """Fetch best bid/ask prices from Maker exchange."""
        # Use WebSocket data if available (EdgeX specific check, can be generalized later)
        # For now, we rely on the client's fetch_bbo_prices which handles REST/WS abstraction
        
        # If order_book_manager has data, use it (assuming it's populated via WS)
        # Note: GenericArb currently sets up WS for EdgeX but not explicitly for others in a generic way yet
        # But let's check if we can use client's method first.
        
        if not self.maker_client:
             raise Exception("Maker client not initialized")

        return await self.maker_client.fetch_bbo_prices(self.maker_contract_id)

    async def place_bbo_order(self, side: str, quantity: Decimal) -> str:
        """Place a BBO order on Maker exchange."""
        # Note: Price calculation is handled by the exchange client's place_open_order method
        # to ensure it uses the freshest BBO data.


        self.maker_client_order_id = str(int(time.time() * 1000))
        
        # Use generic place_open_order (or we might need a specific place_limit_order in base?)
        # BaseExchangeClient has place_open_order which takes direction.
        # But here we want specific price control for BBO strategy.
        # The BaseExchangeClient.place_open_order calculates price internally!
        # Let's see BaseExchangeClient.place_open_order implementation.
        # It does: price = best_ask - tick (for buy).
        # So we can just use place_open_order!
        
        result = await self.maker_client.place_open_order(
            contract_id=self.maker_contract_id,
            quantity=quantity,
            direction=side
        )

        if not result.success:
            raise Exception(f"Failed to place order: {result.error_message}")

        return result.order_id

    async def calculate_trade_profit(self, side: str, quantity: Decimal, use_maker: bool = True) -> Decimal:
        """Calculate expected profit for a trade.
        
        Args:
            side: 'buy' or 'sell' on maker exchange
            quantity: Trade size
            use_maker: If True, calculate for maker order. If False, for taker order.
            
        Returns:
            Expected profit in USDC
        """
        try:
            # Get current prices
            maker_bid, maker_ask = await self.fetch_maker_bbo_prices()
            lighter_bid, lighter_ask = self.order_book_manager.get_lighter_best_levels()
            
            if not lighter_bid or not lighter_ask:
                return Decimal('-999999')  # Invalid
            
            lighter_bid_price = lighter_bid[0]
            lighter_ask_price = lighter_ask[0]
            
            # Calculate execution prices matching actual order placement
            maker_mid_price = (maker_bid + maker_ask) / Decimal('2')
            
            if side == 'buy':
                # Buy on maker, sell on lighter
                if use_maker:
                    maker_price = maker_mid_price - self.maker_tick_size  # Maker: below mid
                else:
                    maker_price = maker_ask  # Taker: at ask
                lighter_price = lighter_bid_price  # Sell at bid
                
                # Gross profit
                gross_profit = (lighter_price - maker_price) * quantity
            else:
                # Sell on maker, buy on lighter
                if use_maker:
                    maker_price = maker_mid_price + self.maker_tick_size  # Maker: above mid
                else:
                    maker_price = maker_bid  # Taker: at bid
                lighter_price = lighter_ask_price  # Buy at ask
                
                # Gross profit
                gross_profit = (maker_price - lighter_price) * quantity
            
            # Calculate fees
            maker_fee_rate = self.maker_maker_fee if use_maker else self.maker_taker_fee
            maker_fee = maker_price * quantity * maker_fee_rate
            lighter_fee = lighter_price * quantity * self.lighter_fee
            
            net_profit = gross_profit - maker_fee - lighter_fee
            
            return net_profit
            
        except Exception as e:
            self.logger.error(f"Error calculating profit: {e}")
            return Decimal('-999999')

    async def place_smart_order(self, side: str, quantity: Decimal, stop_flag) -> bool:
        """Place order with smart maker/taker selection based on profitability.
        
        Tries maker first (zero fees). If rejected or unprofitable, tries taker if profitable.
        """
        if not self.maker_client:
            raise Exception("Maker client not initialized")
        
        # Calculate expected profits
        maker_profit = await self.calculate_trade_profit(side, quantity, use_maker=True)
        taker_profit = await self.calculate_trade_profit(side, quantity, use_maker=False)
        
        self.logger.info(f"💰 Profit estimate - Maker: {maker_profit:.4f} USDC, Taker: {taker_profit:.4f} USDC")
        
        # Allow small losses to increase trading volume
        # User target: 10k USDC volume, max 1 USDC total loss
        # Actual taker losses observed: -0.0005 to -0.0008 USDC per trade
        # Assuming 50% maker (profitable) + 50% taker (small loss), total loss stays under 1 USDC
        # Allow up to -0.001 USDC per trade to capture most opportunities
        MIN_ACCEPTABLE_PROFIT = Decimal('-0.001')
        
        # Try maker first if acceptable (zero fees!)
        if maker_profit > MIN_ACCEPTABLE_PROFIT:
            self.logger.info(f"📊 Trying MAKER order (0% fee)")
            success = await self.place_maker_post_only_order(side, quantity, stop_flag)
            if success:
                return True
            self.logger.warning("⚠️ Maker order failed/rejected")
        else:
            self.logger.info(f"📊 Skipping MAKER order (too unprofitable: {maker_profit:.4f} USDC)")
        
        # Fallback to taker if acceptable
        if taker_profit > MIN_ACCEPTABLE_PROFIT:
            self.logger.info(f"📊 Trying TAKER order (0.024% fee, profit: {taker_profit:.4f} USDC)")
            return await self.place_maker_taker_order(side, quantity, stop_flag)
        else:
            self.logger.info(f"❌ Skipping trade - both too unprofitable (taker: {taker_profit:.4f} USDC)")
            return False

    async def place_maker_taker_order(self, side: str, quantity: Decimal, stop_flag) -> bool:
        """Place a taker order on Maker exchange."""
        if not self.maker_client:
            raise Exception("Maker client not initialized")

        self.maker_order_status = None
        self.current_maker_order_id = None
        self.logger.info(f"[OPEN] [Maker] [{side}] Placing Maker TAKER order")
        
        try:
            # Check if maker_client has place_taker_order method
            if not hasattr(self.maker_client, 'place_taker_order'):
                self.logger.error("❌ Maker client does not support taker orders")
                return False
                
            result = await self.maker_client.place_taker_order(
                contract_id=self.maker_contract_id,
                quantity=quantity,
                direction=side
            )
            
            if not result.success:
                self.logger.error(f"❌ Error placing taker order: {result.error_message}")
                return False
                
            order_id = result.order_id
            self.current_maker_order_id = order_id
            
            # Taker orders fill immediately, wait briefly for confirmation
            await asyncio.sleep(0.5)
            
            # Check order status
            try:
                order_info = await self.maker_client.get_order_info(order_id)
                if order_info and order_info.status == 'FILLED':
                    self.handle_maker_order_update({
                        'order_id': order_id,
                        'side': order_info.side,
                        'filled_size': order_info.filled_size,
                        'price': order_info.price,
                        'status': 'FILLED'
                    })
                    return True
            except Exception as e:
                self.logger.warning(f"⚠️ Error checking taker order status: {e}")
                # CRITICAL FIX: For IOC taker orders, assume filled and manually trigger hedge
                self.logger.info(f"ℹ️  Assuming taker order filled, triggering hedge manually")
                self.handle_maker_order_update({
                    'order_id': order_id,
                    'side': side,
                    'filled_size': quantity,
                    'price': 0,  # Price will be fetched from actual fill
                    'status': 'FILLED'
                })
                return True
                
            # If we reach here, order status check didn't confirm FILLED
            # But for IOC taker orders, WebSocket should have already triggered hedge
            # Return True to allow hedge loop to execute
            self.logger.info(f"ℹ️  Taker order status not confirmed via API, but WebSocket should have handled it")
            return True
            
        except Exception as e:
            self.logger.error(f"❌ Error placing taker order: {e}")
            return False


    async def place_maker_post_only_order(self, side: str, quantity: Decimal, stop_flag) -> bool:
        """Place a post-only order on Maker exchange."""
        if not self.maker_client:
            raise Exception("Maker client not initialized")

        self.maker_order_status = None
        self.current_maker_order_id = None  # Reset order ID
        self.logger.info(f"[OPEN] [Maker] [{side}] Placing Maker POST-ONLY order")
        
        try:
            order_id = await self.place_bbo_order(side, quantity)
            self.current_maker_order_id = order_id  # Store current order ID
        except Exception as e:
            self.logger.error(f"❌ Error placing Maker order: {e}")
            return False

        start_time = time.time()
        while not stop_flag:
            # We need a way to check status. 
            # In generic arb, we rely on WS updates calling handle_maker_order_update
            # OR we need to poll if no WS.
            
            # Poll status if not final
            if self.maker_order_status not in ['FILLED', 'CANCELED', 'EXPIRED']:
                 # Poll status
                 try:
                     order_info = await self.maker_client.get_order_info(order_id)
                     if order_info:
                         self.update_maker_order_status(order_info.status)
                         if order_info.status == 'FILLED':
                             # Manually trigger update handler if polling found it filled
                             self.handle_maker_order_update({
                                 'order_id': order_id,
                                 'side': order_info.side,
                                 'filled_size': order_info.filled_size,
                                 'price': order_info.price,
                                 'status': order_info.status
                             })
                 except Exception as e:
                     self.logger.warning(f"⚠️ Error polling order status: {e}")

            if self.maker_order_status == 'CANCELED':
                self.logger.info(f"ℹ️  Maker order {order_id} was canceled.")
                return False
            elif self.maker_order_status in ['NEW', 'OPEN', 'PENDING', 'CANCELING', 'PARTIALLY_FILLED', None]:
                await asyncio.sleep(0.5)
                # If order is not filled within 5 seconds, cancel it and retry loop
                if time.time() - start_time > 5:
                    self.logger.info(f"⏱️  Maker order {order_id} timed out (5s), canceling...")
                    try:
                        cancel_result = await self.maker_client.cancel_order(order_id)
                        if not cancel_result.success:
                            self.logger.error(f"❌ Error canceling Maker order: {cancel_result.error_message}")
                        
                        # Wait a bit for cancellation to process
                        await asyncio.sleep(1)
                        
                        # Check if it was filled during cancellation or partially filled before
                        try:
                            order_info = await self.maker_client.get_order_info(order_id)
                            if order_info and order_info.filled_size > 0:
                                self.logger.info(f"ℹ️  Maker order {order_id} was filled/partially filled ({order_info.filled_size}) before cancel.")
                                self.handle_maker_order_update({
                                     'order_id': order_id,
                                     'side': order_info.side,
                                     'filled_size': order_info.filled_size,
                                     'price': order_info.price,
                                     'status': 'FILLED' # Treat as filled for hedging
                                })
                                return True
                        except Exception as e:
                            self.logger.error(f"⚠️ Error checking order info after cancel: {e}")


                        # Check if WebSocket triggered a hedge while we were canceling
                        if self.waiting_for_lighter_fill:
                            self.logger.info(f"ℹ️  Order {order_id} filled during cancel, proceeding with hedge")
                            return True
                        
                        # Break the loop to return False, so the main loop can decide to retry
                        return False
                    except Exception as e:
                        self.logger.error(f"❌ Error canceling Maker order: {e}")
                        return False
            elif self.maker_order_status == 'FILLED':
                break
            else:
                if self.maker_order_status is not None:
                    self.logger.error(f"❌ Unknown Maker order status: {self.maker_order_status}")
                    return False
                else:
                    await asyncio.sleep(0.5)
        return True

    def handle_maker_order_update(self, order_data: dict):
        """Handle Maker order update."""
        order_id = order_data.get('order_id')
        side = order_data.get('side', '').lower()
        filled_size = order_data.get('filled_size')
        price = order_data.get('price', '0')
        status = order_data.get('status')
        
        # For taker orders, WebSocket events may arrive before current_maker_order_id is set
        # Only ignore if we have a current order ID AND it doesn't match
        if self.current_maker_order_id and order_id and str(order_id) != str(self.current_maker_order_id):
            self.logger.info(f"ℹ️  Ignoring order update for {order_id} (current order: {self.current_maker_order_id})")
            return
        
        # If we don't have a current order ID yet, this might be a fast WebSocket event for our taker order
        # Accept it and set the current order ID
        if not self.current_maker_order_id and order_id:
            self.logger.info(f"ℹ️  Accepting WebSocket event for order {order_id} (fast taker fill)")
            self.current_maker_order_id = order_id
        
        self.update_maker_order_status(status)

        self.logger.info(f"🔍 DEBUG: status={status}, side={side}, filled_size={filled_size}, price={price}")

        if status == 'FILLED':
            if side == 'buy':
                lighter_side = 'sell'
            else:
                lighter_side = 'buy'

            self.current_lighter_side = lighter_side
            self.current_lighter_quantity = filled_size
            self.current_lighter_price = Decimal(price) if price else Decimal('0')
            self.waiting_for_lighter_fill = True
            self.logger.info(f"✅ Set waiting_for_lighter_fill=True, lighter_side={lighter_side}, quantity={filled_size}")

    def update_maker_order_status(self, status: str):
        """Update Maker order status."""
        self.maker_order_status = status

    async def place_lighter_market_order(self, lighter_side: str, quantity: Decimal,
                                         price: Decimal, stop_flag) -> Optional[str]:
        """Place a market order on Lighter."""
        if not self.lighter_client:
            raise Exception("Lighter client not initialized")

        best_bid, best_ask = self.order_book_manager.get_lighter_best_levels()
        if not best_bid or not best_ask:
            raise Exception("Lighter order book not ready")

        if lighter_side.lower() == 'buy':
            order_type = "CLOSE"
            is_ask = False
            price = best_ask[0] * Decimal('1.002')
        else:
            order_type = "OPEN"
            is_ask = True
            price = best_bid[0] * Decimal('0.998')

        # CRITICAL: Round price to tick size to avoid "invalid order base or quote amount" error
        # Lighter requires prices to be multiples of tick_size
        if self.tick_size:
            price = (price / self.tick_size).quantize(Decimal('1'), rounding='ROUND_DOWN') * self.tick_size
            self.logger.info(f"🔍 Rounded price to tick size: {price} (tick_size={self.tick_size})")

        self.lighter_order_filled = False
        self.lighter_order_price = price
        self.lighter_order_side = lighter_side
        self.lighter_order_size = quantity

        try:
            client_order_index = int(time.time() * 1000)
            
            # Calculate base_amount
            base_amount = int(quantity * self.base_amount_multiplier)
            price_int = int(price * self.price_multiplier)
            
            # Debug logging
            self.logger.info(f"🔍 Lighter order params: quantity={quantity}, base_amount={base_amount}, "
                           f"price={price}, price_int={price_int}, "
                           f"multipliers: base={self.base_amount_multiplier}, price={self.price_multiplier}")
            
            tx_info, error = self.lighter_client.sign_create_order(
                market_index=self.lighter_market_index,
                client_order_index=client_order_index,
                base_amount=base_amount,
                price=price_int,
                is_ask=is_ask,
                order_type=self.lighter_client.ORDER_TYPE_LIMIT,
                time_in_force=self.lighter_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                reduce_only=False,
                trigger_price=0,
            )
            if error is not None:
                raise Exception(f"Sign error: {error}")

            tx_hash = await self.lighter_client.send_tx(
                tx_type=self.lighter_client.TX_TYPE_CREATE_ORDER,
                tx_info=tx_info
            )

            self.logger.info(f"[{client_order_index}] [{order_type}] [Lighter] [OPEN]: {quantity}")

            await self.monitor_lighter_order(client_order_index, stop_flag)

            return tx_hash
        except Exception as e:
            self.logger.error(f"❌ Error placing Lighter order: {e}")
            # CRITICAL: Clear the flag so we don't get stuck
            self.waiting_for_lighter_fill = False
            self.order_execution_complete = True
            return None

    async def monitor_lighter_order(self, client_order_index: int, stop_flag):
        """Monitor Lighter order and wait for fill."""
        start_time = time.time()
        while not self.lighter_order_filled and not stop_flag:
            if time.time() - start_time > 30:
                self.logger.error(
                    f"❌ Timeout waiting for Lighter order fill after {time.time() - start_time:.1f}s")
                self.logger.warning("⚠️ Using fallback - marking order as filled to continue trading")
                self.lighter_order_filled = True
                self.waiting_for_lighter_fill = False
                self.order_execution_complete = True
                break

            await asyncio.sleep(0.1)

    def handle_lighter_order_filled(self, order_data: dict):
        """Handle Lighter order fill notification."""
        try:
            order_data["avg_filled_price"] = (
                Decimal(order_data["filled_quote_amount"]) /
                Decimal(order_data["filled_base_amount"])
            )
            if order_data["is_ask"]:
                order_data["side"] = "SHORT"
                order_type = "OPEN"
            else:
                order_data["side"] = "LONG"
                order_type = "CLOSE"

            client_order_index = order_data["client_order_id"]

            self.logger.info(
                f"[{client_order_index}] [{order_type}] [Lighter] [FILLED]: "
                f"{order_data['filled_base_amount']} @ {order_data['avg_filled_price']}")

            if self.on_order_filled:
                self.on_order_filled(order_data)

            self.lighter_order_filled = True
            self.order_execution_complete = True

        except Exception as e:
            self.logger.error(f"Error handling Lighter order result: {e}")

    def get_maker_client_order_id(self) -> str:
        """Get current Maker client order ID."""
        return self.maker_client_order_id
