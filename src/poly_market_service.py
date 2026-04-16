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

TOPIC_TYPE = "orders_matched" 
WS_URL = "wss://ws-live-data.polymarket.com"

def get_current_5m_ts():
    return int(time.time() // 300) * 300

def get_next_5m_ts(ts):
    return ts + 300

def get_slug(ts):
    return f"btc-updown-5m-{ts}"


class PolyMarketWebsocket:
    def __init__(self):
        self.ws = None
        self.thread = None
        self.is_running = False
        self.current_ts = 0
        self.next_ts = 0
        self.price_dict = {"Up": 0.0, "Down": 0.0}
        self._heartbeat_thread = None
        self._rotator_thread = None

    def start(self):
        self.is_running = True
        self.current_ts = get_current_5m_ts()
        self.next_ts = get_next_5m_ts(self.current_ts)
        self.thread = threading.Thread(target=self._run_ws, daemon=True)
        self.thread.start()
        time.sleep(2)

    def stop(self):
        self.is_running = False
        if self.ws:
            self.ws.close()
        if self.thread and self.thread.is_alive():
            self.thread.join()

    def _subscribe(self, ts):
        if not self.ws:
            return
        slug = get_slug(ts)
        filter_str = json.dumps({"market_slug": slug}, separators=(',', ':'))
        payload = {
            "action": "subscribe",
            "subscriptions": [{"topic": "activity", "type": TOPIC_TYPE, "filters": filter_str}]
        }
        self.ws.send(json.dumps(payload))
        logger.info(f"--- Subscribed to: {slug} ---")

    def _unsubscribe(self, ts):
        if not self.ws:
            return
        slug = get_slug(ts)
        filter_str = json.dumps({"market_slug": slug}, separators=(',', ':'))
        payload = {
            "action": "unsubscribe",
            "subscriptions": [{"topic": "activity", "type": TOPIC_TYPE, "filters": filter_str}]
        }
        self.ws.send(json.dumps(payload))
        logger.info(f"--- Unsubscribed from: {slug} ---")

    def _heartbeat_loop(self):
        while self.is_running:
            try:
                if self.ws:
                    self.ws.send("ping")
            except Exception:
                pass
            time.sleep(5)

    def _rotator_loop(self):
        while self.is_running:
            try:
                if int(time.time()) >= self.next_ts:
                    new_ts = get_current_5m_ts()
                    self._unsubscribe(self.current_ts)
                    self.current_ts = new_ts
                    self.next_ts = get_next_5m_ts(self.current_ts)
                    self._subscribe(self.current_ts)
                    time.sleep(2)
            except Exception as e:
                logger.error(f"[PolyMarketWebSocket] Rotator error: {e}")
            time.sleep(1)

    def on_open(self, ws):
        self.is_running = True
        logger.info(f"[PolyMarketWebSocket] Connected.")
        self._subscribe(self.current_ts)

        self._heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()

        self._rotator_thread = threading.Thread(target=self._rotator_loop, daemon=True)
        self._rotator_thread.start()

    def on_error(self, ws, error):
        self.is_running = False
        logger.error(f"[PolyMarketWebSocket] Error: {error}")

    def on_close(self, ws, close_status_code, close_msg):
        self.is_running = False
        logger.info(f"[PolyMarketWebSocket] Closed | status: {close_status_code}, msg: {close_msg}")

    def on_message(self, ws, message):
        raw_data = message.strip()
        if raw_data in ("ping", "pong", ""):
            return

        try:
            event = json.loads(raw_data)
            data = event.get("payload")
            if data:
                self.price_dict[data.get("outcome")] = float(f"{float(data.get('price', 0)):.2f}")
        except Exception:
            pass

    def _run_ws(self):
        while self.is_running:
            try:
                self.ws = websocket.WebSocketApp(
                    WS_URL,
                    on_open=self.on_open,
                    on_message=self.on_message,
                    on_error=self.on_error,
                    on_close=self.on_close
                )
                self.ws.run_forever(ping_interval=30)
            except Exception as e:
                logger.error(f"[PolyMarketWebSocket] Exception: {e}")
            if self.is_running:
                logger.info(f"[PolyMarketWebSocket] Reconnecting in 5 seconds...")
                time.sleep(5)


def get_poly_price(socket: PolyMarketWebsocket) -> dict:
    return socket.price_dict


if __name__ == "__main__":
    monitor = PolyMarketWebsocket()
    monitor.start()
    try:
        while True:
            price = get_poly_price(monitor)
            logger.info(f"UP: {price['Up']:.2f} | DOWN: {price['Down']:.2f}")
            time.sleep(1)
    except KeyboardInterrupt:
        monitor.stop()
        logger.info("Stopped.")