import csv
import statistics
import sys

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
                    if item['maker_bid'] > 0 and item['maker_ask'] > 0 and \
                       item['lighter_bid'] > 0 and item['lighter_ask'] > 0:
                        data.append(item)
                except (ValueError, KeyError):
                    continue
    except Exception as e:
        print(f"Error loading data: {e}")
        return None
    return data

def analyze_spreads(data):
    spreads = []
    for row in data:
        lighter_mid = (row['lighter_bid'] + row['lighter_ask']) / 2
        ex_mid = (row['maker_bid'] + row['maker_ask']) / 2
        spread = lighter_mid - ex_mid
        spreads.append(spread)
        
    if not spreads:
        print("No spreads calculated.")
        return

    avg_spread = statistics.mean(spreads)
    try:
        std_spread = statistics.stdev(spreads)
    except:
        std_spread = 0
    max_spread = max(spreads)
    min_spread = min(spreads)
    abs_spreads = [abs(s) for s in spreads]
    avg_abs_spread = statistics.mean(abs_spreads)
    max_abs_spread = max(abs_spreads)
    
    print("="*50)
    print("SPREAD STATISTICS")
    print(f"Count: {len(spreads)}")
    print(f"Average Spread: {avg_spread:.4f}")
    print(f"Std Dev: {std_spread:.4f}")
    print(f"Max Spread: {max_spread:.4f}")
    print(f"Min Spread: {min_spread:.4f}")
    print(f"Avg Abs Spread: {avg_abs_spread:.4f}")
    print(f"Max Abs Spread: {max_abs_spread:.4f}")
    print("="*50)
    
    # Check potential profitability
    # Assume price ~90,000
    price = 90000
    # Fee rate 0.15% -> Break even spread = 135
    break_even = price * 0.0015
    print(f"Estimated Break-Even Spread (0.15% fees @ {price}): {break_even:.2f}")
    
    profitable_count = sum(1 for s in abs_spreads if s > break_even)
    print(f"Number of potentially profitable spreads: {profitable_count}")
    print("="*50)

if __name__ == "__main__":
    filepath = 'logs/edgex_BTC_bbo_data.csv'
    data = load_data(filepath)
    if data:
        analyze_spreads(data)
