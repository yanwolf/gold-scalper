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
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone

import websocket  # pip package: websocket-client

from app import db

logger = logging.getLogger("binance_client")

SYMBOL = os.getenv("BINANCE_GOLD_SYMBOL", "xauusdt").lower()

PUBLIC_WS_URL = f"wss://fstream.binance.com/public/stream?streams={SYMBOL}@bookTicker"
MARKET_WS_URL = f"wss://fstream.binance.com/market/stream?streams={SYMBOL}@aggTrade"

MAX_TICK_HISTORY = 2000
MAX_TRADE_HISTORY = 100000  # 逐筆成交量比報價更新頻繁，保留更多筆給分析模組用
                            # (從20000提高到100000：5分K/15分K纏論一根K棒平均要吃掉
                            # 5倍於1分K的成交筆數才能湊滿，同樣的筆數上限對5分K來說
                            # 一直偏緊，尤其服務重啟、記憶體歸零重新累積時特別明顯。
                            # 100000筆實測記憶體約18MB、運算耗時0.06~0.09秒，成本可忽略，
                            # 15分K在這個資料量下能穩定拼出多個中樞，對趨勢/盤整背馳判斷
                            # 有實質幫助，見README修正記錄)
DB_FLUSH_INTERVAL_SECONDS = 20  # 多久把新累積的逐筆成交寫進資料庫一次

# 盤口(bookTicker)過期判定(修正記錄見README)：盤口事件時間落後最後成交超過
# BOOK_STALE_LAG_SECONDS，或盤口中價偏離最後成交價超過價格的BOOK_STALE_DIVERGENCE_RATIO
# (黃金4400時0.1%≈4.4點，正常情況中價與最後成交差不到1點)，視為過期不採用。
# 落後超過BOOK_RECONNECT_LAG_SECONDS則由watchdog強制重連盤口連線。
BOOK_STALE_LAG_SECONDS = 5.0
BOOK_STALE_DIVERGENCE_RATIO = 0.001
BOOK_WATCHDOG_INTERVAL_SECONDS = 10
BOOK_RECONNECT_LAG_SECONDS = 30.0
BOOK_RECONNECT_COOLDOWN = 60.0


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

    def force_reconnect(self):
        """
        強制關閉目前的WebSocket，讓_run_forever的迴圈自動重連。給「連線還在、
        但資料悶掉」的假死情況用——這種情況on_error/on_close都不會觸發，
        ping/pong也可能還是通的，只有資料本身停了，得靠外部watchdog主動踢
        (修正記錄見README)。
        """
        with self._status_lock:
            self._connected = False
            self._last_error = "watchdog強制重連(資料停滯)"
        if self._ws_app:
            try:
                self._ws_app.close()
            except Exception:
                pass

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
        self._watchdog_thread = None
        self._watchdog_stop_flag = threading.Event()
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
                "latest_book_time": self._latest_price.get("time") if self._latest_price else None,
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

    def get_book_freshness(self):
        """
        判斷盤口報價(bookTicker)相對成交流(aggTrade)有沒有過期(修正記錄見README)。

        兩條WebSocket是獨立的：盤口那條一旦假死(連線還在、沒資料)，_latest_price
        會凍結在舊值，成交流卻繼續跑。使用者實際遇到：訊號(用成交流算)說「站上
        4437」，拿來當基準的bid/ask卻還停在4398，40點的落差被誤判成滑點，帳面
        損益也跟著錯。這裡用兩邊的「交易所事件時間」直接比(不依賴本機時鐘)：
        盤口E落後最後一筆成交T超過門檻，或盤口中價跟最後成交價偏離太多，就視為
        過期。回傳dict：{"stale": bool, "lag_seconds": float|None,
        "divergence": float|None, "book_mid": float|None, "last_trade_price": float|None}
        """
        with self._lock:
            tick = self._latest_price
            last_trade = self._trade_history[-1] if self._trade_history else None
        info = {"stale": False, "lag_seconds": None, "divergence": None, "book_mid": None, "last_trade_price": None}
        if not tick or not tick.get("bid") or not tick.get("ask"):
            info["stale"] = True
            return info
        try:
            mid = (float(tick["bid"]) + float(tick["ask"])) / 2
        except (TypeError, ValueError):
            info["stale"] = True
            return info
        info["book_mid"] = mid
        if not last_trade:
            return info
        info["last_trade_price"] = last_trade["price"]
        try:
            lag = (float(last_trade["time"]) - float(tick["time"])) / 1000.0
        except (TypeError, ValueError):
            lag = None
        info["lag_seconds"] = lag
        info["divergence"] = abs(mid - last_trade["price"])
        if (lag is not None and lag > BOOK_STALE_LAG_SECONDS) or info["divergence"] > mid * BOOK_STALE_DIVERGENCE_RATIO:
            info["stale"] = True
        return info

    def _watchdog_loop(self):
        """
        每隔幾秒檢查盤口是否落後成交流太久；是的話強制重連盤口那條連線
        (限速：兩次強制重連至少間隔BOOK_RECONNECT_COOLDOWN秒，避免一直踢)。
        """
        last_kick = 0.0
        while not self._watchdog_stop_flag.is_set():
            time.sleep(BOOK_WATCHDOG_INTERVAL_SECONDS)
            try:
                f = self.get_book_freshness()
                lag = f.get("lag_seconds")
                if lag is not None and lag > BOOK_RECONNECT_LAG_SECONDS and (time.time() - last_kick) > BOOK_RECONNECT_COOLDOWN:
                    logger.warning(f"盤口報價落後成交流{lag:.0f}秒(偏離{f.get('divergence')})，強制重連bookTicker連線")
                    self._public_conn.force_reconnect()
                    last_kick = time.time()
            except Exception as e:
                logger.error(f"盤口watchdog檢查失敗: {e}")

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

        if not (self._watchdog_thread and self._watchdog_thread.is_alive()):
            self._watchdog_stop_flag.clear()
            self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
            self._watchdog_thread.start()

    def stop(self):
        self._public_conn.stop()
        self._market_conn.stop()
        self._flush_stop_flag.set()
        self._watchdog_stop_flag.set()


# 單例，供 main.py 匯入使用
binance_streamer = BinanceGoldStreamer()
