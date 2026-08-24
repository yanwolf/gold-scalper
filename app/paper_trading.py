"""
模擬單(paper trading)追蹤引擎 —— 即時版本。

實際的開倉/移動停損/出場判斷規則都在 app/trading_core.py (純函式，
跟資料庫、背景執行緒無關)，這個檔案只負責：
1. 背景執行緒定期呼叫 app/signal_engine.py 取得即時訊號
2. 呼叫 trading_core 的純函式決定要不要開倉/更新停損/出場
3. 把結果寫進資料庫(有接的話)，並且維護記憶體中的目前倉位狀態

回測(app/backtest.py)呼叫的是同一套 trading_core 純函式，
確保「即時模擬單」和「歷史回測」用的是完全一樣的交易規則。
"""

import os
import threading
import logging
from collections import deque
from datetime import datetime, timezone

from app.signal_engine import compute_full_signal
from app import db
from app import trading_core
from app.trading_stats import compute_stats, assess_readiness

logger = logging.getLogger("paper_trading")

PAPER_POLL_SECONDS = int(os.getenv("PAPER_POLL_SECONDS", "15"))
PAPER_SL_POINTS = float(os.getenv("PAPER_SL_POINTS", "3.0"))
PAPER_TRAIL_TRIGGER_POINTS = float(os.getenv("PAPER_TRAIL_TRIGGER_POINTS", "3.0"))
PAPER_TRAIL_DISTANCE_POINTS = float(os.getenv("PAPER_TRAIL_DISTANCE_POINTS", "3.0"))

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_BUCKET_SIZE = 1.0
DEFAULT_TRADE_LIMIT = 3000

MAX_MEMORY_TRADES = 500  # 沒有資料庫時，最多在記憶體保留這麼多筆已平倉紀錄


class PaperTradingEngine:
    def __init__(self):
        self._lock = threading.Lock()
        self._position = None
        self._closed_trades_memory = deque(maxlen=MAX_MEMORY_TRADES)
        self._thread = None
        self._stop_flag = threading.Event()
        self._seeded_from_db = False

    def start(self):
        if not self._seeded_from_db:
            with self._lock:
                self._position = db.get_open_paper_trade()
            self._seeded_from_db = True

        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info("模擬單追蹤引擎已啟動(移動停損模式)")

    def stop(self):
        self._stop_flag.set()

    def _run_forever(self):
        while not self._stop_flag.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.error(f"模擬單檢查失敗: {e}")
            self._stop_flag.wait(PAPER_POLL_SECONDS)

    def _tick(self):
        result = compute_full_signal(
            interval_seconds=DEFAULT_INTERVAL_SECONDS,
            bucket_size=DEFAULT_BUCKET_SIZE,
            trade_limit=DEFAULT_TRADE_LIMIT,
        )
        current_price = result.get("current_price")
        if current_price is None:
            return

        with self._lock:
            position = self._position

        if position:
            changed = trading_core.update_trailing_stop(
                position, current_price, PAPER_TRAIL_TRIGGER_POINTS, PAPER_TRAIL_DISTANCE_POINTS
            )
            if changed:
                db.update_paper_trade_stop(
                    position.get("id"), position["sl_price"], position["peak_price"], position["trailing_active"]
                )

            exit_reason = trading_core.check_exit(position, current_price, result["stage"], result["direction"])
            if exit_reason:
                self._close_position(position, current_price, exit_reason)
                position = None

        if position is None and result["stage"] == "訊號" and result["direction"]:
            self._open_position(result, current_price)

    def _open_position(self, signal_result, current_price):
        position = trading_core.open_position(
            direction=signal_result["direction"],
            current_price=current_price,
            entry_time=datetime.now(timezone.utc).isoformat(),
            sl_points=PAPER_SL_POINTS,
            chan_reason=signal_result["chan"]["reason"],
            profile_reason=signal_result["profile"]["reason"],
        )
        db_id = db.insert_open_paper_trade(position)
        position["id"] = db_id

        with self._lock:
            self._position = position

        logger.info(f"模擬單開倉: {position['direction']} @ {current_price:.2f} (初始SL:{position['sl_price']:.2f})")

    def _close_position(self, position, exit_price, exit_reason):
        exit_time = datetime.now(timezone.utc).isoformat()
        closed_record = trading_core.close_position(position, exit_price, exit_reason, exit_time)

        db.close_paper_trade(position.get("id"), exit_price, exit_time, exit_reason, closed_record["pnl_points"])
        self._closed_trades_memory.append(closed_record)

        with self._lock:
            self._position = None

        logger.info(
            f"模擬單平倉: {position['direction']} @ {exit_price:.2f} "
            f"({exit_reason}, 損益:{closed_record['pnl_points']:+.2f})"
        )

    def get_summary(self, limit=50):
        """
        績效摘要：總筆數、勝率、總損益、獲利因子、最大回撤、目前開倉狀態、
        最近N筆紀錄、以及對照「達標門檻」的評估結果。
        """
        if db.is_enabled():
            trades = db.get_closed_paper_trades(limit=max(limit, 500))
        else:
            trades = list(self._closed_trades_memory)[::-1]

        stats = compute_stats(trades)
        readiness = assess_readiness(stats)

        with self._lock:
            position = self._position

        return {
            **stats,
            "open_position": position,
            "recent_trades": trades[:limit],
            "sl_points": PAPER_SL_POINTS,
            "trail_trigger_points": PAPER_TRAIL_TRIGGER_POINTS,
            "trail_distance_points": PAPER_TRAIL_DISTANCE_POINTS,
            "readiness": readiness,
        }


# 單例，供 main.py 匯入使用
paper_trading = PaperTradingEngine()
