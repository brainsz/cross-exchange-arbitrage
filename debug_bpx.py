import asyncio
from bpx.public import Public

async def main():
    public = Public()
    markets = public.get_markets()
    print(f"Type: {type(markets)}")
    if isinstance(markets, list):
        print(f"First item: {markets[0]}")
    elif isinstance(markets, dict):
        print(f"Keys: {list(markets.keys())[:5]}")
        first_val = next(iter(markets.values()))
        print(f"First value: {first_val}")
    else:
        print(f"Content: {markets}")

if __name__ == "__main__":
    asyncio.run(main())
