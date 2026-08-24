"""
GoldAPI.io 輪詢式報價模組。

跟 oanda_client.py / binance_client.py 共用同一套「背景 thread + 共享狀態」介面，
差別在於 GoldAPI.io 是 REST 輪詢（沒有 streaming），所以用 sleep 間隔取代
long-lived connection。免費方案有請求次數限制，預設輪詢間隔設寬鬆一點（60秒），
避免超額。

用途：低頻confirmation訊號源，跟 Binance 的 tick 級資料做交叉比對，
不當作極短線的主要判斷依據。
"""

import os
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests

API_URL = "https://www.goldapi.io/api/XAU/USD"
MAX_TICK_HISTORY = 500


class GoldApiStreamer:
    def __init__(self):
        self._lock = threading.Lock()
        self._latest_price = None
        self._tick_history = deque(maxlen=MAX_TICK_HISTORY)
        self._connected = False
        self._last_error = None
        self._thread = None
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

    def _run_forever(self):
        poll_interval = int(os.getenv("GOLDAPI_POLL_INTERVAL_SECONDS", "60"))
        while not self._stop_flag.is_set():
            try:
                self._poll_once()
            except Exception as e:
                with self._lock:
                    self._connected = False
                    self._last_error = str(e)
            # 用 wait 而不是 sleep，讓 stop() 呼叫時能立刻中斷等待，不用等到下個週期
            self._stop_flag.wait(poll_interval)

    def _poll_once(self):
        api_key = os.getenv("GOLDAPI_KEY")
        if not api_key:
            raise RuntimeError("缺少環境變數 GOLDAPI_KEY")

        headers = {"x-access-token": api_key}
        resp = requests.get(API_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        tick = {
            "time": data.get("timestamp"),
            "instrument": "XAU/USD",
            "price": data.get("price"),
            "bid": data.get("bid"),
            "ask": data.get("ask"),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._connected = True
            self._last_error = None
            self._latest_price = tick
            self._tick_history.append(tick)


# 單例，供 main.py 匯入使用
goldapi_streamer = GoldApiStreamer()
