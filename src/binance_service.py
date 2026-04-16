import websocket
import json
import threading
import time
import logging

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 1 Day in seconds
RESTART_DURATION = 86400

class BinanceWebsocket:
    def __init__(self, symbol: str):
        self.ws = None
        self.thread = None
        self._rotator_thread = None
        self.is_running = False
        self.symbol = f"{symbol}usdt".upper()
        self.current_market_data = {"price": 0.0, "symbol": ""}

    def start(self):
        self.is_running = True
        self.thread = threading.Thread(target=self.run_vsocket, args=(self.symbol,), daemon=True)
        self.thread.start()
        self._rotator_thread = threading.Thread(target=self._rotator_loop, daemon=True)
        self._rotator_thread.start()

    def stop(self):
        self.is_running = False
        if self.ws:
            self.ws.close()
        if self.thread and self.thread.is_alive():
            self.thread.join()

    def on_open(self, ws):
        self.is_running = True
        logger.info("[BinanceWebSocket] Connected")

    def on_error(self, ws, error):
        self.is_running = False
        logger.error(f"[BinanceWebSocket] Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        self.is_running = False
        logger.info(f"[BinanceWebSocket] Closed | status: {close_status_code}, msg: {close_msg}")

    def on_message(self, ws, message):
        data = json.loads(message)
        self.current_market_data["price"] = float(data['c'])
        self.current_market_data["symbol"] = data['s']
    
    def update_symbol(self, new_symbol: str):
        logger.info(f"Switching monitor to: {new_symbol}")
        self.is_running = False
        if self.ws:
            self.ws.close()
        
        self.symbol = new_symbol.upper()
        self.current_market_data = {"price": 0.0, "symbol": self.symbol}
        
        self.start()

    def run_vsocket(self, symbol: str):
        socket_url = f"wss://stream.binance.com:9443/ws/{symbol.lower()}@ticker"
        while self.is_running:
            try:
                self.ws = websocket.WebSocketApp(socket_url, 
                                            on_open=self.on_open,
                                            on_message=self.on_message,
                                            on_error=self.on_error,
                                            on_close=self.on_close)
                self.ws.run_forever(ping_interval=30)
            except Exception as e:
                logger.error(f"[BinanceWebSocket] Exception: {e}")
            if self.is_running:
                logger.info(f"[BinanceWebSocket] Reconnecting in 5 seconds...")
                time.sleep(5)
    
    def _rotator_loop(self):
        while True:
            now = time.time()
            sleep_secs = RESTART_DURATION - (now % RESTART_DURATION)
            time.sleep(sleep_secs)
            if not self.is_running:
                break
            try:
                logger.info("[BinanceWebSocket] Rotator: re-initiating websocket...")
                self.is_running = False
                if self.ws:
                    self.ws.close()
                if self.thread and self.thread.is_alive():
                    self.thread.join(timeout=5)
                self.is_running = True
                self.thread = threading.Thread(target=self.run_vsocket, args=(self.symbol,), daemon=True)
                self.thread.start()
            except Exception as e:
                logger.error(f"[BinanceWebSocket] Rotator error: {e}")
            

def get_binance_price(socket: BinanceWebsocket) -> str:
    symbol = socket.current_market_data["symbol"]
    price = socket.current_market_data["price"]
    return {symbol: float(f"{price:.2f}")}

def change_monitored_ticker(symbol: str) -> str:
    monitor.update_symbol(symbol)
    return f"Now monitoring {symbol}. Please wait a moment for the first price update."

if __name__ == "__main__":
    monitor = BinanceWebsocket("BTCUSDT")
    monitor.start()
    while True:
        print(get_binance_price(monitor))
        time.sleep(1)
