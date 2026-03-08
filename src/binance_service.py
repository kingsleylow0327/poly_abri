import requests

def request_price(symbol: str):
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    response = requests.get(url)
    return float(response.json().get('price'))

# might need to use chain link, https://data.chain.link/streams/btc-usd-cexprice-streams
    
if __name__ == "__main__":
    print(request_price("BTCUSDT"))