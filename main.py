import sys
import asyncio

from src.strategy_5min import strategy

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python strategy.py <symbol>")
        print("Example: python strategy.py btc")
        sys.exit(1)
    
    symbol = sys.argv[1].lower()
    # symbol = "btc"
    asyncio.run(strategy(symbol))