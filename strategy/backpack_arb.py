"""Main arbitrage trading bot for Backpack and Lighter exchanges."""
import asyncio
import signal
import logging
import os
import sys
import time
import requests
import traceback
from decimal import Decimal
from typing import Tuple
from collections import deque
import math

from lighter.signer_client import SignerClient
# from edgex_sdk import Client, WebSocketManager # Not needed for Backpack
# Import Backpack adapter
from exchanges.backpack import BackpackClient

from .data_logger import DataLogger
from .order_book_manager import OrderBookManager
from .websocket_manager import WebSocketManagerWrapper
from .order_manager import OrderManager
from .position_tracker import PositionTracker


class Config:
    """Simple config class to wrap dictionary."""
    def __init__(self, config_dict):
        for key, value in config_dict.items():
            setattr(self, key, value)


class BackpackArb:
    """Arbitrage trading bot: makes post-only orders on Backpack, and market orders on Lighter."""

    def __init__(self, ticker: str, order_quantity: Decimal,
                 fill_timeout: int = 5, max_position: Decimal = Decimal('0'),
                 window_size: int = 50, z_score: float = 1.5, min_spread: float = 0.0):
        """Initialize the arbitrage trading bot."""
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
        self.spread_history = deque(maxlen=window_size)
        self.current_mean = None
        self.current_std = None

        # Setup logger
        self._setup_logger()

        # Initialize modules
        self.data_logger = DataLogger(exchange="backpack", ticker=ticker, logger=self.logger)
        self.order_book_manager = OrderBookManager(self.logger)
        # WS Manager might need adaptation for Backpack if we use WS
        self.ws_manager = WebSocketManagerWrapper(self.order_book_manager, self.logger)
        self.order_manager = OrderManager(self.order_book_manager, self.logger)

        # Initialize clients (will be set later)
        self.backpack_client = None
        self.lighter_client = None

        # Configuration
        self.lighter_base_url = "https://mainnet.zklighter.elliot.ai"
        self.account_index = int(os.getenv('LIGHTER_ACCOUNT_INDEX'))
        self.api_key_index = int(os.getenv('LIGHTER_API_KEY_INDEX'))
        
        # Backpack config
        self.backpack_api_key = os.getenv('BACKPACK_API_KEY')
        self.backpack_api_secret = os.getenv('BACKPACK_API_SECRET')

        # Contract/market info (will be set during initialization)
        self.backpack_contract_id = None # This will be the symbol, e.g. "SOL_USDC"
        self.backpack_tick_size = None
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
        self.log_filename = f"logs/backpack_{self.ticker}_log.txt"

        self.logger = logging.getLogger(f"arbitrage_bot_backpack_{self.ticker}")
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
            on_edgex_order_update=self._handle_backpack_order_update # Reusing the name for now
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
                backpack_price = self.order_manager.current_lighter_price # Using same var name in manager
                
                if backpack_price and backpack_price > 0:
                    profit = Decimal('0')
                    direction = ""
                    
                    if order_data["side"] == "SHORT": # Lighter Sell, Backpack Buy (Long Backpack)
                        profit = (lighter_price - backpack_price) * lighter_qty
                        direction = "Long Backpack / Short Lighter"
                    else: # Lighter Buy, Backpack Sell (Short Backpack)
                        profit = (backpack_price - lighter_price) * lighter_qty
                        direction = "Short Backpack / Long Lighter"
                        
                    self.logger.info(f"💰 Profit: {profit:.6f} (Backpack: {backpack_price:.2f}, Lighter: {lighter_price:.2f}) [{direction}]")
                    
                    self.data_logger.log_profit_to_csv(
                        edgex_price=backpack_price, # Reusing column name
                        lighter_price=lighter_price,
                        quantity=lighter_qty,
                        profit=profit,
                        direction=direction
                    )
            except Exception as e:
                self.logger.error(f"Error calculating profit: {e}")

            # Mark execution as complete
            self.order_manager.lighter_order_filled = True
            self.order_manager.order_execution_complete = True

        except Exception as e:
            self.logger.error(f"Error handling Lighter order result: {e}")

    def _handle_backpack_order_update(self, order: dict):
        """Handle Backpack order update."""
        # For now, since we are using REST for Backpack, this might not be triggered by WS.
        # But if we poll or implement WS, this structure is useful.
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
            self.order_manager.update_edgex_order_status(status) # Reusing method

            # Handle filled orders
            if status == 'FILLED' and filled_size > 0:
                if side == 'buy':
                    if self.position_tracker:
                        self.position_tracker.update_edgex_position(filled_size) # Reusing method
                else:
                    if self.position_tracker:
                        self.position_tracker.update_edgex_position(-filled_size) # Reusing method

                self.logger.info(
                    f"[{order_id}] [{order_type}] [Backpack] [{status}]: {filled_size} @ {price}")

                if filled_size > 0.0001:
                    # Log Backpack trade to CSV
                    self.data_logger.log_trade_to_csv(
                        exchange='backpack',
                        side=side,
                        price=str(price),
                        quantity=str(filled_size)
                    )

                # Trigger Lighter order placement
                self.order_manager.handle_edgex_order_update({ # Reusing method
                    'order_id': order_id,
                    'side': side,
                    'status': status,
                    'size': size,
                    'price': price,
                    'contract_id': self.backpack_contract_id,
                    'filled_size': filled_size
                })
            elif status != 'FILLED':
                if status == 'OPEN':
                    self.logger.info(f"[{order_id}] [{order_type}] [Backpack] [{status}]: {size} @ {price}")
                else:
                    self.logger.info(
                        f"[{order_id}] [{order_type}] [Backpack] [{status}]: {filled_size} @ {price}")

        except Exception as e:
            self.logger.error(f"Error handling Backpack order update: {e}")

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

    def initialize_backpack_client(self):
        """Initialize the Backpack client."""
        self.backpack_client = BackpackClient({
            'ticker': self.ticker,
            'contract_id': self.ticker, # Will be updated
            'quantity': self.order_quantity,
            'tick_size': Decimal('0.01') # Default, will be updated
        })
        
        self.logger.info("✅ Backpack client initialized successfully")
        return self.backpack_client

    def get_lighter_market_config(self) -> Tuple[int, int, int, Decimal]:
        """Get Lighter market configuration."""
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

            for market in data["order_books"]:
                if market["symbol"] == self.ticker:
                    price_multiplier = pow(10, market["supported_price_decimals"])
                    return (market["market_id"],
                            pow(10, market["supported_size_decimals"]),
                            price_multiplier,
                            Decimal("1") / (Decimal("10") ** market["supported_price_decimals"]))
            raise Exception(f"Ticker {self.ticker} not found")

        except Exception as e:
            self.logger.error(f"⚠️ Error getting market config: {e}")
            raise

    async def get_backpack_contract_info(self) -> Tuple[str, Decimal]:
        """Get Backpack contract info."""
        # For Backpack, the contract_id is the symbol itself, e.g. "SOL_USDC"
        # We need to fetch tick size from public API
        try:
            markets = await asyncio.to_thread(self.backpack_client.public.get_markets)
            for m in markets:
                if m['symbol'] == self.ticker:
                    tick_size = Decimal(m['filters']['price']['tickSize'])
                    min_quantity = Decimal(m['filters']['quantity']['minQuantity'])
                    
                    if self.order_quantity < min_quantity:
                        raise ValueError(f"Order quantity {self.order_quantity} < min quantity {min_quantity}")
                        
                    return self.ticker, tick_size
            raise ValueError(f"Market {self.ticker} not found on Backpack")
        except Exception as e:
            self.logger.error(f"Error getting Backpack info: {e}")
            raise

    async def trading_loop(self):
        """Main trading loop implementing the strategy."""
        self.logger.info(f"🚀 Starting Backpack arbitrage bot for {self.ticker}")

        # Initialize clients
        try:
            self.initialize_lighter_client()
            self.initialize_backpack_client()

            # Get contract info
            self.backpack_contract_id, self.backpack_tick_size = await self.get_backpack_contract_info()
            
            # Update client config with correct tick size
            self.backpack_client.config.tick_size = self.backpack_tick_size
            self.backpack_client.config.contract_id = self.backpack_contract_id
            
            (self.lighter_market_index, self.base_amount_multiplier,
             self.price_multiplier, self.tick_size) = self.get_lighter_market_config()

            self.logger.info(
                f"Contract info loaded - Backpack: {self.backpack_contract_id}, "
                f"Lighter: {self.lighter_market_index}")

        except Exception as e:
            self.logger.error(f"❌ Failed to initialize: {e}")
            return

        # Initialize position tracker
        # We need to adapt PositionTracker to work with Backpack or create a new one.
        # For now, let's assume we can pass the backpack client as 'edgex_client' if interfaces match enough
        # or we mock it. PositionTracker uses get_account_positions which we implemented.
        self.position_tracker = PositionTracker(
            self.ticker,
            self.backpack_client, # Passing backpack client
            self.backpack_contract_id,
            self.lighter_base_url,
            self.account_index,
            self.logger
        )

        # Configure modules
        self.order_manager.set_edgex_config( # Reusing method name
            self.backpack_client, self.backpack_contract_id, self.backpack_tick_size)
        self.order_manager.set_lighter_config(
            self.lighter_client, self.lighter_market_index,
            self.base_amount_multiplier, self.price_multiplier, self.tick_size)

        # WS Manager setup
        # For Backpack, we might not have WS setup yet in this iteration.
        # We will rely on REST polling for BBO in the loop if WS is not ready.
        
        self.ws_manager.set_lighter_config(
            self.lighter_client, self.lighter_market_index, self.account_index)

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

        except Exception as e:
            self.logger.error(f"❌ Failed to setup Lighter websocket: {e}")
            return

        await asyncio.sleep(5)

        # Get initial positions
        self.position_tracker.edgex_position = await self.position_tracker.get_edgex_position()
        self.position_tracker.lighter_position = await self.position_tracker.get_lighter_position()

        # Main trading loop
        while not self.stop_flag:
            try:
                # Fetch Backpack BBO via REST (since we don't have WS yet)
                bp_best_bid, bp_best_ask = await self.backpack_client.fetch_bbo_prices(self.backpack_contract_id)
            except Exception as e:
                self.logger.error(f"⚠️ Error fetching Backpack BBO prices: {e}")
                await asyncio.sleep(0.5)
                continue

            lighter_bid, lighter_ask = self.order_book_manager.get_lighter_bbo()

            # Calculate mid prices and spread
            if not (lighter_bid and lighter_ask and bp_best_bid and bp_best_ask):
                await asyncio.sleep(0.1)
                continue

            lighter_mid = (float(lighter_bid) + float(lighter_ask)) / 2
            bp_mid = (float(bp_best_bid) + float(bp_best_ask)) / 2
            current_spread = lighter_mid - bp_mid

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
            long_bp = False
            short_bp = False
            
            # Open Logic
            # Spread > Upper -> Lighter expensive, Backpack cheap -> Sell Lighter, Buy Backpack (Long Backpack)
            if current_spread > upper_threshold and current_spread > self.min_spread:
                long_bp = True
            # Spread < Lower -> Lighter cheap, Backpack expensive -> Buy Lighter, Sell Backpack (Short Backpack)
            elif current_spread < lower_threshold and current_spread < -self.min_spread:
                short_bp = True

            # Close Logic (Mean Reversion)
            close_long_bp = False
            close_short_bp = False
            
            current_pos = self.position_tracker.get_current_edgex_position() # Reusing method
            
            if current_pos > 0 and current_spread < self.current_mean:
                close_long_bp = True
            elif current_pos < 0 and current_spread > self.current_mean:
                close_short_bp = True

            # Log BBO data
            self.data_logger.log_bbo_to_csv(
                maker_bid=bp_best_bid,
                maker_ask=bp_best_ask,
                lighter_bid=lighter_bid if lighter_bid else Decimal('0'),
                lighter_ask=lighter_ask if lighter_ask else Decimal('0'),
                long_maker=long_bp,
                short_maker=short_bp,
                long_maker_threshold=Decimal(str(upper_threshold)),
                short_maker_threshold=Decimal(str(lower_threshold))
            )

            if self.stop_flag:
                break

            # Execute trades
            if (current_pos < self.max_position and long_bp):
                await self._execute_long_trade()
            elif (current_pos > -1 * self.max_position and short_bp):
                await self._execute_short_trade()
            # Close positions logic
            elif close_long_bp:
                await self._execute_short_trade()
            elif close_short_bp:
                await self._execute_long_trade()
            else:
                await asyncio.sleep(0.05)

    async def _execute_long_trade(self):
        """Execute a long trade (buy on Backpack, sell on Lighter)."""
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
            f"Backpack position: {self.position_tracker.edgex_position} | "
            f"Lighter position: {self.position_tracker.lighter_position}")

        if abs(self.position_tracker.get_net_position()) > self.order_quantity * 2:
            self.logger.error(
                f"❌ Position diff is too large: {self.position_tracker.get_net_position()}")
            sys.exit(1)

        self.order_manager.order_execution_complete = False
        self.order_manager.waiting_for_lighter_fill = False

        try:
            side = 'buy'
            order_filled = await self.order_manager.place_edgex_post_only_order(
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
                await self.order_manager.place_lighter_market_order(
                    self.order_manager.current_lighter_side,
                    self.order_manager.current_lighter_quantity,
                    self.order_manager.current_lighter_price,
                    self.stop_flag
                )
                break

            await asyncio.sleep(0.01)
            if time.time() - start_time > 180:
                self.logger.error("❌ Timeout waiting for trade completion")
                break

    async def _execute_short_trade(self):
        """Execute a short trade (sell on Backpack, buy on Lighter)."""
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
            f"Backpack position: {self.position_tracker.edgex_position} | "
            f"Lighter position: {self.position_tracker.lighter_position}")

        if abs(self.position_tracker.get_net_position()) > self.order_quantity * 2:
            self.logger.error(
                f"❌ Position diff is too large: {self.position_tracker.get_net_position()}")
            sys.exit(1)

        self.order_manager.order_execution_complete = False
        self.order_manager.waiting_for_lighter_fill = False

        try:
            side = 'sell'
            order_filled = await self.order_manager.place_edgex_post_only_order(
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
                await self.order_manager.place_lighter_market_order(
                    self.order_manager.current_lighter_side,
                    self.order_manager.current_lighter_quantity,
                    self.order_manager.current_lighter_price,
                    self.stop_flag
                )
                break

            await asyncio.sleep(0.01)
            if time.time() - start_time > 180:
                self.logger.error("❌ Timeout waiting for trade completion")
