
from bpx.public import Public
import time

public = Public()

# 24 hours ago
start_time = int(time.time()) - 24 * 3600

print(f"Fetching klines for SOL_USDC 1h starting from {start_time}...")

try:
    # Arguments: symbol, interval, start_time
    klines = public.get_klines("SOL_USDC", "1h", start_time)
    
    print(f"Got {len(klines)} candles.")
    if klines:
        print("First candle:", klines[0])
        print("Last candle:", klines[-1])
        
except Exception as e:
    print(f"Error: {e}")
