"""Generic arbitrage trading bot for any supported exchange and Lighter."""
import asyncio
import signal
import logging
import os
import sys
import time
import traceback
from decimal import Decimal
from typing import Tuple, Type
from collections import deque
import math

from lighter.signer_client import SignerClient
from exchanges.base import BaseExchangeClient

from .data_logger import DataLogger
from .order_book_manager import OrderBookManager
from .websocket_manager import WebSocketManagerWrapper
from .order_manager import OrderManager
from .position_tracker import PositionTracker


class GenericArb:
    """Arbitrage trading bot: makes post-only orders on Maker Exchange, and market orders on Lighter."""

    def __init__(self, 
                 exchange_client_class: Type[BaseExchangeClient],
                 exchange_name: str,
                 ticker: str, 
                 order_quantity: Decimal,
                 fill_timeout: int = 5, 
                 max_position: Decimal = Decimal('0'),
                 window_size: int = 50, 
                 z_score: float = 1.5, 
                 min_spread: float = 0.0,
                 lighter_fee: float = 0.001,
                 backpack_fee: float = 0.0):
        """Initialize the arbitrage trading bot."""
        self.exchange_client_class = exchange_client_class
        self.exchange_name = exchange_name
        self.ticker = ticker
        self.order_quantity = order_quantity
        self.fill_timeout = fill_timeout
        self.max_position = max_position
        self.stop_flag = False
        self._cleanup_done = False

        # Dynamic threshold parameters
        self.window_size = window_size
        self.z_score = z_score
        self.min_spread = min_spread
        self.lighter_fee = Decimal(str(lighter_fee))
        self.backpack_fee = Decimal(str(backpack_fee))
        self.spread_history = deque(maxlen=window_size)
        self.current_mean = None
        self.current_std = None

        # Setup logger
        self._setup_logger()

        # Initialize modules
        self.data_logger = DataLogger(exchange=exchange_name, ticker=ticker, logger=self.logger)
        self.order_book_manager = OrderBookManager(self.logger)
        self.ws_manager = WebSocketManagerWrapper(self.order_book_manager, self.logger)
        self.order_manager = OrderManager(self.order_book_manager, self.logger)

        # Initialize clients (will be set later)
        self.maker_client = None
        self.lighter_client = None

        # Configuration
        self.lighter_base_url = "https://mainnet.zklighter.elliot.ai"
        
        lighter_account_index = os.getenv('LIGHTER_ACCOUNT_INDEX')
        if lighter_account_index is None:
            raise ValueError("Environment variable 'LIGHTER_ACCOUNT_INDEX' is not set. Please check your .env file.")
        self.account_index = int(lighter_account_index)

        lighter_api_key_index = os.getenv('LIGHTER_API_KEY_INDEX')
        if lighter_api_key_index is None:
            raise ValueError("Environment variable 'LIGHTER_API_KEY_INDEX' is not set. Please check your .env file.")
        self.api_key_index = int(lighter_api_key_index)
        
        # Contract/market info (will be set during initialization)
        self.maker_contract_id = None 
        self.maker_tick_size = None
        self.lighter_market_index = None
        self.base_amount_multiplier = None
        self.price_multiplier = None
        self.tick_size = None

        # Position tracker (will be initialized after clients)
        self.position_tracker = None

        # Setup callbacks
        self._setup_callbacks()

    def _setup_logger(self):
        """Setup logging configuration."""
        os.makedirs("logs", exist_ok=True)
        self.log_filename = f"logs/{self.exchange_name}_{self.ticker}_log.txt"

        self.logger = logging.getLogger(f"arbitrage_bot_{self.exchange_name}_{self.ticker}")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()

        # Disable verbose logging from external libraries
        logging.getLogger('urllib3').setLevel(logging.WARNING)
        logging.getLogger('requests').setLevel(logging.WARNING)
        logging.getLogger('websockets').setLevel(logging.WARNING)

        # Create file handler
        file_handler = logging.FileHandler(self.log_filename)
        file_handler.setLevel(logging.INFO)

        # Create console handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)

        # Create formatters
        file_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console_formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')

        file_handler.setFormatter(file_formatter)
        console_handler.setFormatter(console_formatter)

        # Add handlers
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
        self.logger.propagate = False

    def _setup_callbacks(self):
        """Setup callback functions for order updates."""
        self.ws_manager.set_callbacks(
            on_lighter_order_filled=self._handle_lighter_order_filled,
            on_edgex_order_update=self._handle_maker_order_update
        )
        self.order_manager.set_callbacks(
            on_order_filled=self._handle_lighter_order_filled
        )

    def _update_spread_stats(self, current_spread: float) -> Tuple[float, float]:
        """Update spread history and calculate mean and std."""
        self.spread_history.append(current_spread)
        
        if len(self.spread_history) < self.window_size:
            return None, None
            
        mean = sum(self.spread_history) / len(self.spread_history)
        variance = sum([((x - mean) ** 2) for x in self.spread_history]) / len(self.spread_history)
        std = math.sqrt(variance)
        
        return mean, std

    def _handle_lighter_order_filled(self, order_data: dict):
        """Handle Lighter order fill."""
        try:
            order_data["avg_filled_price"] = (
                Decimal(order_data["filled_quote_amount"]) /
                Decimal(order_data["filled_base_amount"])
            )
            if order_data["is_ask"]:
                order_data["side"] = "SHORT"
                order_type = "OPEN"
                if self.position_tracker:
                    self.position_tracker.update_lighter_position(
                        -Decimal(order_data["filled_base_amount"]))
            else:
                order_data["side"] = "LONG"
                order_type = "CLOSE"
                if self.position_tracker:
                    self.position_tracker.update_lighter_position(
                        Decimal(order_data["filled_base_amount"]))

            client_order_index = order_data["client_order_id"]
            self.logger.info(
                f"[{client_order_index}] [{order_type}] [Lighter] [FILLED]: "
                f"{order_data['filled_base_amount']} @ {order_data['avg_filled_price']}")

            # Log trade to CSV
            self.data_logger.log_trade_to_csv(
                exchange='lighter',
                side=order_data['side'],
                price=str(order_data['avg_filled_price']),
                quantity=str(order_data['filled_base_amount'])
            )

            # Calculate and Log Profit
            try:
                lighter_price = order_data["avg_filled_price"]
                lighter_qty = Decimal(order_data["filled_base_amount"])
                maker_price = self.order_manager.current_lighter_price 
                
                if maker_price and maker_price > 0:
                    profit = Decimal('0')
                    direction = ""
                    
                    if order_data["side"] == "SHORT": # Lighter Sell, Maker Buy (Long Maker)
                        profit = (lighter_price - maker_price) * lighter_qty
                        direction = f"Long {self.exchange_name} / Short Lighter"
                    else: # Lighter Buy, Maker Sell (Short Maker)
                        profit = (maker_price - lighter_price) * lighter_qty
                        direction = f"Short {self.exchange_name} / Long Lighter"
                    
                    # Calculate fees
                    lighter_fee_cost = lighter_price * lighter_qty * self.lighter_fee
                    maker_fee_cost = maker_price * lighter_qty * self.backpack_fee
                    total_fee = lighter_fee_cost + maker_fee_cost
                    net_profit = profit - total_fee

                    self.logger.info(f"💰 Gross Profit: {profit:.6f} | Net Profit: {net_profit:.6f} (Fees: {total_fee:.6f})")
                    self.logger.info(f"   ({self.exchange_name}: {maker_price:.2f}, Lighter: {lighter_price:.2f}) [{direction}]")
                    
                    self.data_logger.log_profit_to_csv(
                        edgex_price=maker_price, # Keeping column name for compatibility
                        lighter_price=lighter_price,
                        quantity=lighter_qty,
                        profit=net_profit, # Log Net Profit
                        direction=direction
                    )
            except Exception as e:
                self.logger.error(f"Error calculating profit: {e}")

            # Mark execution as complete
            self.order_manager.lighter_order_filled = True
            self.order_manager.order_execution_complete = True

        except Exception as e:
            self.logger.error(f"Error handling Lighter order result: {e}")

    def _handle_maker_order_update(self, order: dict):
        """Handle Maker order update."""
        try:
            order_id = order.get('order_id')
            status = order.get('status')
            side = order.get('side', '').lower()
            filled_size = Decimal(order.get('filled_size', '0'))
            size = Decimal(order.get('size', '0'))
            price = order.get('price', '0')

            if side == 'buy':
                order_type = "OPEN"
            else:
                order_type = "CLOSE"

            # Update order status
            self.order_manager.update_maker_order_status(status)

            # Handle filled orders
            if status == 'FILLED' and filled_size > 0:
                if side == 'buy':
                    if self.position_tracker:
                        self.position_tracker.update_edgex_position(filled_size)
                else:
                    if self.position_tracker:
                        self.position_tracker.update_edgex_position(-filled_size)

                self.logger.info(
                    f"[{order_id}] [{order_type}] [{self.exchange_name}] [{status}]: {filled_size} @ {price}")

                if filled_size > 0.0001:
                    # Log trade to CSV
                    self.data_logger.log_trade_to_csv(
                        exchange=self.exchange_name,
                        side=side,
                        price=str(price),
                        quantity=str(filled_size)
                    )

                # Trigger Lighter order placement
                self.order_manager.handle_maker_order_update({
                    'order_id': order_id,
                    'side': side,
                    'status': status,
                    'size': size,
                    'price': price,
                    'contract_id': self.maker_contract_id,
                    'filled_size': filled_size
                })
            elif status != 'FILLED':
                if status == 'OPEN':
                    self.logger.info(f"[{order_id}] [{order_type}] [{self.exchange_name}] [{status}]: {size} @ {price}")
                else:
                    self.logger.info(
                        f"[{order_id}] [{order_type}] [{self.exchange_name}] [{status}]: {filled_size} @ {price}")

        except Exception as e:
            self.logger.error(f"Error handling {self.exchange_name} order update: {e}")

    def shutdown(self, signum=None, frame=None):
        """Graceful shutdown handler."""
        if self.stop_flag:
            return

        self.stop_flag = True

        if signum is not None:
            self.logger.info("\n🛑 Stopping...")
        else:
            self.logger.info("🛑 Stopping...")

        # Shutdown WebSocket connections
        try:
            if self.ws_manager:
                self.ws_manager.shutdown()
        except Exception as e:
            self.logger.error(f"Error shutting down WebSocket manager: {e}")
            
        # Shutdown maker client if needed
        try:
            if self.maker_client:
                asyncio.create_task(self.maker_client.disconnect())
        except Exception as e:
            self.logger.error(f"Error disconnecting maker client: {e}")

        # Close data logger
        try:
            if self.data_logger:
                self.data_logger.close()
        except Exception as e:
            self.logger.error(f"Error closing data logger: {e}")

        # Close logging handlers
        for handler in self.logger.handlers[:]:
            try:
                handler.close()
                self.logger.removeHandler(handler)
            except Exception:
                pass

    def setup_signal_handlers(self):
        """Setup signal handlers for graceful shutdown."""
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    def initialize_lighter_client(self):
        """Initialize the Lighter client."""
        if self.lighter_client is None:
            api_key_private_key = os.getenv('API_KEY_PRIVATE_KEY')
            if not api_key_private_key:
                raise Exception("API_KEY_PRIVATE_KEY environment variable not set")

            self.lighter_client = SignerClient(
                url=self.lighter_base_url,
                private_key=api_key_private_key,
                account_index=self.account_index,
                api_key_index=self.api_key_index,
            )

            err = self.lighter_client.check_client()
            if err is not None:
                raise Exception(f"CheckClient error: {err}")

            self.logger.info("✅ Lighter client initialized successfully")
        return self.lighter_client

    async def initialize_maker_client(self):
        """Initialize the Maker client."""
        self.maker_client = self.exchange_client_class({
            'ticker': self.ticker,
            'quantity': self.order_quantity,
            # Initial dummy values, will be updated
            'contract_id': self.ticker, 
            'tick_size': Decimal('0.01'),
            'close_order_side': 'sell' # Default assumption
        })
        
        await self.maker_client.connect()
        self.logger.info(f"✅ {self.exchange_name} client initialized successfully")
        
        # Setup order update handler
        self.maker_client.setup_order_update_handler(self._handle_maker_order_update)
        
        return self.maker_client

    def get_lighter_market_config(self) -> Tuple[int, int, int, Decimal]:
        """Get Lighter market configuration."""
        # ... (Same as before)
        import requests # Ensure requests is imported
        url = f"{self.lighter_base_url}/api/v1/orderBooks"
        headers = {"accept": "application/json"}

        try:
            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            if not response.text.strip():
                raise Exception("Empty response from Lighter API")

            data = response.json()

            if "order_books" not in data:
                raise Exception("Unexpected response format")

            # Try to match exact ticker first, then base symbol
            target_symbols = [self.ticker]
            if '_' in self.ticker:
                target_symbols.append(self.ticker.split('_')[0])
            
            for target in target_symbols:
                for market in data["order_books"]:
                    if market["symbol"] == target:
                        price_multiplier = pow(10, market["supported_price_decimals"])
                        return (market["market_id"],
                                pow(10, market["supported_size_decimals"]),
                                price_multiplier,
                                Decimal("1") / (Decimal("10") ** market["supported_price_decimals"]))
            
            raise Exception(f"Ticker {self.ticker} (or base symbol) not found in Lighter order books")

        except Exception as e:
            self.logger.error(f"⚠️ Error getting market config: {e}")
            raise

    async def trading_loop(self):
        """Main trading loop implementing the strategy."""
        self.logger.info(f"🚀 Starting {self.exchange_name} arbitrage bot for {self.ticker}")

        # Initialize clients
        try:
            self.initialize_lighter_client()
            await self.initialize_maker_client()

            # Get contract info
            self.maker_contract_id, self.maker_tick_size = await self.maker_client.get_contract_attributes()
            
            # Update client config (already done inside get_contract_attributes usually, but ensuring)
            self.maker_client.config.tick_size = self.maker_tick_size
            self.maker_client.config.contract_id = self.maker_contract_id
            
            (self.lighter_market_index, self.base_amount_multiplier,
             self.price_multiplier, self.tick_size) = self.get_lighter_market_config()

            self.logger.info(
                f"Contract info loaded - {self.exchange_name}: {self.maker_contract_id}, "
                f"Lighter: {self.lighter_market_index}")

            # Calculate and log breakeven spread
            # Assuming price is roughly the mid price of Lighter for estimation
            try:
                # Fetch initial price for estimation
                est_price_tuple = await self.maker_client.fetch_bbo_prices(self.maker_contract_id)
                est_price = (est_price_tuple[0] + est_price_tuple[1]) / 2
                if est_price == 0:
                    est_price = Decimal('90000') # Fallback
                
                # Assuming self.lighter_fee and self.backpack_fee are Decimal values representing percentages (e.g., 0.001 for 0.1%)
                # And that these are initialized in __init__
                breakeven = est_price * (self.lighter_fee + self.backpack_fee)
                self.logger.info(f"ℹ️  Fee Rates - Lighter: {self.lighter_fee*100}%, {self.exchange_name}: {self.backpack_fee*100}%")
                self.logger.info(f"ℹ️  Estimated Breakeven Spread: ~{breakeven:.2f} USDC (at price {est_price:.2f})")
                
                if Decimal(str(self.min_spread)) < breakeven:
                    self.logger.warning(f"⚠️  WARNING: min-spread ({self.min_spread}) is less than estimated breakeven ({breakeven:.2f}). You may lose money on fees.")
            except Exception as e:
                self.logger.warning(f"Could not calculate breakeven: {e}")

        except Exception as e:
            self.logger.error(f"❌ Failed to initialize: {e}")
            self.logger.error(traceback.format_exc())
            return

        # Initialize position tracker
        self.position_tracker = PositionTracker(
            self.ticker,
            self.maker_client, 
            self.maker_contract_id,
            self.lighter_base_url,
            self.account_index,
            self.logger
        )

        # Configure modules
        self.order_manager.set_maker_config(
            self.maker_client, self.maker_contract_id, self.maker_tick_size)
        self.order_manager.set_lighter_config(
            self.lighter_client, self.lighter_market_index,
            self.base_amount_multiplier, self.price_multiplier, self.tick_size)

        self.ws_manager.set_lighter_config(
            self.lighter_client, self.lighter_market_index, self.account_index)
            
        # If maker client has WS manager that needs setting in wrapper
        if hasattr(self.maker_client, 'ws_manager'):
             self.ws_manager.set_edgex_ws_manager(self.maker_client.ws_manager, self.maker_contract_id)

        # Setup Lighter websocket
        try:
            self.ws_manager.start_lighter_websocket()
            self.logger.info("✅ Lighter WebSocket task started")

            # Wait for initial Lighter order book data
            self.logger.info("⏳ Waiting for initial Lighter order book data...")
            timeout = 10
            start_time = time.time()
            while (not self.order_book_manager.lighter_order_book_ready and
                   not self.stop_flag):
                if time.time() - start_time > timeout:
                    self.logger.warning(
                        f"⚠️ Timeout waiting for Lighter WebSocket order book data after {timeout}s")
                    break
                await asyncio.sleep(0.5)

            if self.order_book_manager.lighter_order_book_ready:
                self.logger.info("✅ Lighter WebSocket order book data received")
            else:
                self.logger.warning("⚠️ Lighter WebSocket order book not ready")
                
            # Wait for Maker order book data if applicable (via WS)
            # If using REST, this loop is skipped or irrelevant
            # We can check if maker client uses WS for BBO
            # For now, we assume if WS is connected, we might get data. 
            # But the loop below handles REST fallback if needed.

        except Exception as e:
            self.logger.error(f"❌ Failed to setup Lighter websocket: {e}")
            return

        await asyncio.sleep(5)

        # Get initial positions
        self.position_tracker.edgex_position = await self.position_tracker.get_edgex_position()
        self.position_tracker.lighter_position = await self.position_tracker.get_lighter_position()

        # Main trading loop
        while not self.stop_flag:

            # Fetch Maker BBO
            # BaseExchangeClient has fetch_bbo_prices
            retry_count = 0
            max_retries = 3
            while retry_count < max_retries:
                try:
                    maker_best_bid, maker_best_ask = await self.maker_client.fetch_bbo_prices(self.maker_contract_id)
                    break
                except Exception as e:
                    retry_count += 1
                    if retry_count == max_retries:
                        self.logger.error(f"⚠️ Error fetching {self.exchange_name} BBO prices after {max_retries} retries: {e}")
                        maker_best_bid, maker_best_ask = Decimal('0'), Decimal('0')
                    else:
                        await asyncio.sleep(0.5 * retry_count)
            
            if maker_best_bid == 0 and maker_best_ask == 0:
                await asyncio.sleep(1)
                continue

            lighter_bid, lighter_ask = self.order_book_manager.get_lighter_bbo()

            # Calculate mid prices and spread
            if not (lighter_bid and lighter_ask and maker_best_bid and maker_best_ask):
                await asyncio.sleep(0.1)
                continue

            lighter_mid = (float(lighter_bid) + float(lighter_ask)) / 2
            maker_mid = (float(maker_best_bid) + float(maker_best_ask)) / 2
            current_spread = lighter_mid - maker_mid

            # Update stats
            self.current_mean, self.current_std = self._update_spread_stats(current_spread)

            if self.current_mean is None:
                self.logger.info(f"Collecting data... {len(self.spread_history)}/{self.window_size}")
                await asyncio.sleep(1)
                continue

            # Determine thresholds
            upper_threshold = self.current_mean + (self.z_score * self.current_std)
            lower_threshold = self.current_mean - (self.z_score * self.current_std)

            # Determine if we should trade
            long_maker = False
            short_maker = False
            
            # Open Logic
            if current_spread > upper_threshold and current_spread > self.min_spread:
                long_maker = True
            elif current_spread < lower_threshold and current_spread < -self.min_spread:
                short_maker = True

            # Close Logic (Mean Reversion)
            close_long_maker = False
            close_short_maker = False
            
            current_pos = self.position_tracker.get_current_edgex_position()
            
            if current_pos > 0 and current_spread < self.current_mean:
                close_long_maker = True
            elif current_pos < 0 and current_spread > self.current_mean:
                close_short_maker = True

            # Log BBO data
            self.data_logger.log_bbo_to_csv(
                maker_bid=maker_best_bid,
                maker_ask=maker_best_ask,
                lighter_bid=lighter_bid if lighter_bid else Decimal('0'),
                lighter_ask=lighter_ask if lighter_ask else Decimal('0'),
                long_maker=long_maker,
                short_maker=short_maker,
                long_maker_threshold=Decimal(str(upper_threshold)),
                short_maker_threshold=Decimal(str(lower_threshold))
            )

            if self.stop_flag:
                break

            # Execute trades
            if (current_pos < self.max_position and long_maker):
                await self._execute_long_trade()
            elif (current_pos > -1 * self.max_position and short_maker):
                await self._execute_short_trade()
            # Close positions logic
            elif close_long_maker:
                await self._execute_short_trade()
            elif close_short_maker:
                await self._execute_long_trade()
            else:
                await asyncio.sleep(0.05)

    async def _validate_trade_conditions(self, lighter_side: str, quantity: Decimal, limit_price: Decimal) -> bool:
        """
        Validate if Lighter has enough balance and liquidity to execute the hedge.
        lighter_side: 'buy' or 'sell'
        quantity: Amount to trade
        limit_price: Worst acceptable price
        """
        # 1. Check Liquidity
        has_liquidity = self.order_book_manager.check_lighter_liquidity(lighter_side, limit_price, quantity)
        if not has_liquidity:
            self.logger.warning(f"⚠️ Insufficient Lighter liquidity to {lighter_side} {quantity} @ {limit_price}")
            return False

        # 2. Check Balance
        # Approximate margin needed (assuming 1x leverage for safety check, though actual is higher)
        # If buying: need USDC ~ price * quantity
        # If selling: need USDC collateral (for perp) ~ price * quantity / leverage
        # Since we don't know leverage exactly, let's assume we need at least 10% of notional value as free collateral
        try:
            balance = await self.position_tracker.get_lighter_balance()
            required_margin = (limit_price * quantity) * Decimal('0.1') # 10x leverage assumption safety buffer
            
            if balance < required_margin:
                self.logger.warning(f"⚠️ Insufficient Lighter balance: {balance} < {required_margin} (required)")
                return False
        except Exception as e:
            self.logger.warning(f"⚠️ Could not validate Lighter balance: {e}")
            # Don't block if check fails, but log warning
        
        return True

    async def _execute_long_trade(self):
        """Execute a long trade (buy on Maker, sell on Lighter)."""
        if self.stop_flag:
            return

        # Update positions
        try:
            self.position_tracker.edgex_position = await asyncio.wait_for(
                self.position_tracker.get_edgex_position(),
                timeout=3.0
            )
            if self.stop_flag:
                return
            self.position_tracker.lighter_position = await asyncio.wait_for(
                self.position_tracker.get_lighter_position(),
                timeout=3.0
            )
        except Exception as e:
            self.logger.error(f"⚠️ Error getting positions: {e}")
            return

        if self.stop_flag:
            return

        self.logger.info(
            f"{self.exchange_name} position: {self.position_tracker.edgex_position} | "
            f"Lighter position: {self.position_tracker.lighter_position}")

        if abs(self.position_tracker.get_net_position()) > self.order_quantity * 2:
            self.logger.error(
                f"❌ Position diff is too large: {self.position_tracker.get_net_position()}")
            sys.exit(1)

        # Validate Lighter conditions (Liquidity & Balance)
        # For Long Trade: Buy Maker, Sell Lighter
        # We need to check if we can SELL on Lighter (Bids)
        best_bid, _ = self.order_book_manager.get_lighter_best_levels()
        if not best_bid:
            self.logger.warning("⚠️ Lighter order book empty, skipping trade")
            return

        limit_price = best_bid[0] * Decimal('0.995') # Allow 0.5% slippage for validation
        if not await self._validate_trade_conditions('sell', self.order_quantity, limit_price):
            return

        self.order_manager.order_execution_complete = False
        self.order_manager.waiting_for_lighter_fill = False

        try:
            side = 'buy'
            order_filled = await self.order_manager.place_maker_post_only_order(
                side, self.order_quantity, self.stop_flag)
            if not order_filled or self.stop_flag:
                return
        except Exception as e:
            self.logger.error(f"⚠️ Error in trading loop: {e}")
            self.logger.error(f"⚠️ Full traceback: {traceback.format_exc()}")
            sys.exit(1)

        start_time = time.time()
        while not self.order_manager.order_execution_complete and not self.stop_flag:
            if self.order_manager.waiting_for_lighter_fill:
                # Retry logic for Lighter order
                max_retries = 5
                for i in range(max_retries):
                    tx_hash = await self.order_manager.place_lighter_market_order(
                        self.order_manager.current_lighter_side,
                        self.order_manager.current_lighter_quantity,
                        self.order_manager.current_lighter_price,
                        self.stop_flag
                    )
                    if tx_hash:
                        break
                    self.logger.warning(f"⚠️ Lighter order placement failed, retrying ({i+1}/{max_retries})...")
                    await asyncio.sleep(1)
                
                if not tx_hash:
                    self.logger.error("❌ CRITICAL: Failed to place Lighter hedge order after retries! Position is unhedged.")
                    self.logger.warning("⚠️ Attempting EMERGENCY CLOSE of Maker position...")
                    try:
                        # We bought on Maker, so we need to Sell to close.
                        # Use aggressive price (e.g. 5% lower) to ensure fill as Taker
                        current_price = self.order_manager.current_lighter_price # This is lighter price, not maker
                        # Fetch current maker bid
                        bbo = await self.maker_client.fetch_bbo_prices(self.maker_contract_id)
                        exit_price = bbo[0] * Decimal('0.95') # Sell into bid with slippage tolerance
                        
                        await self.maker_client.place_close_order(
                            contract_id=self.maker_contract_id,
                            quantity=self.order_quantity,
                            price=exit_price,
                            side='sell',
                            post_only=False
                        )
                        self.logger.info("✅ Emergency close order placed.")
                    except Exception as e:
                        self.logger.error(f"❌ Failed to execute emergency close: {e}")
                
                break

            await asyncio.sleep(0.01)
            if time.time() - start_time > 180:
                self.logger.error("❌ Timeout waiting for trade completion")
                break

    async def _execute_short_trade(self):
        """Execute a short trade (sell on Maker, buy on Lighter)."""
        if self.stop_flag:
            return

        # Update positions
        try:
            self.position_tracker.edgex_position = await asyncio.wait_for(
                self.position_tracker.get_edgex_position(),
                timeout=3.0
            )
            if self.stop_flag:
                return
            self.position_tracker.lighter_position = await asyncio.wait_for(
                self.position_tracker.get_lighter_position(),
                timeout=3.0
            )
        except Exception as e:
            self.logger.error(f"⚠️ Error getting positions: {e}")
            return

        if self.stop_flag:
            return

        self.logger.info(
            f"{self.exchange_name} position: {self.position_tracker.edgex_position} | "
            f"Lighter position: {self.position_tracker.lighter_position}")

        if abs(self.position_tracker.get_net_position()) > self.order_quantity * 2:
            self.logger.error(
                f"❌ Position diff is too large: {self.position_tracker.get_net_position()}")
            sys.exit(1)

        # Validate Lighter conditions (Liquidity & Balance)
        # For Short Trade: Sell Maker, Buy Lighter
        # We need to check if we can BUY on Lighter (Asks)
        _, best_ask = self.order_book_manager.get_lighter_best_levels()
        if not best_ask:
            self.logger.warning("⚠️ Lighter order book empty, skipping trade")
            return

        limit_price = best_ask[0] * Decimal('1.005') # Allow 0.5% slippage for validation
        if not await self._validate_trade_conditions('buy', self.order_quantity, limit_price):
            return

        self.order_manager.order_execution_complete = False
        self.order_manager.waiting_for_lighter_fill = False

        try:
            side = 'sell'
            order_filled = await self.order_manager.place_maker_post_only_order(
                side, self.order_quantity, self.stop_flag)
            if not order_filled or self.stop_flag:
                return
        except Exception as e:
            self.logger.error(f"⚠️ Error in trading loop: {e}")
            self.logger.error(f"⚠️ Full traceback: {traceback.format_exc()}")
            sys.exit(1)

        start_time = time.time()
        while not self.order_manager.order_execution_complete and not self.stop_flag:
            if self.order_manager.waiting_for_lighter_fill:
                # Retry logic for Lighter order
                max_retries = 5
                for i in range(max_retries):
                    tx_hash = await self.order_manager.place_lighter_market_order(
                        self.order_manager.current_lighter_side,
                        self.order_manager.current_lighter_quantity,
                        self.order_manager.current_lighter_price,
                        self.stop_flag
                    )
                    if tx_hash:
                        break
                    self.logger.warning(f"⚠️ Lighter order placement failed, retrying ({i+1}/{max_retries})...")
                    await asyncio.sleep(1)
                
                if not tx_hash:
                    self.logger.error("❌ CRITICAL: Failed to place Lighter hedge order after retries! Position is unhedged.")
                    self.logger.warning("⚠️ Attempting EMERGENCY CLOSE of Maker position...")
                    try:
                        # We sold on Maker, so we need to Buy to close.
                        # Use aggressive price (e.g. 5% higher) to ensure fill as Taker
                        bbo = await self.maker_client.fetch_bbo_prices(self.maker_contract_id)
                        exit_price = bbo[1] * Decimal('1.05') # Buy from ask with slippage tolerance
                        
                        await self.maker_client.place_close_order(
                            contract_id=self.maker_contract_id,
                            quantity=self.order_quantity,
                            price=exit_price,
                            side='buy',
                            post_only=False
                        )
                        self.logger.info("✅ Emergency close order placed.")
                    except Exception as e:
                        self.logger.error(f"❌ Failed to execute emergency close: {e}")
                
                break

            await asyncio.sleep(0.01)
            if time.time() - start_time > 180:
                self.logger.error("❌ Timeout waiting for trade completion")
