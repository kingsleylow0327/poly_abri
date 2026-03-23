import websocket
import json
import threading
import time

def request_price(symbol: str):
    url = f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}"
    response = requests.get(url)
    return float(response.json().get('price'))

class BinanceWebsocket:
    def __init__(self, symbol: str):
        self.ws = None
        self.thread = None
        self.running = False
        self.symbol = symbol
        self.current_market_data = {"price": 0.0, "symbol": ""}

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self.run_vsocket, args=(self.symbol,), daemon=True)
        self.thread.start()
        time.sleep(2)

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close()
        if self.thread and self.thread.is_alive():
            self.thread.join()

    def on_error(self, ws, error):
        print(f"[BinanceWebSocket] Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        print(f"[BinanceWebSocket] Closed | status: {close_status_code}, msg: {close_msg}")

    def on_message(self, ws, message):
        data = json.loads(message)
        # Update the shared dictionary
        self.current_market_data["price"] = float(data['c'])
        self.current_market_data["symbol"] = data['s']

    def run_vsocket(self, symbol: str):
        # Use lowercase symbol for the URL
        socket_url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@ticker"
        while self.running:
            try:
                self.ws = websocket.WebSocketApp(socket_url, 
                                            on_message=self.on_message,
                                            on_error=self.on_error,
                                            on_close=self.on_close)
                self.ws.run_forever(ping_interval=30)
            except Exception as e:
                print(f"[BinanceWebSocket] Exception: {e}")
            if self.running:
                print(f"[BinanceWebSocket] Reconnecting in 5 seconds...")
                time.sleep(5)
    
    def get_price(self):
        return self.current_market_data["price"]


if __name__ == "__main__":
    binance_websocket = BinanceWebsocket("BTCUSDT")
    binance_websocket.start()
    while True:
        print(binance_websocket.get_price())