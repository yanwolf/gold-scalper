"""
OANDA v20 streaming 背景執行緒模組。

負責在獨立 thread 中持續連線 OANDA pricing stream，
把最新的 bid/ask tick 寫進一個 thread-safe 的共享狀態，
給 FastAPI 的 REST / WebSocket endpoint 讀取。

用 thread（而非 asyncio 原生串流）是因為 requests 的
stream=True 是 blocking I/O，用獨立 thread 隔開最簡單可靠，
不用額外處理 async http streaming 的複雜度。
"""

import os
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone

import requests

INSTRUMENT = os.getenv("OANDA_INSTRUMENT", "XAU_USD")
ENVIRONMENT = os.getenv("OANDA_ENVIRONMENT", "practice")  # practice | live

BASE_URLS = {
    "practice": "https://stream-fxpractice.oanda.com",
    "live": "https://stream-fxtrade.oanda.com",
}

# 保留最近 N 筆 tick 在記憶體中，給分價量表等分析模組取用
MAX_TICK_HISTORY = 2000


class OandaGoldStreamer:
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
        """外層迴圈：斷線時自動重連，避免程式因為單次連線失敗而整個停掉。"""
        while not self._stop_flag.is_set():
            try:
                self._stream_once()
            except Exception as e:
                with self._lock:
                    self._connected = False
                    self._last_error = str(e)
            if not self._stop_flag.is_set():
                time.sleep(5)  # 重連前等待，避免無限快速重試

    def _stream_once(self):
        token = os.getenv("OANDA_API_TOKEN")
        account_id = os.getenv("OANDA_ACCOUNT_ID")
        if not token or not account_id:
            raise RuntimeError("缺少環境變數 OANDA_API_TOKEN / OANDA_ACCOUNT_ID")

        base_url = BASE_URLS.get(ENVIRONMENT, BASE_URLS["practice"])
        url = f"{base_url}/v3/accounts/{account_id}/pricing/stream"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"instruments": INSTRUMENT}

        with requests.get(url, headers=headers, params=params, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with self._lock:
                self._connected = True
                self._last_error = None

            for line in resp.iter_lines():
                if self._stop_flag.is_set():
                    break
                if not line:
                    continue
                data = json.loads(line.decode("utf-8"))

                if data.get("type") == "PRICE":
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    tick = {
                        "time": data.get("time"),
                        "instrument": data.get("instrument", INSTRUMENT),
                        "bid": bids[0]["price"] if bids else None,
                        "ask": asks[0]["price"] if asks else None,
                        "received_at": datetime.now(timezone.utc).isoformat(),
                    }
                    with self._lock:
                        self._latest_price = tick
                        self._tick_history.append(tick)


# 單例，供 main.py 匯入使用
streamer = OandaGoldStreamer()
