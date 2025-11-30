import asyncio
import sys
import argparse
from decimal import Decimal
import dotenv

from strategy.generic_arb import GenericArb
from exchanges.edgex import EdgeXClient
from exchanges.backpack import BackpackClient


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Cross-Exchange Arbitrage Bot Entry Point',
        formatter_class=argparse.RawDescriptionHelpFormatter
        )

    parser.add_argument('--exchange', type=str, default='edgex',
                        help='Exchange to use (edgex, backpack)')
    parser.add_argument('--ticker', type=str, default='BTC',
                        help='Ticker symbol (default: BTC)')
    parser.add_argument('--size', type=str, required=True,
                        help='Number of tokens to buy/sell per order')
    parser.add_argument('--fill-timeout', type=int, default=5,
                        help='Timeout in seconds for maker order fills (default: 5)')
    parser.add_argument('--max-position', type=Decimal, default=Decimal('0'),
                        help='Maximum position to hold (default: 0)')
    parser.add_argument('--window-size', type=int, default=50,
                        help='Window size for rolling stats (default: 50)')
    parser.add_argument('--z-score', type=float, default=1.5,
                        help='Z-score for dynamic thresholds (default: 1.5)')
    parser.add_argument('--min-spread', type=float, default=0.0,
                        help='Minimum spread required to trade (default: 0.0)')
    return parser.parse_args()


def get_exchange_client_class(exchange_name):
    """Get the exchange client class based on name."""
    if exchange_name == 'edgex':
        return EdgeXClient
    elif exchange_name == 'backpack':
        return BackpackClient
    else:
        raise ValueError(f"Unsupported exchange: {exchange_name}")


async def main():
    """Main entry point that creates and runs the cross-exchange arbitrage bot."""
    args = parse_arguments()

    dotenv.load_dotenv()

    try:
        exchange_class = get_exchange_client_class(args.exchange)
        
        bot = GenericArb(
            exchange_client_class=exchange_class,
            exchange_name=args.exchange,
            ticker=args.ticker.upper(),
            order_quantity=Decimal(args.size),
            fill_timeout=args.fill_timeout,
            max_position=args.max_position,
            window_size=args.window_size,
            z_score=args.z_score,
            min_spread=args.min_spread
        )

        # Run the bot
        await bot.trading_loop()

    except KeyboardInterrupt:
        print("\nCross-Exchange Arbitrage interrupted by user")
        return 1
    except Exception as e:
        print(f"Error running cross-exchange arbitrage: {e}")
        import traceback
        print(f"Full traceback: {traceback.format_exc()}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
