"""
Binance Futures XAUUSDT 即時報價 + 逐筆成交 streaming 模組。

架構刻意跟 oanda_client.py 對稱（同樣是背景 thread、同樣的共享狀態介面）。

重要背景（2026年Binance WebSocket改版）：
Binance 把 WebSocket 資料流分成 /public、/market、/private 三個路由。
- bookTicker（最佳買賣報價）屬於 /public
- aggTrade（逐筆成交，含真實成交量）屬於 /market
沒有指定路由的舊式連線方式現在只會收到 /public 的資料，/market 底下的頻道
會被靜默丟棄（不會報錯，只是收不到資料），所以這裡拆成兩條獨立連線，
分別接 /public 和 /market，避免同一個問題再發生。

都不需要 API Key，公開市場資料。
"""

import os
import json
import threading
import time
from collections import deque
from datetime import datetime, timezone

import websocket  # pip package: websocket-client

from app import db

SYMBOL = os.getenv("BINANCE_GOLD_SYMBOL", "xauusdt").lower()

PUBLIC_WS_URL = f"wss://fstream.binance.com/public/stream?streams={SYMBOL}@bookTicker"
MARKET_WS_URL = f"wss://fstream.binance.com/market/stream?streams={SYMBOL}@aggTrade"

MAX_TICK_HISTORY = 2000
MAX_TRADE_HISTORY = 20000  # 逐筆成交量比報價更新頻繁，保留更多筆給分析模組用
DB_FLUSH_INTERVAL_SECONDS = 20  # 多久把新累積的逐筆成交寫進資料庫一次


class _SingleStreamConnection:
    """
    共用的單條 WebSocket 連線邏輯（斷線自動重連），
    給 public(bookTicker) 和 market(aggTrade) 各開一個實例，互不干擾。
    """

    def __init__(self, url, on_data_callback):
        self._url = url
        self._on_data_callback = on_data_callback
        self._connected = False
        self._last_error = None
        self._thread = None
        self._ws_app = None
        self._stop_flag = threading.Event()
        self._status_lock = threading.Lock()

    @property
    def connected(self):
        with self._status_lock:
            return self._connected

    @property
    def last_error(self):
        with self._status_lock:
            return self._last_error

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
        while not self._stop_flag.is_set():
            try:
                self._connect_once()
            except Exception as e:
                with self._status_lock:
                    self._connected = False
                    self._last_error = str(e)
            if not self._stop_flag.is_set():
                time.sleep(5)

    def _connect_once(self):
        def on_open(ws):
            with self._status_lock:
                self._connected = True
                self._last_error = None

        def on_message(ws, message):
            envelope = json.loads(message)
            data = envelope.get("data", envelope)  # combined stream才有data包裝，保險起見兩種都處理
            self._on_data_callback(data)

        def on_error(ws, error):
            with self._status_lock:
                self._connected = False
                self._last_error = str(error)

        def on_close(ws, close_status_code, close_msg):
            with self._status_lock:
                self._connected = False

        self._ws_app = websocket.WebSocketApp(
            self._url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        self._ws_app.run_forever(ping_interval=20, ping_timeout=10)


class BinanceGoldStreamer:
    def __init__(self):
        self._lock = threading.Lock()
        self._latest_price = None
        self._tick_history = deque(maxlen=MAX_TICK_HISTORY)
        self._trade_history = deque(maxlen=MAX_TRADE_HISTORY)
        self._unflushed_trades = []  # 累積尚未寫入資料庫的新成交，由 flush thread 定期清空
        self._seeded_from_db = False

        self._public_conn = _SingleStreamConnection(PUBLIC_WS_URL, self._handle_book_ticker)
        self._market_conn = _SingleStreamConnection(MARKET_WS_URL, self._handle_agg_trade)

        self._flush_thread = None
        self._flush_stop_flag = threading.Event()

    @property
    def status(self):
        with self._lock:
            latest_trade_time = self._trade_history[-1]["time"] if self._trade_history else None
            return {
                "connected": self._public_conn.connected and self._market_conn.connected,
                "public_connected": self._public_conn.connected,
                "market_connected": self._market_conn.connected,
                "last_error": self._public_conn.last_error or self._market_conn.last_error,
                "latest_price": self._latest_price,
                "tick_count": len(self._tick_history),
                "trade_count": len(self._trade_history),
                "latest_trade_time": latest_trade_time,  # epoch ms，給health_monitor.py判斷資料是否停滯用
                "db_persistence_enabled": db.is_enabled(),
            }

    def get_latest_trade_time(self):
        """
        最新一筆成交的時間戳(epoch ms)。用來判斷資料是否真的停滯——
        trade_count在成交量累積超過MAX_TRADE_HISTORY上限後會卡住不再變化
        (因為deque滿了，新增一筆就會擠掉最舊一筆，總數不變)，不能拿來當作
        「資料是否還在更新」的判斷依據，要看最新一筆的實際時間才準確。
        """
        with self._lock:
            if self._trade_history:
                return self._trade_history[-1]["time"]
            return None

    def get_latest(self):
        with self._lock:
            return self._latest_price

    def get_recent_ticks(self, limit: int = 200):
        with self._lock:
            return list(self._tick_history)[-limit:]

    def get_recent_trades(self, limit: int = 2000):
        """給分析模組（分價量表、K線聚合）用的逐筆成交資料。"""
        with self._lock:
            return list(self._trade_history)[-limit:]

    def _handle_book_ticker(self, data):
        tick = {
            "time": data.get("E"),
            "instrument": data.get("s", SYMBOL.upper()),
            "bid": data.get("b"),
            "ask": data.get("a"),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        with self._lock:
            self._latest_price = tick
            self._tick_history.append(tick)

    def _handle_agg_trade(self, data):
        # aggTrade 欄位: p=成交價, q=成交量, T=成交時間(ms), m=是否為賣方主動成交(maker)
        trade = {
            "time": data.get("T"),
            "price": float(data.get("p", 0)),
            "qty": float(data.get("q", 0)),
            "is_buyer_maker": data.get("m"),
        }
        with self._lock:
            self._trade_history.append(trade)
            self._unflushed_trades.append(trade)

    def _flush_loop(self):
        """定期把累積的新成交批次寫進資料庫，不是每筆都馬上寫，減少資料庫負擔。"""
        while not self._flush_stop_flag.is_set():
            self._flush_stop_flag.wait(DB_FLUSH_INTERVAL_SECONDS)
            with self._lock:
                to_flush = self._unflushed_trades
                self._unflushed_trades = []
            if to_flush:
                db.insert_trades(to_flush)

    def start(self):
        # 這裡才做歷史資料回填(而不是__init__)，因為main.py會先呼叫db.init_schema()
        # 建立好資料庫連線，才呼叫streamer.start()，順序上要確保db已經就緒
        if not self._seeded_from_db:
            seeded = db.load_recent_trades(limit=MAX_TRADE_HISTORY)
            if seeded:
                with self._lock:
                    self._trade_history.extend(seeded)
            self._seeded_from_db = True

        self._public_conn.start()
        self._market_conn.start()

        if db.is_enabled() and not (self._flush_thread and self._flush_thread.is_alive()):
            self._flush_stop_flag.clear()
            self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
            self._flush_thread.start()

    def stop(self):
        self._public_conn.stop()
        self._market_conn.stop()
        self._flush_stop_flag.set()


# 單例，供 main.py 匯入使用
binance_streamer = BinanceGoldStreamer()
