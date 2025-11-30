import csv
import math
import sys
from decimal import Decimal
import statistics

def load_data(filepath):
    """Load BBO data from CSV."""
    data = []
    try:
        with open(filepath, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    item = {
                        'maker_bid': float(row['maker_bid']),
                        'maker_ask': float(row['maker_ask']),
                        'lighter_bid': float(row['lighter_bid']),
                        'lighter_ask': float(row['lighter_ask']),
                    }
                    # Basic validation
                    if item['maker_bid'] > 0 and item['maker_ask'] > 0 and \
                       item['lighter_bid'] > 0 and item['lighter_ask'] > 0:
                        data.append(item)
                except (ValueError, KeyError):
                    continue
    except Exception as e:
        print(f"Error loading data: {e}")
        return None
    return data

def calculate_rolling_stats(values, window_size):
    if len(values) < window_size:
        return None, None
    window = values[-window_size:]
    mean = statistics.mean(window)
    if window_size > 1:
        stdev = statistics.stdev(window)
    else:
        stdev = 0
    return mean, stdev

def simulate_strategy(data, window_size, z_score, fee_rate=0.001):
    """
    Simulate the arbitrage strategy with given parameters.
    fee_rate: estimated total fee per round trip (e.g. 0.1% = 0.001)
    """
    spread_history = []
    
    position = 0 # 0: Flat, 1: Long EdgeX, -1: Short EdgeX
    entry_price_ex = 0
    entry_price_lighter = 0
    total_profit = 0
    trade_count = 0
    
    # Assume fixed quantity for simulation
    quantity = 0.001 
    
    for i, row in enumerate(data):
        # Calculate mid prices
        lighter_mid = (row['lighter_bid'] + row['lighter_ask']) / 2
        ex_mid = (row['maker_bid'] + row['maker_ask']) / 2
        spread = lighter_mid - ex_mid
        
        spread_history.append(spread)
        
        if len(spread_history) < window_size:
            continue
            
        # Get rolling stats
        # Optimization: maintain running sum/sq_sum for speed if needed, 
        # but for N=450 rows, slicing is fine.
        window = spread_history[-window_size:]
        mean = statistics.mean(window)
        stdev = statistics.stdev(window) if window_size > 1 else 0
        
        upper_threshold = mean + (z_score * stdev)
        lower_threshold = mean - (z_score * stdev)
        
        # Open Logic
        if position == 0:
            if spread > upper_threshold:
                # Open Long EdgeX (Buy Ex, Sell Lighter)
                # Execution: Buy Ex @ Bid, Sell Lighter @ Bid
                # Check if profitable including fees? The bot currently doesn't check this, 
                # it only checks Z-score. We simulate the bot AS IS.
                
                position = 1
                entry_price_ex = row['maker_bid']
                entry_price_lighter = row['lighter_bid']
                trade_count += 1
                
            elif spread < lower_threshold:
                # Open Short EdgeX (Sell Ex, Buy Lighter)
                # Execution: Sell Ex @ Ask, Buy Lighter @ Ask
                position = -1
                entry_price_ex = row['maker_ask']
                entry_price_lighter = row['lighter_ask']
                trade_count += 1
                
        # Close Logic
        elif position == 1: # Long EdgeX
            if spread < mean:
                # Close Long EdgeX (Sell Ex, Buy Lighter)
                # Execution: Sell Ex @ Ask, Buy Lighter @ Ask
                exit_price_ex = row['maker_ask']
                exit_price_lighter = row['lighter_ask']
                
                # Profit Calculation
                # Long Ex: (Exit - Entry)
                pnl_ex = (exit_price_ex - entry_price_ex) * quantity
                # Short Lighter: (Entry - Exit)
                pnl_lighter = (entry_price_lighter - exit_price_lighter) * quantity
                
                # Fees (approximate)
                # 4 trades total (Open Ex, Open Light, Close Ex, Close Light)
                # Assume 0.05% per trade on average -> 0.2% total?
                # Let's use the fee_rate param.
                fees = (entry_price_ex + entry_price_lighter + exit_price_ex + exit_price_lighter) * quantity * fee_rate / 4
                
                total_profit += (pnl_ex + pnl_lighter - fees)
                position = 0
                
        elif position == -1: # Short EdgeX
            if spread > mean:
                # Close Short EdgeX (Buy Ex, Sell Lighter)
                # Execution: Buy Ex @ Bid, Sell Lighter @ Bid
                exit_price_ex = row['maker_bid']
                exit_price_lighter = row['lighter_bid']
                
                # Profit Calculation
                # Short Ex: (Entry - Exit)
                pnl_ex = (entry_price_ex - exit_price_ex) * quantity
                # Long Lighter: (Exit - Entry)
                pnl_lighter = (exit_price_lighter - entry_price_lighter) * quantity
                
                fees = (entry_price_ex + entry_price_lighter + exit_price_ex + exit_price_lighter) * quantity * fee_rate / 4
                
                total_profit += (pnl_ex + pnl_lighter - fees)
                position = 0
                
    return total_profit, trade_count

import argparse

# ... (imports remain the same, just adding argparse)

def optimize():
    parser = argparse.ArgumentParser(description='Optimize arbitrage strategy parameters.')
    parser.add_argument('--file', type=str, default='logs/edgex_BTC_bbo_data.csv',
                        help='Path to the BBO data CSV file')
    args = parser.parse_args()

    filepath = args.file
    data = load_data(filepath)
    if not data:
        print(f"No data found in {filepath}")
        return

    print(f"Loaded {len(data)} rows of data from {filepath}")

    window_sizes = [10, 20, 30, 40, 50, 60]
    z_scores = [1.0, 1.5, 2.0, 2.5, 3.0]
    
    # Set fee_rate to 0 to analyze gross profit potential
    fee_rate = 0.0
    
    results = []
    
    print(f"{'Window':<10} {'Z-Score':<10} {'Profit':<15} {'Trades':<10}")
    print("-" * 50)
    
    for w in window_sizes:
        for z in z_scores:
            profit, trades = simulate_strategy(data, w, z, fee_rate)
            results.append({
                'window': w,
                'z_score': z,
                'profit': profit,
                'trades': trades
            })
            print(f"{w:<10} {z:<10} {profit:<15.8f} {trades:<10}")
        
    # Find best
    if not results:
        print("No results.")
        return

    best_result = max(results, key=lambda x: x['profit'])
    
    print("\n" + "="*50)
    print("BEST PARAMETERS (Max Profit):")
    print(f"Window Size: {best_result['window']}")
    print(f"Z-Score:     {best_result['z_score']}")
    print(f"Total Profit: {best_result['profit']:.8f}")
    print(f"Total Trades: {best_result['trades']}")
    print("="*50)

if __name__ == "__main__":
    optimize()
