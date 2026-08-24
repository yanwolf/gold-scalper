"""
Binance Futures XAUUSDT 即時報價 streaming 模組。

架構刻意跟 oanda_client.py 對稱（同樣是背景 thread、同樣的共享狀態介面），
方便 main.py 用同一套模式接兩個資料源，之後也方便再擴充第三個來源。

Binance 市場資料（bookTicker: 最佳買賣價）不需要 API Key，
公開 WebSocket 就能訂閱，比 OANDA 簡單一些。
"""

import os
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone

import websocket  # pip package: websocket-client

SYMBOL = os.getenv("BINANCE_GOLD_SYMBOL", "xauusdt")
WS_URL = f"wss://fstream.binance.com/ws/{SYMBOL}@bookTicker"

MAX_TICK_HISTORY = 2000


class BinanceGoldStreamer:
    def __init__(self):
        self._lock = threading.Lock()
        self._latest_price = None
        self._tick_history = deque(maxlen=MAX_TICK_HISTORY)
        self._connected = False
        self._last_error = None
        self._thread = None
        self._ws_app = None
        self._stop_flag = threading.Event()

    @property
    def status(self):
        with self._lock:
            return {
                "connected": self._connected,
                "last_error": self._last_error,
                "latest_price": self._latest_price,
                "tick_count": len(self._tick_history),
            }

    def get_latest(self):
        with self._lock:
            return self._latest_price

    def get_recent_ticks(self, limit: int = 200):
        with self._lock:
            return list(self._tick_history)[-limit:]

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_flag.set()
        if self._ws_app:
            self._ws_app.close()

    def _run_forever(self):
        """外層迴圈：斷線自動重連，邏輯跟 OANDA streamer 保持一致。"""
        while not self._stop_flag.is_set():
            try:
                self._connect_once()
            except Exception as e:
                with self._lock:
                    self._connected = False
                    self._last_error = str(e)
            if not self._stop_flag.is_set():
                time.sleep(5)

    def _connect_once(self):
        def on_open(ws):
            with self._lock:
                self._connected = True
                self._last_error = None

        def on_message(ws, message):
            data = json.loads(message)
            # bookTicker 欄位: b=best bid price, a=best ask price
            tick = {
                "time": data.get("E"),  # event time (epoch ms)
                "instrument": data.get("s", SYMBOL.upper()),
                "bid": data.get("b"),
                "ask": data.get("a"),
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
            with self._lock:
                self._latest_price = tick
                self._tick_history.append(tick)

        def on_error(ws, error):
            with self._lock:
                self._connected = False
                self._last_error = str(error)

        def on_close(ws, close_status_code, close_msg):
            with self._lock:
                self._connected = False

        self._ws_app = websocket.WebSocketApp(
            WS_URL,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        # run_forever 是 blocking call，會一直跑到連線關閉或出錯才返回
        self._ws_app.run_forever(ping_interval=20, ping_timeout=10)


# 單例，供 main.py 匯入使用
binance_streamer = BinanceGoldStreamer()
