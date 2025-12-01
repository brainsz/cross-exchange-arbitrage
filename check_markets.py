import asyncio
from bpx.public import Public

async def main():
    public = Public()
    markets = await asyncio.to_thread(public.get_markets)
    for m in markets:
        print(f"Symbol: {m['symbol']}")

if __name__ == "__main__":
    asyncio.run(main())
