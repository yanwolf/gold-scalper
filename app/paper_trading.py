"""
模擬單(paper trading)追蹤引擎。

用途：在訊號階段升級成「訊號」時虛擬開一筆倉位(不是真的下單，只是紀錄)，
之後每個檢查週期比對現價跟停損/停利，或是訊號反轉時出場，紀錄每筆盈虧。
目的是在正式接軌Pepperstone MT5自動下單前，先用一段時間的模擬單績效
(勝率、獲利因子、期望值)來判斷這套訊號邏輯值不值得真的接execution。

出場規則(三選一，先到先出)：
1. 觸價停損(SL)
2. 觸價停利(TP)
3. 訊號反轉：出現方向相反的「訊號」時，視為原倉位理由不再成立，出場

同一時間只維護一筆模擬倉位，不做加倉/多筆並存，保持邏輯單純、
方便看懂每一筆的因果關係。

風控參數(PAPER_SL_POINTS/PAPER_TP_POINTS)是保守預設值，可以透過環境變數調整，
不需要改程式碼重新部署以外的操作。
"""

import os
import threading
import logging
from collections import deque
from datetime import datetime, timezone

from app.signal_engine import compute_full_signal
from app import db

logger = logging.getLogger("paper_trading")

PAPER_POLL_SECONDS = int(os.getenv("PAPER_POLL_SECONDS", "15"))
PAPER_SL_POINTS = float(os.getenv("PAPER_SL_POINTS", "3.0"))   # 保守預設：停損3美元(以黃金每點=1美元計)
PAPER_TP_POINTS = float(os.getenv("PAPER_TP_POINTS", "6.0"))   # 停利6美元，風報比預設1:2

DEFAULT_INTERVAL_SECONDS = 60  # 跟notifier.py保持一致，用1分鐘K線判斷
DEFAULT_BUCKET_SIZE = 1.0
DEFAULT_TRADE_LIMIT = 3000

MAX_MEMORY_TRADES = 500  # 沒有資料庫時，最多在記憶體保留這麼多筆已平倉紀錄


class PaperTradingEngine:
    def __init__(self):
        self._lock = threading.Lock()
        self._position = None  # 目前開倉中的模擬單，None代表空手
        self._closed_trades_memory = deque(maxlen=MAX_MEMORY_TRADES)  # DB沒開時的備用儲存
        self._thread = None
        self._stop_flag = threading.Event()
        self._seeded_from_db = False

    def start(self):
        # 放在start()而不是__init__，確保main.py的db.init_schema()已經先執行過
        if not self._seeded_from_db:
            with self._lock:
                self._position = db.get_open_paper_trade()
            self._seeded_from_db = True

        if self._thread and self._thread.is_alive():
            return
        self._stop_flag.clear()
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info("模擬單追蹤引擎已啟動")

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
            exit_reason = self._check_exit(position, current_price, result)
            if exit_reason:
                self._close_position(position, current_price, exit_reason)
                position = None

        if position is None and result["stage"] == "訊號" and result["direction"]:
            self._open_position(result, current_price)

    def _check_exit(self, position, current_price, signal_result):
        direction = position["direction"]

        if direction == "bullish":
            if current_price <= position["sl_price"]:
                return "觸及停損"
            if current_price >= position["tp_price"]:
                return "觸及停利"
        else:
            if current_price >= position["sl_price"]:
                return "觸及停損"
            if current_price <= position["tp_price"]:
                return "觸及停利"

        if signal_result["stage"] == "訊號" and signal_result["direction"] and signal_result["direction"] != direction:
            return "訊號反轉"

        return None

    def _open_position(self, signal_result, current_price):
        direction = signal_result["direction"]
        if direction == "bullish":
            sl_price = current_price - PAPER_SL_POINTS
            tp_price = current_price + PAPER_TP_POINTS
        else:
            sl_price = current_price + PAPER_SL_POINTS
            tp_price = current_price - PAPER_TP_POINTS

        position = {
            "direction": direction,
            "entry_price": current_price,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "sl_price": sl_price,
            "tp_price": tp_price,
            "chan_reason": signal_result["chan"]["reason"],
            "profile_reason": signal_result["profile"]["reason"],
        }
        db_id = db.insert_open_paper_trade(position)
        position["id"] = db_id

        with self._lock:
            self._position = position

        logger.info(f"模擬單開倉: {direction} @ {current_price:.2f} (SL:{sl_price:.2f} TP:{tp_price:.2f})")

    def _close_position(self, position, exit_price, exit_reason):
        direction = position["direction"]
        pnl_points = (
            (exit_price - position["entry_price"]) if direction == "bullish"
            else (position["entry_price"] - exit_price)
        )
        exit_time = datetime.now(timezone.utc).isoformat()

        db.close_paper_trade(position.get("id"), exit_price, exit_time, exit_reason, pnl_points)

        closed_record = {
            **position,
            "exit_price": exit_price,
            "exit_time": exit_time,
            "exit_reason": exit_reason,
            "pnl_points": pnl_points,
        }
        self._closed_trades_memory.append(closed_record)

        with self._lock:
            self._position = None

        logger.info(f"模擬單平倉: {direction} @ {exit_price:.2f} ({exit_reason}, 損益:{pnl_points:+.2f})")

    def get_summary(self, limit=50):
        """
        績效摘要：總筆數、勝率、總損益(points)、獲利因子、平均獲利/虧損，
        以及目前開倉狀態和最近N筆紀錄，供dashboard渲染。
        """
        if db.is_enabled():
            trades = db.get_closed_paper_trades(limit=max(limit, 500))
        else:
            trades = list(self._closed_trades_memory)[::-1]  # 記憶體版本由新到舊

        total = len(trades)
        wins = [t for t in trades if t["pnl_points"] and t["pnl_points"] > 0]
        losses = [t for t in trades if t["pnl_points"] and t["pnl_points"] <= 0]

        total_pnl = sum(t["pnl_points"] for t in trades if t["pnl_points"] is not None)
        gross_profit = sum(t["pnl_points"] for t in wins)
        gross_loss = abs(sum(t["pnl_points"] for t in losses))

        win_rate = (len(wins) / total * 100) if total > 0 else 0.0
        avg_win = (gross_profit / len(wins)) if wins else 0.0
        avg_loss = (gross_loss / len(losses)) if losses else 0.0
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)

        with self._lock:
            position = self._position

        return {
            "total_trades": total,
            "win_count": len(wins),
            "loss_count": len(losses),
            "win_rate": round(win_rate, 1),
            "total_pnl_points": round(total_pnl, 2),
            "avg_win_points": round(avg_win, 2),
            "avg_loss_points": round(avg_loss, 2),
            "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else None,
            "open_position": position,
            "recent_trades": trades[:limit],
            "sl_points": PAPER_SL_POINTS,
            "tp_points": PAPER_TP_POINTS,
        }


# 單例，供 main.py 匯入使用
paper_trading = PaperTradingEngine()
